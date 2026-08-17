#!/usr/bin/env python3
"""
GetUntethered — quarterly company-status check ("demotion check").

WHY THIS EXISTS
  companies_config.json is a hand-built list. Companies leave it the way
  companies always leave lists: they get acquired, taken private, or delisted,
  and nobody notices for a year. Until now the only way to catch that was for a
  human to read the refresh_data.py run log, spot an UNRESOLVED line, and go
  check EDGAR by hand. That doesn't scale and it doesn't happen on schedule.

  This script does that check automatically, on every company, against EDGAR.

WHAT IT DOES NOT DO
  It does not delete anything. Ever. It writes a flagged list and stops.
  Removing a company is a one-time human confirm (--apply, below), because the
  cost of a wrong removal is asymmetric: a company wrongly kept shows a slightly
  stale number for a quarter, while a company wrongly removed silently vanishes
  from the extension and nobody finds out. When a signal is ambiguous, this
  script flags rather than concludes — "show nothing" is the safe fallback, and
  a flag that a human dismisses in ten seconds is cheap.

------------------------------------------------------------------------------
THE RULES, AND WHY THEY'RE WEIGHTED THIS WAY
------------------------------------------------------------------------------

  CONTINUED 10-K FILING IS THE QUALIFYING SIGNAL. Not the ticker, not the
  exchange listing, not the absence of a Form 25 or Form 15. If a company is
  still filing annual reports with the SEC, it still has the XBRL data this
  project is built on, and it stays.

  Concretely:

  1. NO 10-K IN THE TRAILING ~18 MONTHS  ->  REMOVE_CANDIDATE
     This is the actual disqualifying signal, and the only one that proposes a
     removal. A company that has stopped filing annual reports has stopped
     producing the data this project reads; whatever the reason (merged,
     deregistered, gone dark), there is nothing left to show. 18 months, not 12,
     because a 10-K lands a few months after fiscal year end and fiscal years
     aren't all calendar years — 12 months would flag healthy filers every
     spring.

  2. FORM 25 (delisting) ON FILE      ->  REVIEW, never an automatic removal
  3. FORM 15 (deregistration) ON FILE ->  REVIEW, never an automatic removal

     THIS IS THE IMPORTANT ONE, and it is deliberately weaker than it looks.
     Both forms are routinely filed by companies that are entirely fine:

       - A Form 25 is filed on an exchange TRANSFER. American Electric Power
         filed a 25-NSE on 2023-08-14 moving from NYSE to Nasdaq. AEP is one of
         the largest utilities in the country and files 10-Ks on time. A naive
         "Form 25 -> remove" rule deletes it.
       - A Form 15 deregisters a class of securities, not the company. A company
         with public debt keeps its Exchange Act reporting obligation and keeps
         filing 10-Ks afterward. Continental Resources filed a 25-NSE in
         2022-11 and a 15-12G in 2023-01, went private — and filed a 10-K on
         2026-02-23 and a 10-Q on 2026-07-31 anyway. A naive rule deletes it.

     So a Form 25 or 15 is treated as a REASON TO LOOK, not a verdict. What
     resolves the look is whether a 10-K or 10-Q was filed AFTER the form:

       - Filed one since  ->  the signal is ANSWERED. Not flagged at all,
         recorded under "resolved_signals" in the JSON. This is the common case
         by a wide margin: on the first full run, 84 of 257 companies had a
         Form 25 or 15 somewhere in their history and every one was still
         actively filing — 3M (2023), AT&T (2026), Affiliated Managers Group
         (2017). Large companies delist bond and preferred series routinely. A
         quarterly report that flags a third of the list is one nobody reads,
         and an ignored report is the manual-verification problem it was
         supposed to solve.
       - Filed nothing since  ->  REVIEW, high priority, sorted to the top.
         Consistent with a company that really has left, but still NOT an
         automatic removal. Rule 1 remains the only thing that proposes one.

     Sealed Air and Coterra Energy both sat in this second group when they were
     verified by hand: gone, confirmed, but with 10-Ks recent enough that rule 1
     would not have fired for another ~18 months. That gap is intentional. The
     high-priority review is what surfaces them early; the human confirm is what
     removes them.

  4. DOMAIN FOOTER NO LONGER NAMES THE COMPANY  ->  REVIEW
     Catches the case EDGAR can't: the filer is unchanged but the consumer-facing
     brand was absorbed, redirected, or retired. Fetch-only, never conclusive —
     any fetch failure, bot wall, or JS-rendered footer produces NO flag rather
     than a false one. Off by default (--check-domains) since it's slow and the
     noisiest of the four.

  Two guards keep rule 1 honest:
    - Still filing 10-Qs but no 10-K in 18 months downgrades REMOVE_CANDIDATE to
      REVIEW. That combination is nearly impossible for a real departure, so it
      reads as a data anomaly, and an anomaly is not grounds for a removal.

      It found one on the first run, and it wasn't a departure at all: Exxon
      Mobil reorganized into a holding company (Form 8-K12B, 2026-07-01). Ticker
      XOM now points at the NEW CIK (2115436), which has filed a 10-Q but no
      10-K, so it carries no annual tax history — while the full history sits on
      the predecessor CIK (34088). resolve_cik() succeeds, finds no annual data,
      and falls back to ITEP without reporting anything as UNRESOLVED. A
      silently stale company that the existing run log had no way to surface.
      That's why this guard names succession explicitly when it sees an 8-K12B.
    - A company whose CIK doesn't resolve can't be checked at all, so it is
      reported as UNCHECKED. Absence of evidence is not a removal signal.

USAGE
  python3 demotion_check.py                  # dry run, all companies (default)
  python3 demotion_check.py --check-domains  # also run the footer check
  python3 demotion_check.py --limit 20 --verbose
  python3 demotion_check.py --apply          # interactive per-company confirm

  Writes demotion-flags.json either way. --apply is the ONLY mode that can edit
  companies_config.json, and it asks about each company one at a time.

RATE LIMITS
  One submissions.json request per company (~257 today), sleeping between calls,
  well under the SEC's ~10/sec allowance. Unlike refresh_data.py this isn't worth
  a 1.3 GB bulk download: it runs quarterly and reads a few fields per company.
"""

import re
import sys
import json
import time
import html
import argparse
import collections
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

from refresh_data import (
    HEADERS,
    build_cik_index,
    resolve_cik,
    normalize_name,
)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
REQUEST_SLEEP = 0.15

# How stale an annual report has to get before it counts as "stopped filing".
# See rule 1 above for why this is 18 and not 12.
TENK_STALE_DAYS = 548  # ~18 months

# Forms that count as an annual report. 10-K405 and 10-KT are historical/
# transition variants; 10-K/A is an amendment, which implies an original was
# filed, so it counts as evidence the company is still reporting.
TENK_FORMS_PREFIX = "10-K"
TENQ_FORMS_PREFIX = "10-Q"

# Delisting and deregistration forms. Grouped, but NOT treated as removals —
# see the rules block above.
FORM_25 = {"25", "25-NSE"}
FORM_15 = {"15-12B", "15-12G", "15F-12B", "15F-12G", "15F-15D", "15-15D"}

# Verdicts, most severe first.
REMOVE_CANDIDATE = "REMOVE_CANDIDATE"
REVIEW = "REVIEW"
UNCHECKED = "UNCHECKED"
OK = "OK"
SEVERITY = {REMOVE_CANDIDATE: 0, REVIEW: 1, UNCHECKED: 2, OK: 3}
# Ordering within a verdict, so the reviews most likely to be real departures
# sort above the ones that are almost certainly noise.
PRIORITY = {"high": 0, "normal": 1, "low": 2}


# ---------------------------------------------------------------------------
# EDGAR
# ---------------------------------------------------------------------------

def fetch_submissions(cik, tries=3):
    """EDGAR submissions.json for a CIK, or None if it can't be fetched."""
    url = SUBMISSIONS_URL.format(cik=cik)
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            time.sleep(1.0 + attempt)
        except requests.RequestException:
            time.sleep(1.0 + attempt)
    return None


EDGAR_COMPANY_SEARCH = "https://www.sec.gov/cgi-bin/browse-edgar"


def _name_matches(config_name, candidate_name):
    """True if an EDGAR entity name is the company we're looking for.

    Anchored at the start rather than a bare substring test: "SEALED AIR CORP/DE"
    normalizes to "SEALED AIR /DE" and should match "Sealed Air", but a loose
    `in` test is how you end up silently checking the wrong company — the exact
    failure mode resolve_cik() dropped its fuzzy fallback to avoid.
    """
    needle = normalize_name(config_name)
    cand = normalize_name(candidate_name or "")
    if not needle or not cand:
        return False
    return cand == needle or cand.startswith(needle + " ")


def lookup_cik_by_name(name):
    """Find a CIK on EDGAR by company name. Returns (cik, note) or (None, note).

    WHY THIS EXISTS: company_tickers.json only lists companies with a live
    exchange-listed ticker. The moment a company is delisted its ticker leaves
    that file, so resolve_cik() returns None — meaning the companies MOST likely
    to need demoting are precisely the ones the ticker path can no longer find.
    Without this fallback the checker reports them UNCHECKED forever and never
    reaches the EDGAR evidence that would confirm they're gone. Verified against
    Sealed Air and Coterra Energy, both of which are unreachable by ticker.

    Every candidate is confirmed against its own submissions.json before being
    accepted, because a name search WILL return near-miss companies.
    """
    try:
        r = requests.get(
            EDGAR_COMPANY_SEARCH,
            params={"company": name, "action": "getcompany", "type": "10-K",
                    "output": "atom", "count": "10"},
            headers=HEADERS, timeout=30,
        )
        if r.status_code != 200:
            return None, f"EDGAR name search returned {r.status_code}"
        body = r.text
    except requests.RequestException as e:
        return None, f"EDGAR name search failed ({e.__class__.__name__})"

    candidates = []
    seen = set()
    for m in re.finditer(r"<cik>\s*(\d+)\s*</cik>", body):
        cik = int(m.group(1))
        if cik not in seen:
            seen.add(cik)
            candidates.append(cik)
    if not candidates:
        return None, "EDGAR name search found no company"

    confirmed = []
    for cik in candidates[:5]:
        time.sleep(REQUEST_SLEEP)
        subs = fetch_submissions(cik)
        if not subs:
            continue
        names = [subs.get("name")] + [f.get("name") for f in subs.get("formerNames", [])]
        if any(_name_matches(name, n) for n in names):
            history = filing_history(subs)
            last = history[0][0] if history else ""
            confirmed.append((last, cik, subs.get("name")))

    if not confirmed:
        return None, f"EDGAR name search found {len(candidates)} company(s), none matching the name"

    # Most recently active wins. This is what separates a live filer from a
    # long-dead namesake — e.g. "Continental Resources, Inc" (filing through
    # 2026) from "Continental Resources Group, Inc." (dark since 2013).
    confirmed.sort(reverse=True)
    last, cik, sec_name = confirmed[0]
    note = f"resolved by EDGAR name search -> CIK {cik} ({sec_name})"
    if len(confirmed) > 1:
        note += f"; {len(confirmed)} name matches, picked the most recently active"
    return cik, note


def filing_history(subs):
    """[(date, form)] newest-first from a submissions payload.

    Only the "recent" block is read. It holds roughly the last 1,000 filings,
    which covers an 18-month window many times over for an active filer — and
    for a company that STOPPED filing, its last filings are still the most
    recent ones in that block. Neither case needs the paginated archive.
    """
    recent = (subs or {}).get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    return list(zip(dates, forms))


def last_filing_of(history, prefix):
    """Date of the newest filing whose form starts with prefix, or None."""
    for date, form in history:
        if form.startswith(prefix):
            return date, form
    return None, None


def periodic_filing_after(history, datestr):
    """Newest (date, form) 10-K/10-Q filed strictly after datestr, or None.

    This is what separates "deregistered and left" from "deregistered and kept
    reporting". A company that files an annual or quarterly report AFTER its own
    Form 25/15 still has a live reporting obligation — an exchange transfer, or
    public debt outliving the equity registration. One that files nothing
    periodic afterward has, in all likelihood, actually gone.

    Deliberately ignores 8-K, SC 13G, and Form 4: those keep trickling in from
    third parties and residual obligations for months after a merger closes, so
    counting them would erase the distinction this function exists to draw.
    """
    hits = [
        (d, f) for d, f in history
        if d > datestr and (f.startswith(TENK_FORMS_PREFIX) or f.startswith(TENQ_FORMS_PREFIX))
    ]
    return max(hits) if hits else None


def days_since(datestr, today):
    try:
        return (today - datetime.strptime(datestr, "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Rule 4: domain footer
# ---------------------------------------------------------------------------

def footer_text(domain):
    """Last chunk of visible text from a domain's homepage, or None.

    None means "couldn't check" and must never produce a flag.
    """
    for scheme in ("https://", "http://"):
        try:
            r = requests.get(scheme + domain, headers=HEADERS, timeout=15,
                             allow_redirects=True)
            if r.status_code != 200 or not r.text:
                continue
            text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text)
            text = re.sub(r"<[^>]+>", " ", text)
            text = html.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) < 200:
                # Almost certainly a JS shell or a bot wall, not a real page.
                return None
            return text[-3000:]
        except requests.RequestException:
            continue
    return None


def footer_names_company(name, domain):
    """(matched, detail). matched=None means the check couldn't run."""
    text = footer_text(domain)
    if text is None:
        return None, "page not fetchable (no flag raised)"
    haystack = normalize_name(text)
    needle = normalize_name(name)
    if not needle:
        return None, "company name normalizes to nothing"
    if needle in haystack:
        return True, "footer names the company"
    # A distinctive leading token is enough — "Alphabet" for "Alphabet Inc.",
    # and it survives the brand/legal-entity mismatch that trips exact matching.
    head = needle.split(" ")[0]
    if len(head) >= 4 and head in haystack:
        return True, f"footer names '{head.title()}'"
    return False, f"footer text does not mention '{name}'"


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def check_company(name, config_entry, by_ticker, by_name, today, check_domains):
    """Evaluate one company. Returns a result dict; never mutates anything."""
    result = {
        "company": name,
        "verdict": OK,
        "priority": "normal",
        "cik": None,
        "reasons": [],
        "evidence": {},
    }

    cik, _pinned = resolve_cik(name, config_entry.get("ticker"), by_ticker, by_name)
    result["evidence"]["cik_source"] = "ticker file"
    if not cik:
        # The ticker path failing is itself informative — it usually means the
        # ticker is no longer listed. Fall back to searching EDGAR by name so the
        # check can still run; a delisted company is exactly the case that needs
        # checking most, and it is exactly the case the ticker file cannot serve.
        cik, note = lookup_cik_by_name(name)
        result["evidence"]["cik_source"] = note
        if not cik:
            result["verdict"] = UNCHECKED
            result["reasons"].append(
                f"no CIK — cannot check EDGAR ({note}). Not a removal signal; "
                f"pin it in CIK_OVERRIDES in refresh_data.py if you know it."
            )
            return result
        result["reasons"].append(
            f"not in company_tickers.json (no live listed ticker); {note}"
        )

    result["cik"] = cik
    subs = fetch_submissions(cik)
    if subs is None:
        result["verdict"] = UNCHECKED
        result["reasons"].append(f"EDGAR submissions unavailable for CIK {cik}")
        return result

    result["evidence"]["sec_name"] = subs.get("name")
    result["evidence"]["tickers"] = subs.get("tickers") or []
    result["evidence"]["exchanges"] = subs.get("exchanges") or []

    history = filing_history(subs)
    tenk_date, tenk_form = last_filing_of(history, TENK_FORMS_PREFIX)
    tenq_date, _ = last_filing_of(history, TENQ_FORMS_PREFIX)
    result["evidence"]["last_10k"] = tenk_date
    result["evidence"]["last_10k_form"] = tenk_form
    result["evidence"]["last_10q"] = tenq_date

    # --- Rules 2 and 3: delisting / deregistration. Gathered as evidence here,
    # weighted below. On their own they never remove anything.
    f25 = [(d, f) for d, f in history if f in FORM_25]
    f15 = [(d, f) for d, f in history if f in FORM_15]
    result["evidence"]["form_25"] = f25[:3]
    result["evidence"]["form_15"] = f15[:3]

    # --- Rule 1: the disqualifying signal.
    stale = tenk_date is None
    age = days_since(tenk_date, today) if tenk_date else None
    if age is not None and age > TENK_STALE_DAYS:
        stale = True
    result["evidence"]["days_since_10k"] = age

    if stale:
        if tenk_date:
            result["reasons"].append(
                f"no 10-K in {age} days (last: {tenk_form} on {tenk_date}, "
                f"threshold {TENK_STALE_DAYS})"
            )
        else:
            result["reasons"].append("no 10-K on file at all in recent filings")

        # Guard: 10-Ks stopped but 10-Qs continue is an anomaly, not a departure.
        tenq_age = days_since(tenq_date, today) if tenq_date else None
        if tenq_age is not None and tenq_age <= TENK_STALE_DAYS:
            result["verdict"] = REVIEW
            result["reasons"].append(
                f"but still filing 10-Qs (last {tenq_date}) — anomalous, "
                f"downgraded to review rather than proposed for removal"
            )
            # The usual cause, and the one worth naming: a holding-company
            # reorganization. The 8-K12B is the successor registrant announcing
            # it has taken over the old one's registration. The ticker follows
            # the NEW CIK immediately, but the annual XBRL history stays behind
            # on the OLD one until the successor files its first 10-K — so the
            # pipeline resolves the ticker fine, finds no annual data, and
            # silently falls back to ITEP with nothing reported as UNRESOLVED.
            # That's a data-staleness bug that hides itself, hence the callout.
            succession = [(d, f) for d, f in history if f.startswith("8-K12B")]
            if succession or not tenk_date:
                result["priority"] = "high"
                note = (
                    f"filed {succession[0][1]} on {succession[0][0]} (successor "
                    f"registrant) — looks like a holding-company reorganization"
                    if succession else
                    "no 10-K on this CIK at all — looks like a successor entity"
                )
                result["reasons"].append(
                    f"{note}. The annual XBRL history is probably still on the "
                    f"PREDECESSOR CIK, so refresh_data.py is silently falling "
                    f"back to ITEP for this company. Find the predecessor CIK "
                    f"and pin it in CIK_OVERRIDES."
                )
        else:
            result["verdict"] = REMOVE_CANDIDATE
            # Corroboration, not cause.
            if f25:
                result["reasons"].append(
                    f"corroborated by Form {f25[0][1]} (delisting) on {f25[0][0]}")
            if f15:
                result["reasons"].append(
                    f"corroborated by Form {f15[0][1]} (deregistration) on {f15[0][0]}")
    else:
        # Filings are current. A 25 or 15 on file is now explicitly NOT a
        # removal — it's the AEP / Continental Resources case.
        if f25 or f15:
            result["verdict"] = REVIEW
            forms = ", ".join(f"{f} on {d}" for d, f in (f25 + f15)[:3])
            newest_exit = max(d for d, _ in (f25 + f15))
            resumed = periodic_filing_after(history, newest_exit)
            result["evidence"]["exit_form_date"] = newest_exit
            result["evidence"]["periodic_after_exit"] = resumed

            if resumed:
                # Filed a 10-K or 10-Q AFTER deregistering/delisting, so the
                # reporting obligation outlived the exit. AEP (exchange
                # transfer) and Continental Resources (public debt) both land
                # here — and so, it turns out, does a third of the list: big
                # companies routinely delist a bond or preferred series and file
                # a Form 25 for it. On the first full run this fired for 84 of
                # 257 companies, every one of them actively filing.
                #
                # So this is NOT flagged. A quarterly report that cries wolf
                # about a third of the list is one nobody reads, which is the
                # manual-verification burden this script exists to remove. The
                # signal is real but already ANSWERED — the company kept
                # reporting — so it's recorded as resolved evidence in the JSON
                # and kept out of the flagged list.
                result["verdict"] = OK
                result["priority"] = "low"
                result["resolved_signal"] = (
                    f"{forms} on file; superseded by {resumed[1]} on {resumed[0]}"
                )
            else:
                # Nothing periodic since the exit. Consistent with a company
                # that has actually left, but its last 10-K is still recent
                # enough that rule 1 hasn't fired, so this does NOT propose a
                # removal — it just sorts to the top of the review list. Rule 1
                # will catch it outright once the 10-K goes stale.
                result["priority"] = "high"
                result["reasons"].append(
                    f"has {forms} on file and has filed NO 10-K or 10-Q since — "
                    f"last was {tenk_form} on {tenk_date}, which predates the exit. "
                    f"Consistent with a company that has actually left. Still a "
                    f"review, not an automatic removal: confirm on EDGAR."
                )

    # --- Rule 4: domain footer.
    if check_domains:
        for domain in config_entry.get("domains") or []:
            matched, detail = footer_names_company(name, domain)
            result["evidence"].setdefault("domains", {})[domain] = detail
            if matched is False:
                if result["verdict"] == OK:
                    result["verdict"] = REVIEW
                result["reasons"].append(f"{domain}: {detail}")

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_report(results, path="demotion-flags.json"):
    flagged = [r for r in results if r["verdict"] != OK]
    flagged.sort(key=lambda r: (SEVERITY[r["verdict"]],
                                PRIORITY.get(r.get("priority"), 1),
                                r["company"]))
    payload = {
        "generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ"),
        "policy": {
            "qualifying_signal": "continued 10-K filing",
            "disqualifying_signal": f"no 10-K in {TENK_STALE_DAYS} days",
            "form_25_or_15": "flag for review, never an automatic removal",
            "on_uncertainty": "flag, do not conclude",
        },
        "counts": {
            "checked": len(results),
            "ok": sum(1 for r in results if r["verdict"] == OK),
            "review": sum(1 for r in results if r["verdict"] == REVIEW),
            "remove_candidates": sum(1 for r in results if r["verdict"] == REMOVE_CANDIDATE),
            "unchecked": sum(1 for r in results if r["verdict"] == UNCHECKED),
            "resolved_signals": sum(1 for r in results if r.get("resolved_signal")),
        },
        "flagged": flagged,
        # Companies that HAVE a Form 25/15 on file but kept filing periodic
        # reports afterward. Deliberately not flagged (see check_company), but
        # recorded so the report can still answer "did you look at this one?"
        "resolved_signals": {
            r["company"]: r["resolved_signal"]
            for r in sorted(results, key=lambda x: x["company"])
            if r.get("resolved_signal")
        },
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return payload


def print_report(payload):
    c = payload["counts"]
    print("\n" + "=" * 72)
    print("DEMOTION CHECK")
    print("=" * 72)
    print(f"  checked           : {c['checked']}")
    print(f"  ok                : {c['ok']}")
    print(f"  review            : {c['review']}")
    print(f"  remove candidates : {c['remove_candidates']}")
    print(f"  unchecked         : {c['unchecked']}")
    print(f"  resolved signals  : {c['resolved_signals']}  "
          f"(Form 25/15 on file but still filing — not flagged, "
          f"see resolved_signals in the JSON)")

    for verdict, header in (
        (REMOVE_CANDIDATE, "REMOVE CANDIDATES — no recent 10-K. Confirm each before removing."),
        (REVIEW, "REVIEW — a signal fired but filings continue. Usually keep."),
        (UNCHECKED, "UNCHECKED — could not be verified. Not a removal signal."),
    ):
        rows = [r for r in payload["flagged"] if r["verdict"] == verdict]
        if not rows:
            continue
        print(f"\n{header}")
        for r in rows:
            ev = r["evidence"]
            bits = []
            if ev.get("last_10k"):
                bits.append(f"last 10-K {ev['last_10k']}")
            if ev.get("tickers"):
                bits.append(f"ticker {'/'.join(ev['tickers'])}")
            suffix = f"  [{', '.join(bits)}]" if bits else ""
            tag = f"({r.get('priority')} priority) " if r.get("priority") in ("high", "low") else ""
            print(f"  - {tag}{r['company']} (CIK {r['cik']}){suffix}")
            for reason in r["reasons"]:
                print(f"      {reason}")


def apply_removals(payload, config_path="companies_config.json"):
    """Interactive, one company at a time. Only REMOVE_CANDIDATEs are offered."""
    candidates = [r for r in payload["flagged"] if r["verdict"] == REMOVE_CANDIDATE]
    if not candidates:
        print("\nNothing to apply — no removal candidates.")
        return
    if not sys.stdin.isatty():
        print("\n--apply needs an interactive terminal. Nothing changed.")
        return

    d = json.loads(open(config_path).read(), object_pairs_hook=collections.OrderedDict)
    removed = []
    print(f"\n{len(candidates)} removal candidate(s). Answer y/N for each.")
    for r in candidates:
        name = r["company"]
        if name not in d["companies"]:
            continue
        print(f"\n  {name} (CIK {r['cik']})")
        for reason in r["reasons"]:
            print(f"    {reason}")
        answer = input(f"  Remove '{name}' from {config_path}? [y/N] ").strip().lower()
        if answer == "y":
            d["companies"].pop(name)
            removed.append(name)
            print(f"    removed.")
        else:
            print(f"    kept.")

    if removed:
        # No trailing newline: companies_config.json is stored without one, and
        # adding it here would put a spurious one-line change at the bottom of
        # every diff this ever produces, on top of the real removal.
        with open(config_path, "w") as f:
            f.write(json.dumps(d, indent=2))
        print(f"\nRemoved {len(removed)}: {', '.join(removed)}")
        print(f"{len(d['companies'])} companies remain. Re-run refresh_data.py.")
    else:
        print("\nNothing removed.")


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Quarterly EDGAR-signal check for company status.")
    ap.add_argument("--limit", type=int, help="only check the first N companies")
    ap.add_argument("--only", action="append", help="check only this company (repeatable)")
    ap.add_argument("--verbose", action="store_true", help="print every company as it goes")
    ap.add_argument("--check-domains", action="store_true", help="also run the footer check (slow)")
    ap.add_argument("--apply", action="store_true", help="interactively confirm removals")
    ap.add_argument("--config", default="companies_config.json")
    ap.add_argument("--out", default="demotion-flags.json")
    args = ap.parse_args()

    config = json.load(open(args.config))
    companies = config["companies"]
    items = list(companies.items())
    if args.only:
        wanted = set(args.only)
        items = [(n, c) for n, c in items if n in wanted]
        missing = wanted - {n for n, _ in items}
        if missing:
            sys.exit(f"Not in {args.config}: {', '.join(sorted(missing))}")
    if args.limit:
        items = items[:args.limit]

    print(f"Checking {len(items)} companies against EDGAR "
          f"(10-K staleness threshold: {TENK_STALE_DAYS} days)")
    if args.check_domains:
        print("Domain footer check: ON")

    by_ticker, by_name = build_cik_index()
    today = datetime.utcnow()

    results = []
    for i, (name, entry) in enumerate(items, 1):
        r = check_company(name, entry, by_ticker, by_name, today, args.check_domains)
        results.append(r)
        if args.verbose or r["verdict"] != OK:
            print(f"  [{i}/{len(items)}] {name:<34} {r['verdict']}")
        time.sleep(REQUEST_SLEEP)

    payload = write_report(results, args.out)
    print_report(payload)
    print(f"\nWrote {args.out}")

    if args.apply:
        apply_removals(payload, args.config)
    else:
        print("Dry run — nothing was changed. Re-run with --apply to confirm removals.")


if __name__ == "__main__":
    main()
