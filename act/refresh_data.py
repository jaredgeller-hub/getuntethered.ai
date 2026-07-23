#!/usr/bin/env python3
"""
GetUntethered — annual data refresh.

Pulls tax + buyback numbers for every company from the SEC's FREE public data
(no API key, no subscription) and rebuilds the extension's data files.

WHAT IT DOES
  1. Reads companies_config.json (your company list + ITEP fallback figures).
  2. Resolves each company's SEC CIK from the SEC's free ticker file.
  3. Pulls each company's XBRL "company facts" from data.sec.gov and extracts:
       - U.S. domestic pretax income   (IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic)
       - current federal tax           (CurrentFederalTaxExpenseBenefit)
       - stock buybacks                (PaymentsForRepurchaseOfCommonStock, with fallback)
  4. Classifies each company (full / rate_only / refund), computes the gap,
     picks a scale comparison, and writes:
       - extraction-panel-data.json   (source of truth)
       - extraction-data.js           (what the extension loads)

WHERE IT RUNS
  Anywhere with internet access to the SEC (your laptop, or a Render cron job).
  It CANNOT run inside the Claude sandbox — that environment is walled off from
  data.sec.gov. The first real run is the test; it logs everything it does.

USAGE
  pip install requests
  python3 refresh_data.py

  Optional flags:
    --limit N     only process the first N companies (for a quick test run)
    --verbose     print every company as it goes

RATE LIMITS
  The SEC allows ~10 requests/sec. This script sleeps 0.2s between calls to stay
  well under that. A full 259-company run takes a few minutes.

NOTE ON CEO PAY
  CEO / worker pay is deliberately NOT pulled here yet. See extract_ceo_pay()
  at the bottom for why, and the plan to add it safely.
"""

import json
import re
import sys
import time
import argparse
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# SEC requires a descriptive User-Agent with a contact email on every request.
USER_AGENT = "GetUntethered hello@getuntethered.ai"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

STATUTORY_RATE = 0.21
REQUEST_SLEEP = 0.2  # seconds between SEC calls (well under the 10/sec limit)

# Data-quality guardrails. When EDGAR's numbers fall outside these bounds, the
# computed rate is almost always a data artifact (tiny/odd denominator, a tag
# reported as zero, etc.), so we defer to ITEP's vetted figure instead of
# shipping a garbage number like "-727% tax rate".
SANE_ETR_MIN = -40.0   # below this, a "refund rate" is noise, not a real refund
SANE_ETR_MAX = 45.0    # above 21% is possible; beyond this is almost always an artifact
MIN_PRETAX_B = 0.10    # under ~$100M domestic pretax, the rate ratio is too noisy to trust

# XBRL tags (these are the ones proven to work for this use case)
TAG_PRETAX_DOMESTIC = "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"
TAG_CURRENT_FEDERAL = "CurrentFederalTaxExpenseBenefit"
TAG_BUYBACKS = ["PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"]

# Scale-comparison programs, tiered by size of the gap (annual federal cost).
PROGRAMS = {
    "school_lunch": {"cost_b": 16.3, "label": "the National School Lunch Program",
                     "detail": "National School Lunch Program: $16.3B/year, feeds 29.7M children daily"},
    "vet_homeless": {"cost_b": 3.2, "label": "federal veterans homelessness programs",
                     "detail": "VA homelessness programs: $3.2B/year"},
    "va_suicide":   {"cost_b": 0.583, "label": "VA suicide prevention programs",
                     "detail": "VA suicide prevention programs: $583M/year"},
}

# Companies whose SEC name doesn't cleanly match your label. Add "Your Name": "TICKER"
# here whenever the run log reports one as UNRESOLVED.
TICKER_OVERRIDES = {
    # Companies the auto-match missed:
    "Petco Health and Wellness": "WOOF",
    "Kohl's": "KSS",
    "Domino's Pizza": "DPZ",
    # ASGN renamed itself Everforth, Inc. and moved from ticker ASGN to EFOR
    # on 2026-04-24. Same CIK, same filings — only the ticker changed.
    "ASGN": "EFOR",
    "Brink's": "BCO",
    "J.P. Morgan Chase & Co.": "JPM",
    # Coterra merged into Devon Energy and was delisted 2026-05-07; CTRA no
    # longer exists as a listed ticker, so this stays UNRESOLVED on purpose and
    # the company keeps its last ITEP figures. Left here as a record of why.
    "Coterra Energy": "CTRA",
    "Instacart": "CART",
    "Colgate-Palmolive": "CL",
    "Sherwin-Williams": "SHW",
    # IAC renamed itself People Incorporated and moved from ticker IAC to PPLI
    # on 2026-06-04. Same CIK — only the name and ticker changed. The key here
    # must match the company name in companies_config.json.
    "People Incorporated": "PPLI",
    # Companies the auto-match got WRONG (matched to a similarly-named company):
    "IDEX": "IEX",
    "IDEXX Laboratories": "IDXX",
    "SAIC": "SAIC",
    "Mosaic": "MOS",
    "Seaboard": "SEB",
    # Sealed Air was taken private by CD&R and delisted 2026-04-09. SEE is no
    # longer a listed ticker — stays UNRESOLVED on purpose, keeps ITEP figures.
    "Sealed Air": "SEE",
    "Coca-Cola": "KO",
    # Companies strict matching skipped — pinned by ticker:
    "Waters": "WAT",
    "Bank of America": "BAC",
    "Newmont": "NEM",
    "Academy Sports": "ASO",
    "SPX": "SPXC",
    "Pitney Bowes": "PBI",
    "Teradata": "TDC",
    "UGI": "UGI",
    "Telephone & Data Systems": "TDS",
    "ArcBest": "ARCB",
    "Graphic Packaging": "GPK",
    "Portland General Electric": "POR",
    "Exxon Mobil": "XOM",
    "Greenbrier": "GBX",
    "US Foods": "USFD",
    "Factset": "FDS",
    "L3Harris Technologies": "LHX",
    "Sempra Energy": "SRE",
    "Oneok": "OKE",
    "Williams": "WMB",
    "Martin Marietta": "MLM",
    "Entergy": "ETR",
    "Fortune Brands": "FBIN",
    "Mattel": "MAT",
    "Paycom": "PAYC",
    "Devon Energy": "DVN",
    "Meta": "META",
    "Mettler Toledo": "MTD",
    "Uber": "UBER",
    # Continental Resources went private in 2022 — no current filings; stays on ITEP data.
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch_json(url, tries=3):
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            # 429 or transient: back off and retry
            time.sleep(1.0 + attempt)
        except requests.RequestException:
            time.sleep(1.0 + attempt)
    return None


# ---------------------------------------------------------------------------
# CIK resolution
# ---------------------------------------------------------------------------

def normalize_name(name):
    n = name.upper()
    n = re.sub(r"[.,&']", " ", n)
    n = re.sub(r"\b(INC|CORP|CORPORATION|CO|COMPANY|LTD|PLC|HOLDINGS|GROUP|THE|LLC|LP)\b", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def build_cik_index():
    """Returns (by_ticker, by_name) mapping to zero-paddable int CIKs."""
    data = fetch_json(SEC_TICKERS_URL)
    if not data:
        sys.exit("Could not download the SEC ticker file. Check your internet / User-Agent.")
    by_ticker, by_name = {}, {}
    for row in data.values():
        cik = int(row["cik_str"])
        by_ticker[row["ticker"].upper()] = cik
        by_name[normalize_name(row["title"])] = cik
    return by_ticker, by_name


def resolve_cik(name, ticker, by_ticker, by_name):
    """Returns (cik, pinned). pinned=True means we resolved it from an explicit
    ticker, which we trust over any fuzzy name match."""
    if ticker and ticker.upper() in by_ticker:
        return by_ticker[ticker.upper()], True
    override = TICKER_OVERRIDES.get(name)
    if override and override.upper() in by_ticker:
        return by_ticker[override.upper()], True
    key = normalize_name(name)
    if key in by_name:
        return by_name[key], False
    # No loose "contains" fallback — it caused wrong-company matches.
    # Better to report UNRESOLVED (safe) than to guess (silently wrong).
    return None, False


# ---------------------------------------------------------------------------
# XBRL extraction
# ---------------------------------------------------------------------------

def latest_annual(facts, tag, unit="USD"):
    """Most recent FY 10-K value for a us-gaap tag, or None."""
    try:
        entries = facts["facts"]["us-gaap"][tag]["units"][unit]
    except (KeyError, TypeError):
        return None
    annual = [e for e in entries if e.get("form") == "10-K" and e.get("fp") == "FY" and e.get("val") is not None]
    if not annual:
        return None
    annual.sort(key=lambda e: e.get("end", ""))
    return annual[-1]  # {"val":..., "end":..., "fy":..., ...}


def extract_edgar(cik):
    """Returns dict of pretax income, federal tax, buybacks (in $B) + fy_end, or None."""
    facts = fetch_json(COMPANYFACTS_URL.format(cik=cik))
    if not facts:
        return None
    out = {"pretaxB": None, "fedtaxB": None, "buybacksB": None, "fyEnd": None}

    pretax = latest_annual(facts, TAG_PRETAX_DOMESTIC)
    fedtax = latest_annual(facts, TAG_CURRENT_FEDERAL)
    if pretax:
        out["pretaxB"] = round(pretax["val"] / 1e9, 3)
        out["fyEnd"] = pretax.get("end")
    if fedtax:
        out["fedtaxB"] = round(fedtax["val"] / 1e9, 3)
        out["fyEnd"] = out["fyEnd"] or fedtax.get("end")

    for tag in TAG_BUYBACKS:
        bb = latest_annual(facts, tag)
        if bb:
            out["buybacksB"] = round(abs(bb["val"]) / 1e9, 3)
            break

    return out


# ---------------------------------------------------------------------------
# Classification + derived fields
# ---------------------------------------------------------------------------

def make_comparison(gap_b):
    """Pick a program and phrase the gap as a human-scale line."""
    if gap_b is None or gap_b <= 0:
        return None
    for key in ("school_lunch", "vet_homeless", "va_suicide"):
        p = PROGRAMS[key]
        # use the largest program the gap is meaningfully comparable to
        if gap_b >= p["cost_b"] * 0.5 or key == "va_suicide":
            if gap_b >= p["cost_b"]:
                mult = gap_b / p["cost_b"]
                if mult >= 1.15:
                    line = f"About {mult:.1f}\u00d7 the annual cost of {p['label']}."
                else:
                    line = f"More than the entire annual cost of {p['label']}."
            else:
                months = max(1, round(gap_b / p["cost_b"] * 12))
                line = f"Roughly {months} months of {p['label']}."
            return {"program": key, "line": line, "detail": p["detail"]}
    return None


def final_check(rec):
    """
    Last line of defense, applied to the number we're actually about to ship —
    no matter which source it came from. The ITEP fallback can itself be wild
    (e.g. -630%), and a rate like that is a data artifact, not a finding. We do
    not publish it; the company reads as "no public record yet" instead.

    Returns a reason string when the record was suppressed, else None.
    """
    etr = rec.get("etr")
    if etr is None:
        return None
    if not (SANE_ETR_MIN <= etr <= SANE_ETR_MAX):
        rec["format"] = "rate_only"
        rec["etr"] = None
        rec["needsReview"] = True
        for k in ("gapB", "comparison", "fedtaxRefund", "pretaxB", "fedtaxB"):
            rec.pop(k, None)
        return f"final rate {etr}% implausible from every source — suppressed, shows as no-record"
    return None


def classify(company, edgar):
    """
    Decide format + compute fields. Conservative: only trust EDGAR when it gives a
    clean domestic pretax income AND a federal tax number AND the resulting rate is
    plausible. Otherwise fall back to ITEP's vetted effective rate.

    Returns (record, review_reason). review_reason is a string when EDGAR data was
    rejected as implausible (worth an eyeball), otherwise None.
    """
    itep_etr = company.get("itep_etr")
    rec = {"dataSource": company.get("dataSource", "ITEP"),
           "buybacksB": (edgar or {}).get("buybacksB")}

    pretax = (edgar or {}).get("pretaxB")
    fedtax = (edgar or {}).get("fedtaxB")

    reason = None
    edgar_ok = False
    etr = None

    if pretax is not None and fedtax is not None and pretax > 0:
        etr = fedtax / pretax * 100
        if pretax < MIN_PRETAX_B:
            reason = "domestic pretax too small to trust a rate — using ITEP"
        elif fedtax == 0:
            reason = "federal tax reported as exactly 0 — using ITEP instead of asserting a full gap"
        elif not (SANE_ETR_MIN <= etr <= SANE_ETR_MAX):
            reason = f"EDGAR rate {round(etr, 1)}% outside plausible range — using ITEP"
        else:
            edgar_ok = True

    if edgar_ok:
        rec["format"] = "refund" if etr < 0 else "full"
        rec["etr"] = round(etr, 2)
        rec["pretaxB"] = pretax
        rec["fedtaxB"] = fedtax
        rec["fyEnd"] = (edgar or {}).get("fyEnd")
        rec["gapDerived"] = False
        if rec["format"] == "full":
            rec["gapB"] = round(pretax * STATUTORY_RATE - fedtax, 2)
            comp = make_comparison(rec["gapB"])
            if comp:
                rec["comparison"] = comp
        else:
            rec["fedtaxRefund"] = True
        return rec, None

    # Fall back to ITEP effective rate (vetted, normalized).
    if itep_etr is None:
        rec["format"] = "rate_only"
        rec["etr"] = None
        return rec, reason
    rec["etr"] = itep_etr
    rec["format"] = "refund" if itep_etr < 0 else "rate_only"
    if itep_etr < 0:
        rec["fedtaxRefund"] = True
    # preserve ITEP-sourced breaks if present
    if company.get("breaks"):
        rec["breaks"] = company["breaks"]
    return rec, reason


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _fmt_money_b(b):
    """0.85 -> '$850M'; 17.58 -> '$17.6B'"""
    if b is None:
        return None
    if abs(b) < 1:
        return f"${round(b * 1000)}M"
    return f"${b:.1f}B"


def verdict_for(rec):
    """
    The locked verdict rules:
      refund (negative rate) -> loudest red
      < 30% of the federal rate -> red
      30-70%  -> yellow
      70%+    -> green
    Returns None when there's no rate to judge.
    """
    etr = rec.get("etr")
    if etr is None:
        return None
    if etr < 0:
        return "refund"
    ratio = etr / (STATUTORY_RATE * 100)
    if ratio < 0.30:
        return "high"
    if ratio < 0.70:
        return "medium"
    return "low"


def _fraction_label(etr):
    ratio = etr / (STATUTORY_RATE * 100)
    if ratio < 0.10:
        return "Less than a tenth"
    if ratio < 0.25:
        return "Less than a quarter"
    if ratio < 0.50:
        return "Less than half"
    if ratio < 0.75:
        return "About two-thirds"
    return None  # green says its own thing


def build_companies_json(companies, domains, config):
    """
    Emit the extension's companies.json: keyed by domain, display strings only.
    The popup renders; it never computes. Verdict decides what we say AND how
    loudly, per the locked rules.
    """
    out = {}
    for domain, name in domains.items():
        rec = companies.get(name)
        if not rec:
            continue
        verdict = verdict_for(rec)
        if verdict is None:
            continue  # no trustworthy rate -> site reads as no-record

        etr = rec["etr"]
        # Label the tax year by the calendar year the fiscal year mostly covered.
        # Walmart's FY ends 2026-01-31 but ran Feb 2025 -> Jan 2026: that's tax
        # year 2025. Naming it 2026 would be wrong and would read as a year that
        # has barely happened.
        fy = rec.get("fyEnd") or ""
        year = "2024"
        if len(fy) >= 7:
            y, mo = int(fy[:4]), int(fy[5:7])
            year = str(y - 1 if mo <= 6 else y)

        entry = {
            "domain": domain,
            "name": name,
            "verdict": verdict,
            "taxYearLabel": f"TAX YEAR {year}",
            "ratePaid": f"{etr:.1f}%" if etr >= 0 else f"\u2212{abs(etr):.1f}%",
            "federalRate": "21.0%",
            "ratePaidNum": max(etr, 0),
            "federalRateNum": 21.0,
            "actionUrl": "https://getuntethered.ai/act?c=" +
                         re.sub(r"[^a-z0-9]", "", name.lower()),
        }

        if verdict == "refund":
            entry["refundNote"] = (
                f"A negative rate: {name} reported a net federal tax benefit — "
                "it received money back rather than paying in."
            )
        else:
            frac = _fraction_label(etr)
            if frac:
                entry["rateFractionLabel"] = frac

        # Green deliberately suppresses the dollar figure even when one exists:
        # labeling it "AVOIDED TAXES" would contradict "paid close to the full rate".
        if verdict in ("high", "medium") and rec.get("gapB"):
            entry["avoidedTaxes"] = _fmt_money_b(rec["gapB"])
            comp = rec.get("comparison") or {}
            if comp.get("line"):
                entry["programComparison"] = comp["line"]

        if rec.get("buybacksB"):
            entry["buybacks"] = _fmt_money_b(rec["buybacksB"])

        # CEO pay intentionally absent — see extract_ceo_pay().
        out[domain] = entry

    with open("companies.json", "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
    return out


def write_files(companies, domains, unresolved, no_data):
    meta = {
        "generated": datetime.utcnow().strftime("%Y-%m-%d"),
        "statutory_rate": STATUTORY_RATE,
        "source_edgar": "SEC EDGAR XBRL companyfacts (data.sec.gov)",
        "source_itep": "ITEP Corporate Tax Avoidance tracker (fallback effective rates)",
        "programs": {k: {"cost_b": v["cost_b"], "detail": v["detail"]} for k, v in PROGRAMS.items()},
        "counts": {"companies": len(companies), "unresolved": len(unresolved), "no_edgar_data": len(no_data)},
    }
    panel = {"meta": meta, "companies": companies}
    with open("extraction-panel-data.json", "w") as f:
        json.dump(panel, f, indent=2)

    js = ["// AUTO-GENERATED by refresh_data.py — DO NOT HAND-EDIT",
          f"// Generated: {meta['generated']} | {len(companies)} companies | {len(domains)} domains",
          "",
          "const EXTRACTION_DATA = {",
          "  domains: " + json.dumps(domains, indent=2).replace("\n", "\n  ") + ",",
          "  companies: " + json.dumps(companies, indent=2).replace("\n", "\n  "),
          "};",
          "",
          "if (typeof self !== 'undefined') { self.EXTRACTION_DATA = EXTRACTION_DATA; }",
          ""]
    with open("extraction-data.js", "w") as f:
        f.write("\n".join(js))


# ---------------------------------------------------------------------------
# CEO / worker pay — intentionally NOT implemented yet. Read this.
# ---------------------------------------------------------------------------

def extract_ceo_pay(cik):
    """
    NOT WIRED UP ON PURPOSE.

    CEO pay is now XBRL-tagged (SEC pay-versus-performance rule), but it lives in
    the DEF 14A proxy's inline XBRL — a DIFFERENT place from the companyfacts feed
    this script uses for tax + buybacks. Median-worker pay isn't tagged at all.

    We are deliberately not shipping a guessed implementation, because this data is
    about NAMED REAL PEOPLE. A wrong CEO-pay figure isn't a rounding error — it's a
    false factual claim about a person, which is exactly what this product must never
    do. So CEO pay gets built and VERIFIED against known filings as its own focused
    step (likely via the `edgartools` library reading DEF 14A), not bolted on blind.

    Until then: buybacks alone already rebuild "WHERE THE MONEY WENT" with a real,
    sourced number.
    """
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    config = json.load(open("companies_config.json"))
    items = list(config["companies"].items())
    if args.limit:
        items = items[:args.limit]

    print(f"Resolving CIKs from the SEC ticker file...")
    by_ticker, by_name = build_cik_index()

    # First pass: resolve every CIK, then quarantine any CIK claimed by 2+ companies
    # (a duplicate almost always means a wrong name-match).
    resolved, pinned = {}, {}
    for name, company in items:
        cik, is_pinned = resolve_cik(name, company.get("ticker"), by_ticker, by_name)
        resolved[name], pinned[name] = cik, is_pinned
    seen = {}
    for name, cik in resolved.items():
        if cik is not None:
            seen.setdefault(cik, []).append(name)
    # When two companies claim the same SEC ID, an explicitly pinned ticker is
    # trusted and only the fuzzy name-match gets benched.
    collided = set()
    for cik, names in seen.items():
        if len(names) > 1:
            if any(pinned[n] for n in names):
                collided.update(n for n in names if not pinned[n])
            else:
                collided.update(names)
    if collided:
        print(f"Quarantined {len(collided)} companies that matched the same SEC ID "
              f"(kept on ITEP data): {', '.join(sorted(collided))}")

    companies_out = {}
    domains_out = {}
    unresolved, no_data, review = [], [], []

    for i, (name, company) in enumerate(items, 1):
        cik = None if name in collided else resolved[name]
        if not cik:
            unresolved.append(name)
            # keep the company using its existing ITEP data so nothing disappears
            rec, _ = classify(company, None)
            fr = final_check(rec)
            if fr:
                review.append(f"{name}: {fr}")
            companies_out[name] = rec
            for d in company.get("domains", []):
                domains_out[d] = name
            if args.verbose:
                print(f"  [{i}/{len(items)}] {name:<30} UNRESOLVED (kept ITEP data)")
            continue

        edgar = extract_edgar(cik)
        time.sleep(REQUEST_SLEEP)
        if edgar is None:
            no_data.append(name)
        rec, reason = classify(company, edgar)
        final_reason = final_check(rec)
        companies_out[name] = rec
        if final_reason:
            review.append(f"{name}: {final_reason}")
        elif reason:
            review.append(f"{name}: {reason}")
        for d in company.get("domains", []):
            domains_out[d] = name

        if args.verbose:
            bb = rec.get("buybacksB")
            print(f"  [{i}/{len(items)}] {name:<30} CIK {cik:<8} "
                  f"fmt={rec.get('format'):<9} etr={rec.get('etr')} buybacks={bb}")

    write_files(companies_out, domains_out, unresolved, no_data)
    shipped = build_companies_json(companies_out, domains_out, config)
    from collections import Counter
    vc = Counter(e["verdict"] for e in shipped.values())
    print(f"\ncompanies.json  : {len(shipped)} sites live "
          f"(red {vc['high']}, yellow {vc['medium']}, green {vc['low']}, refund {vc['refund']})")

    print("\n--- DONE ---")
    print(f"Companies written : {len(companies_out)}")
    print(f"Domains written   : {len(domains_out)}")
    print(f"Buybacks found    : {sum(1 for c in companies_out.values() if c.get('buybacksB') is not None)}")
    if unresolved:
        print(f"\nUNRESOLVED ({len(unresolved)}) — add a ticker to TICKER_OVERRIDES for each:")
        for n in unresolved:
            print(f"  - {n}")
    if no_data:
        print(f"\nNo EDGAR data ({len(no_data)}) — kept on ITEP fallback:")
        for n in no_data:
            print(f"  - {n}")
    if review:
        print(f"\nREVIEW ({len(review)}) — unusual numbers worth an eyeball:")
        for n in review:
            print(f"  - {n}")


if __name__ == "__main__":
    main()
