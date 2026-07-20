#!/usr/bin/env bash
# Get Untethered — quarterly data refresh (runs on Render Cron)
#
# Regenerates both data files and commits them back to the repo. The site
# auto-deploys, and the extension fetches the fresh companies.json live — so no
# Chrome Web Store resubmission is needed for a data update.
#
# WHAT IT NEEDS (set as environment variables in the Render cron job):
#   GITHUB_TOKEN  — a GitHub token with permission to push to the repo
#   GIT_USER_NAME, GIT_USER_EMAIL — identity for the commit
#
# Both JSON files are written into act/ because that's what the site serves and
# what the extension fetches. There is no second copy to keep in sync anymore.

set -euo pipefail

REPO="jaredgeller-hub/getuntethered.ai"
WORKDIR="$(mktemp -d)"
echo "Working in $WORKDIR"

# 1. Clone the repo (shallow — we only need the latest state)
git clone --depth 1 "https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git" "$WORKDIR/repo"
cd "$WORKDIR/repo"

# 2. Run the two generators (they live in act/)
cd act
python3 refresh_data.py --verbose
python3 build_reps.py

# 3. Sanity check: refuse to ship a truncated file. A normal run has ~100+
#    live sites; if a network failure cut the run short, companies.json will be
#    far smaller, and shipping it would blank out the extension for everyone.
COUNT=$(python3 -c "import json; print(len(json.load(open('companies.json'))))")
echo "companies.json has $COUNT sites"
if [ "$COUNT" -lt 80 ]; then
  echo "ABORT: only $COUNT sites — looks like a failed/partial run. Not committing."
  exit 1
fi

# 4. Commit only if something actually changed
cd "$WORKDIR/repo"
git config user.name  "${GIT_USER_NAME:-GetUntethered Bot}"
git config user.email "${GIT_USER_EMAIL:-hello@getuntethered.ai}"
git add act/companies.json act/reps-data.json

if git diff --cached --quiet; then
  echo "No data changes this run — nothing to commit."
else
  git commit -m "Quarterly data refresh $(date +%Y-%m-%d)"
  git push origin HEAD:main
  echo "Pushed refreshed data."
fi

echo "Done."
