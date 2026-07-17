# /act — the "Contact your rep" page

Serves at getuntethered.ai/act — the page the extension's CTA button opens.

## Put this whole `act` folder at the root of the getuntethered.ai repo.

Result:
  getuntethered.ai/index.html   <- your existing landing page, untouched
  getuntethered.ai/act/         <- this page

The extension links to /act?c=<slug>. The slug is the company name lowercased
with non-letters stripped (amazoncom, walmart). The page matches it against
companies.json itself — nothing to maintain by hand. With no ?c=, it still
works and shows a general letter.

## Files (keep them together — the page fetches its data from this same folder)

| File | What it is |
|---|---|
| `index.html` | The page. No build step, no framework. |
| `reps-data.json` | Every current member of Congress + ZIP→district lookup. |
| `companies.json` | Same file the extension uses. The page reads the company's figures from it. |
| `build_reps.py` | Regenerates reps-data.json from free public-domain sources. |

## companies.json now lives in TWO places

The extension folder and this folder. The quarterly refresh has to update both,
or the receipt and the letter will quote different numbers.

## Refreshing

Run `python3 build_reps.py` on the same quarterly schedule as refresh_data.py.
Members resign and get replaced between elections; districts change after
redistricting.

## What this page does NOT do, and why

**It doesn't send the message.** Congress does not accept email from third
parties — every member requires their own web form, with a captcha and an
address field, specifically to verify you're a constituent. Promising one-click
send would mean promising something that silently fails. So: copy the letter,
open their form, paste. The page says so plainly.

**ZIP can't always pin down a House district.** ~22% of ZIPs straddle two or
more. For those the page asks which district you're in rather than guessing.
Senators are always exact — they're statewide.

## Sources

- Members: unitedstates/congress-legislators (public domain)
- ZIP→district: OpenSourceActivismTech/us-zipcodes-congress, from 2020 Census
  blocks, 119th Congress
- Google's Civic Information "Representatives" API (the usual answer) was shut
  down April 30, 2025. That's why we ship the data ourselves.
