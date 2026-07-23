# Get Untethered — how this thing runs

Written so someone else (or you, a year from now) can pick this up cold.

Last updated: July 2026.

---

## What it is

A Chrome extension that shows how much federal income tax a large public company
actually paid, on that company's own website. The numbers come from SEC filings
and ITEP analysis. Each record links to a pre-written letter to the reader's own
members of Congress.

**The governing principle: flashlight, not receipt.** Show what's on the public
record. Never claim a company broke the law — they didn't; Congress wrote the
rules. Where a number can't be defended, show nothing rather than guess. This
principle is why the tool is credible, and it is enforced in code (see
"Guardrails" below), not just in intention.

---

## Accounts and where things live

| What | Where | Account |
|---|---|---|
| Source code + site + data | GitHub: `jaredgeller-hub/getuntethered.ai` (public) | GitHub |
| Hosting + cron jobs | Render | Render |
| Domain | Porkbun | Porkbun |
| Extension listing | Chrome Web Store | `untethered.dev@gmail.com` |
| Contact email | `hello@getuntethered.ai` → forwards to `untethered.dev@gmail.com` | Porkbun email forwarding |

Extension ID: `bmmecgckloocdhebkadhkhhcbhopcmbm`

The repo is public, so the code and data survive any account loss. What does not
survive is control of the domain, the Render services, and the Web Store
listing — those live behind the accounts above.

**Known single point of failure:** the Chrome Web Store account was suspended
once, which took the extension down for several days and blocked publishing
until an appeal cleared. Keep the recovery email current.

---

## Repository layout

```
getuntethered.ai/
├── index.html            landing page (getuntethered.ai)
├── refresh_all.sh        the quarterly cron entry point
└── act/
    ├── index.html        the "contact your rep" page (getuntethered.ai/act)
    ├── companies.json    OUTPUT — the data the extension fetches
    ├── reps-data.json    OUTPUT — every member of Congress + ZIP lookup
    ├── companies_config.json   INPUT — the 259-company list, domains, ITEP rates
    ├── refresh_data.py   builds companies.json from SEC + ITEP
    ├── build_reps.py     builds reps-data.json
    ├── health_check.py   daily check that the live data is intact
    └── requirements.txt
```

`companies.json` and `reps-data.json` are generated. Don't hand-edit them; edit
`companies_config.json` and re-run the cron.

---

## How data gets updated

1. A Render cron job (`getuntethered.ai`) runs quarterly:
   `0 9 1 1,4,7,10 *` — 9am on the 1st of Jan, Apr, Jul, Oct.
2. It runs `refresh_all.sh`, which clones the repo, runs `refresh_data.py` and
   `build_reps.py` inside `act/`, and commits the two output files back.
3. Render redeploys the static site.
4. The extension fetches `getuntethered.ai/act/companies.json` directly, caches
   it for 24 hours, and falls back to the copy bundled at its last release if
   the site is unreachable.

**Because the extension fetches rather than bundles, a data refresh does NOT
require a Chrome Web Store resubmission.** Only code changes do. This was the
whole point of v5.1.0 — don't undo it.

### To run it manually
Render → the `getuntethered.ai` cron job → **Trigger Run**. Note that pushing to
the repo triggers a *build*, not a *run* — those are different, and the build
log looks deceptively like success.

### Environment variables the cron needs
- `GITHUB_TOKEN` — fine-grained token, Contents: Read and write, this repo only
- `GIT_USER_NAME`, `GIT_USER_EMAIL`

---

## Monitoring

Two layers, both delivered by Render's failure notifications (turn these on in
each job's Settings → Notifications):

1. **The quarterly cron itself** — fails loudly if the SEC pull dies, the token
   expires, or the truncation guard fires.
2. **`getuntethered-health-check`** — a separate daily cron
   (`0 13 * * *`) running `python3 act/health_check.py`. It fetches the *live*
   files off the site and exits non-zero if `companies.json` is missing,
   malformed, truncated, missing canary domains, or has an unknown verdict
   value. This is the layer that matters, because it watches what users
   actually fetch rather than what the build produced.

---

## Guardrails (do not remove these)

These exist because each one caught a real error:

- **Implausible rates suppressed.** Any final rate beyond ±60% is dropped from
  every source, including ITEP, and the company shows "no public record."
  Caught a −630% rate that would otherwise have shipped.
- **CIK collision quarantine.** If two companies resolve to the same SEC
  identifier, both defer to ITEP unless one is pinned by ticker.
- **No loose name matching.** Strict matching only. A "contains" fallback used
  to match the wrong company silently.
- **Truncation guard.** `refresh_all.sh` refuses to commit if `companies.json`
  drops below 80 sites — a half-finished run would blank the extension for
  everyone.
- **Small-denominator check.** Companies whose domestic pretax income is too
  small to produce a trustworthy rate defer to ITEP.

The REVIEW list the cron prints is not an error list. It's the guardrails
reporting what they declined to publish, and why.

---

## The verdicts (locked)

By share of the 21% federal rate actually paid:

| Verdict | Threshold | Treatment |
|---|---|---|
| Red — HIGH TAX AVOIDANCE | under 30% of the rate | full CTA, dollar figure shown |
| Yellow — SOME TAX AVOIDANCE | 30–70% | calmer ask |
| Green — PAID CLOSE TO THE FULL RATE | 70–100% | quiet link, **no dollar figure** |
| Red — GOT MONEY BACK | negative rate | loudest treatment, empty bar |

Green deliberately suppresses the dollar figure, because "avoided taxes" would
contradict the verdict. Green existing at all is the evidence the tool
discriminates rather than lighting everything red.

---

## Annual maintenance: check for corporate drift

**This is the thing most likely to silently rot.** The cron refreshes the
numbers; nothing checks whether the companies still exist in the form recorded.

In one sweep in July 2026, this surfaced seven corporate events:

- `uscellular.com` was mapped to Telephone & Data Systems — but T-Mobile bought
  UScellular's wireless operations (Aug 2025). Removed.
- `angi.com` was mapped to IAC — but IAC spun Angi off (Mar 2025). Removed.
- ASGN renamed itself Everforth; ticker ASGN → EFOR (Apr 2026).
- IAC renamed itself People Incorporated; ticker IAC → PPLI (Jun 2026).
- Sealed Air taken private by CD&R, delisted (Apr 2026).
- Coterra merged into Devon, delisted (May 2026).
- Continental Resources — private since 2022.

Once a year, re-verify every domain in `companies_config.json` still belongs to
the company it's mapped to, hunting specifically for spin-offs, divestitures,
acquisitions and renames. A wrong mapping publishes the wrong company's tax data
on someone's website — the worst failure this product can have.

### Known pending, re-check when they close
- `websterbank.com` — Webster Financial being acquired by Banco Santander
- `dominionenergy.com` — NextEra acquisition, ~2027 close
- `amwater.com` — merging with Essential Utilities, but American Water survives,
  so this one should stay correct

### Permanently unresolved (correct, not a bug)
Sealed Air, Coterra Energy, Continental Resources. No longer file publicly.
They keep their last ITEP figures and have no domains mapped, so they don't
surface in the extension.

---

## Known limitations, stated on the site

- **The sample is biased.** The 259 companies came from published analyses of
  tax avoidance, which start by looking for low payers. So the mix skews red by
  construction. Individual records are each defensible; the aggregate is not
  representative of American business. This is disclosed in the Support section
  and should stay disclosed.
- **Coverage is a small slice of the web** — a few hundred domains.
- **No usage analytics at all**, by choice. Which means there's no data on
  whether anyone actually contacts Congress after seeing a record. That
  question is unanswered.

---

## If something breaks

**Extension shows "no public record" everywhere**
Open `getuntethered.ai/act/companies.json` in a browser. If it 404s, the site
didn't deploy — redeploy the static site in Render (it does not always deploy
automatically; this has bitten before). If the file is fine, it's the 24-hour
client cache; removing and reloading the extension clears it.

**Cron fails**
Check the Render log. Most likely: expired `GITHUB_TOKEN`, or SEC rate-limiting
the run. Re-trigger; the truncation guard means a partial run can't ship.

**Health check emails a failure**
It names the specific problem. Start by opening the live JSON file yourself.

**Need to change extension code**
Any code change means a new version number and a fresh Chrome Web Store review.
Data changes do not. Keep it that way.
