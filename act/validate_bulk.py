#!/usr/bin/env python3
"""
GetUntethered — bulk-archive validation harness (stage 0).

WHAT THIS PROVES
  The plan is to stop making one companyfacts HTTP request per company and instead
  read every company out of the SEC's nightly bulk archive (companyfacts.zip). That
  swap is only safe if the archive produces byte-identical numbers to the live API.
  This harness is that proof — and nothing else. It does NOT change the pipeline.

HOW
  For every company in companies_config.json it resolves the CIK with the pipeline's
  own resolve_cik(), then extracts the figures TWICE:

    A  (live)     extract_edgar(cik)                  — one HTTP request to data.sec.gov
    B  (archive)  extract_from_facts(bulk.get(cik))   — same parser, fed from the zip

  Both paths run the *identical* parse (extract_from_facts), so any difference is a
  difference between the two SOURCES, never between two implementations. It then
  diffs the four fields the pipeline actually consumes — pretaxB, fedtaxB,
  buybacksB, fyEnd — and tallies every company into one of three buckets.

RESULTS
  identical   A and B agree on all four fields.
  mismatched  A and B disagree. Reported with the exact fields and values.
  absent      the CIK resolved but the archive has no member for it.

  (Companies whose CIK doesn't resolve at all are counted separately as
   UNRESOLVED and skipped — there's nothing to compare.)

EXIT CODE
  0   PASS  — every resolved company identical, none absent. The swap is a pure
             source change; the pipeline can move onto the archive without
             re-verifying any downstream logic.
  1   MISMATCH — at least one company disagreed. STOP. The archive rebuilds nightly,
             so a filing that landed TODAY shows up in the live API before it's in
             the archive; re-run tomorrow before assuming anything worse.
  2   ABSENT-only — no mismatches, but some CIKs aren't in the archive. Resolve
             which and why before switching.

USAGE
  pip install requests
  # point at an already-downloaded archive (or let it download one):
  python3 validate_bulk.py --archive companyfacts.zip
  python3 validate_bulk.py --archive companyfacts.zip --limit 25   # a quick 25 first
  python3 validate_bulk.py --download                              # fetch, then validate

NOTE ON WHERE IT RUNS
  Like refresh_data.py, this needs real network access to data.sec.gov AND
  www.sec.gov. It cannot run inside a sandbox walled off from the SEC.
"""

import os
import sys
import time
import json
import argparse

from bulk_reader import download_archive, CompanyFactsArchive, ARCHIVE_URL

# Reuse the pipeline's own resolution + extraction so the harness tests the real
# code paths, not a reimplementation of them.
from refresh_data import (
    build_cik_index,
    resolve_cik,
    extract_edgar,
    extract_from_facts,
    REQUEST_SLEEP,
)

# The four fields the pipeline consumes downstream. If these agree, classify() and
# everything after it behave identically regardless of source.
COMPARE_FIELDS = ("pretaxB", "fedtaxB", "buybacksB", "fyEnd")

DEFAULT_ARCHIVE = "companyfacts.zip"


def diff_fields(a, b):
    """Return a list of (field, a_value, b_value) for every COMPARE_FIELDS key on
    which A and B disagree. Empty list means identical.

    Either side may be None (a live 404, or empty facts) — None vs a dict is itself
    a disagreement worth surfacing, so we normalize a missing dict to all-None
    fields rather than skipping the comparison."""
    a = a or {}
    b = b or {}
    out = []
    for f in COMPARE_FIELDS:
        if a.get(f) != b.get(f):
            out.append((f, a.get(f), b.get(f)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=DEFAULT_ARCHIVE,
                    help=f"path to companyfacts.zip (default: ./{DEFAULT_ARCHIVE})")
    ap.add_argument("--download", action="store_true",
                    help="download the archive to --archive first (overwrites)")
    ap.add_argument("--limit", type=int, default=None,
                    help="only validate the first N companies (e.g. --limit 25)")
    ap.add_argument("--verbose", action="store_true",
                    help="print every company as it's compared")
    args = ap.parse_args()

    # Load the archive (download it first if asked, or if it isn't there yet).
    if args.download or not os.path.exists(args.archive):
        if not args.download and not os.path.exists(args.archive):
            print(f"No archive at {args.archive} — downloading it first.")
        download_archive(args.archive, url=ARCHIVE_URL)
    else:
        print(f"Using existing archive: {args.archive} "
              f"({os.path.getsize(args.archive)} bytes)")

    archive = CompanyFactsArchive(args.archive)
    print(f"Archive indexes {len(archive)} companies.\n")

    config = json.load(open("companies_config.json"))
    items = list(config["companies"].items())
    if args.limit:
        items = items[:args.limit]

    print("Resolving CIKs from the SEC ticker file...")
    by_ticker, by_name = build_cik_index()

    identical, mismatched, absent, unresolved = [], [], [], []

    for i, (name, company) in enumerate(items, 1):
        cik, _pinned = resolve_cik(name, company.get("ticker"), by_ticker, by_name)
        if not cik:
            unresolved.append(name)
            if args.verbose:
                print(f"  [{i}/{len(items)}] {name:<30} UNRESOLVED (skipped)")
            continue

        # B first: it's free (no HTTP). If the CIK isn't in the archive there's
        # nothing to compare against, so record it absent and skip the live call.
        b_facts = archive.get(cik)
        if b_facts is None:
            absent.append((name, cik))
            if args.verbose:
                print(f"  [{i}/{len(items)}] {name:<30} CIK {cik:<8} ABSENT from archive")
            continue

        b = extract_from_facts(b_facts)

        # A: the live HTTP path. Sleep to stay well under the SEC's ~10 req/sec.
        a = extract_edgar(cik)
        time.sleep(REQUEST_SLEEP)

        diffs = diff_fields(a, b)
        if diffs:
            mismatched.append((name, cik, diffs))
            if args.verbose:
                print(f"  [{i}/{len(items)}] {name:<30} CIK {cik:<8} MISMATCH")
        else:
            identical.append((name, cik))
            if args.verbose:
                print(f"  [{i}/{len(items)}] {name:<30} CIK {cik:<8} identical")

    # -----------------------------------------------------------------------
    # Report
    # -----------------------------------------------------------------------
    total_compared = len(identical) + len(mismatched)
    print("\n" + "=" * 70)
    print("VALIDATION RESULT")
    print("=" * 70)
    print(f"  identical  : {len(identical)}")
    print(f"  mismatched : {len(mismatched)}")
    print(f"  absent     : {len(absent)}  (CIK resolved, not in archive)")
    print(f"  ----")
    print(f"  compared   : {total_compared}")
    print(f"  unresolved : {len(unresolved)}  (no CIK — skipped, nothing to compare)")

    if mismatched:
        print(f"\nMISMATCHES ({len(mismatched)}) — A=live API, B=archive:")
        for name, cik, diffs in mismatched:
            print(f"  - {name} (CIK {cik}):")
            for field, av, bv in diffs:
                print(f"      {field}: A={av!r}  B={bv!r}")
        print("\n  The archive rebuilds nightly, so a filing that landed TODAY appears")
        print("  in the live API (A) before the archive (B). Re-run tomorrow before")
        print("  assuming a deeper problem.")

    if absent:
        print(f"\nABSENT FROM ARCHIVE ({len(absent)}) — resolve before switching:")
        for name, cik in absent:
            print(f"  - {name} (CIK {cik})")

    if unresolved:
        print(f"\nUNRESOLVED ({len(unresolved)}) — no CIK, not part of the comparison:")
        for name in unresolved:
            print(f"  - {name}")

    # -----------------------------------------------------------------------
    # Verdict + exit code
    # -----------------------------------------------------------------------
    print("\n" + "=" * 70)
    if mismatched:
        print("VERDICT: MISMATCH — do NOT switch the pipeline. See fields above.")
        sys.exit(1)
    if absent:
        print("VERDICT: ABSENT — no mismatches, but some CIKs aren't in the archive.")
        sys.exit(2)
    print("VERDICT: PASS — every compared company is identical. Safe to switch source.")
    sys.exit(0)


if __name__ == "__main__":
    main()
