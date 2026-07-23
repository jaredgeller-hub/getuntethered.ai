#!/usr/bin/env python3
"""
Get Untethered — health check for the live data the extension depends on.

WHY THIS EXISTS
  The extension fetches companies.json from getuntethered.ai. If that file
  disappears, goes malformed, or shrinks, every extension silently falls back to
  the stale copy bundled at its last release — and nothing tells you. Users see
  old numbers, or "no public record" everywhere, and you find out from a
  complaint months later. This turns a silent failure into an email.

HOW IT REPORTS
  It exits non-zero on any problem. Run it as a Render Cron Job with failure
  notifications turned on, and Render emails you when it fails. No new service,
  no extra account.

WHAT IT CHECKS
  companies.json  — reachable, valid JSON, not truncated, entries well-formed,
                    known domains still present, verdicts within the locked set
  reps-data.json  — reachable and complete (the /act page is useless without it)

USAGE
  python3 health_check.py
"""

import json
import sys

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

BASE = "https://getuntethered.ai/act"
UA = {"User-Agent": "GetUntethered health check hello@getuntethered.ai"}

# A floor, not a target. We publish ~227 sites; if it ever drops below this,
# something upstream broke and we'd rather hear about it than ship it quietly.
MIN_SITES = 150

# Domains that should always be present. If these vanish, the mapping broke.
CANARY_DOMAINS = ["walmart.com", "amazon.com", "homedepot.com", "citi.com"]

REQUIRED_FIELDS = ["name", "domain", "verdict", "ratePaid", "federalRate", "actionUrl"]
VALID_VERDICTS = {"high", "medium", "low", "refund"}

problems = []


def fail(msg):
    problems.append(msg)
    print(f"  FAIL: {msg}")


def fetch(path):
    url = f"{BASE}/{path}"
    print(f"\nChecking {url}")
    try:
        r = requests.get(url, headers=UA, timeout=30)
    except Exception as e:
        fail(f"{path}: request failed — {e}")
        return None
    if r.status_code != 200:
        fail(f"{path}: HTTP {r.status_code}")
        return None
    try:
        return r.json()
    except Exception as e:
        fail(f"{path}: not valid JSON — {e}")
        return None


def check_companies():
    data = fetch("companies.json")
    if data is None:
        return
    if not isinstance(data, dict):
        fail("companies.json: expected an object keyed by domain")
        return

    n = len(data)
    print(f"  {n} sites")
    if n < MIN_SITES:
        fail(f"companies.json: only {n} sites (expected at least {MIN_SITES}) — "
             "looks truncated or partially built")

    for d in CANARY_DOMAINS:
        if d not in data:
            fail(f"companies.json: canary domain '{d}' is missing")

    bad_fields, bad_verdicts = [], []
    for domain, entry in data.items():
        if not isinstance(entry, dict):
            bad_fields.append(domain)
            continue
        if any(not entry.get(f) for f in REQUIRED_FIELDS):
            bad_fields.append(domain)
        if entry.get("verdict") not in VALID_VERDICTS:
            bad_verdicts.append(f"{domain}={entry.get('verdict')}")

    if bad_fields:
        fail(f"companies.json: {len(bad_fields)} entries missing required fields "
             f"(e.g. {', '.join(bad_fields[:5])})")
    if bad_verdicts:
        fail(f"companies.json: {len(bad_verdicts)} entries have an unknown verdict "
             f"(e.g. {', '.join(bad_verdicts[:5])})")

    if not problems:
        from collections import Counter
        c = Counter(e.get("verdict") for e in data.values())
        print(f"  verdicts: red {c['high']}, yellow {c['medium']}, "
              f"green {c['low']}, refund {c['refund']}")


def check_reps():
    data = fetch("reps-data.json")
    if data is None:
        return
    for key in ("zips", "house", "senate"):
        if key not in data:
            fail(f"reps-data.json: missing '{key}'")
            return
    z, h, s = len(data["zips"]), len(data["house"]), len(data["senate"])
    print(f"  {z} ZIPs, {h} House seats, {s} states with senators")
    # The House has 435 voting seats plus delegates; the Senate covers 50 states.
    # Well below these means the build was cut short.
    if z < 30000:
        fail(f"reps-data.json: only {z} ZIPs (expected ~33,000)")
    if h < 400:
        fail(f"reps-data.json: only {h} House seats (expected ~435)")
    if s < 50:
        fail(f"reps-data.json: only {s} states with senators (expected 50)")


def main():
    print("Get Untethered — health check")
    check_companies()
    check_reps()

    print()
    if problems:
        print(f"--- {len(problems)} PROBLEM(S) ---")
        for p in problems:
            print(f"  - {p}")
        # Non-zero exit is the signal Render turns into a failure notification.
        sys.exit(1)

    print("--- ALL GOOD ---")
    sys.exit(0)


if __name__ == "__main__":
    main()
