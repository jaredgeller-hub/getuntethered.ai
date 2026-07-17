#!/usr/bin/env python3
"""
GetUntethered — build reps-data.json for the /act page.

Pulls two FREE, public-domain datasets and packs them into one file the page loads:

  1. unitedstates/congress-legislators  — every current member of Congress, with
     their office phone and official contact form. Public domain (CC0).
  2. OpenSourceActivismTech/us-zipcodes-congress — ZIP (ZCTA) to congressional
     district crosswalk, built from 2020 Census blocks for the 119th Congress.

WHY THIS EXISTS
  Google's Civic Information "Representatives" API — the thing everyone used for
  this — was turned down on April 30, 2025. There is no free hosted replacement,
  so we ship the data ourselves.

CADENCE
  Run it quarterly alongside refresh_data.py. Members resign, die, and get
  replaced between elections; districts change after redistricting.

USAGE
  pip install requests pyyaml
  python3 build_reps.py
"""

import csv
import io
import json
import os
import sys
from collections import defaultdict

try:
    import requests
    import yaml
except ImportError:
    sys.exit("Missing dependencies. Run:  pip install requests pyyaml")

LEGISLATORS_URL = ("https://raw.githubusercontent.com/unitedstates/"
                   "congress-legislators/main/legislators-current.yaml")
ZCCD_URL = ("https://raw.githubusercontent.com/OpenSourceActivismTech/"
            "us-zipcodes-congress/master/zccd.csv")

OUT = "reps-data.json"


def fetch(url):
    r = requests.get(url, timeout=60, headers={"User-Agent": "GetUntethered hello@getuntethered.ai"})
    r.raise_for_status()
    return r.text


def main():
    print("Downloading current members of Congress...")
    leg = yaml.safe_load(fetch(LEGISLATORS_URL))

    senate, house = defaultdict(list), {}
    for m in leg:
        t = m["terms"][-1]
        name = m["name"].get("official_full") or \
            f"{m['name'].get('first','')} {m['name'].get('last','')}".strip()
        rec = {
            "name": name,
            "party": t.get("party"),
            "phone": t.get("phone"),
            # The contact form is the only way to send a written message; fall
            # back to the member's site if a form isn't listed.
            "url": t.get("contact_form") or t.get("url"),
            "type": t.get("type"),
            "state": t.get("state"),
            "district": t.get("district"),
        }
        if t["type"] == "sen":
            senate[t["state"]].append(rec)
        elif t["type"] == "rep":
            house[f"{t['state']}-{t.get('district')}"] = rec

    print("Downloading ZIP-to-district crosswalk...")
    zips = defaultdict(list)
    for row in csv.DictReader(io.StringIO(fetch(ZCCD_URL))):
        try:
            z = row["zcta"].strip().zfill(5)
            key = f"{row['state_abbr']}-{int(row['cd'])}"
        except (KeyError, ValueError):
            continue
        if key not in zips[z]:
            zips[z].append(key)

    data = {"zips": dict(zips), "house": house, "senate": dict(senate)}
    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))

    no_form = [m["name"] for v in senate.values() for m in v if not m.get("url")]
    no_form += [m["name"] for m in house.values() if not m.get("url")]
    multi = sum(1 for v in zips.values() if len(v) > 1)

    print(f"\n--- DONE ---")
    print(f"Wrote {OUT}  ({round(os.path.getsize(OUT)/1024)} KB)")
    print(f"Senators : {sum(len(v) for v in senate.values())}")
    print(f"Reps     : {len(house)}")
    print(f"ZIPs     : {len(zips)}  ({multi} span more than one district — the page asks)")
    if no_form:
        print(f"No contact form ({len(no_form)}): {', '.join(no_form)}")


if __name__ == "__main__":
    main()
