#!/usr/bin/env python3
"""
GetUntethered — SEC bulk companyfacts archive reader.

WHY THIS EXISTS
  refresh_data.py fetches one companyfacts document per company over HTTP. At the
  259-company scale of the curated list that's fine. But the whole point of the
  next stage is to drop the curated list and run against the *full* SEC filer
  universe — thousands of companies — and at that scale per-company requests are
  both slow and exactly the crawl pattern the SEC's fair-access policy asks us to
  avoid:

    "Download only what you need and please moderate requests to minimize server
     load."  — sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data

  The SEC's own recommended alternative is the nightly bulk archive: ONE download
  of every filer's companyfacts, rebuilt each night. The JSON for each company
  inside the zip is byte-for-byte the same shape as the API response, so
  refresh_data.extract_from_facts() can consume it with zero changes. Proving that
  equivalence is what validate_bulk.py does; this module is the archive half of it.

WHAT IT DOES NOT DO
  It does not switch the pipeline over. That's a later, separate change, made only
  once validate_bulk.py reports PASS. This module just makes the archive readable.

USAGE (as a library)
    from bulk_reader import download_archive, CompanyFactsArchive
    download_archive("companyfacts.zip")          # one HTTP request, streamed
    arc = CompanyFactsArchive("companyfacts.zip")
    facts = arc.get(320193)                        # -> dict, or None if absent

USAGE (as a script — just fetch the archive and report its size)
    python3 bulk_reader.py                         # downloads to ./companyfacts.zip
    python3 bulk_reader.py --dest /data/cf.zip
"""

import os
import re
import sys
import json
import zipfile
import argparse

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# The SEC's nightly-rebuilt bulk archive of every filer's companyfacts. Note it
# lives under /Archives/, not data.sec.gov — a different host from the per-company
# API, but the same descriptive-User-Agent rule applies to every SEC request.
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"

# SEC requires a descriptive User-Agent with a contact email on EVERY request,
# archive downloads included. Kept identical to refresh_data.USER_AGENT on purpose.
USER_AGENT = "GetUntethered hello@getuntethered.ai"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}

# The archive is large (multiple GB), so we stream it to disk in chunks rather
# than buffering the whole body in memory.
CHUNK_BYTES = 1 << 20  # 1 MiB


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _human_bytes(n):
    """1234567890 -> '1.15 GB'. Purely for the log line."""
    if n is None:
        return "unknown"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024


def download_archive(dest, url=ARCHIVE_URL):
    """Stream the bulk companyfacts archive to `dest`.

    Streams in chunks (never holds the whole file in memory) and prints the
    server-reported Content-Length before downloading — that number is the whole
    reason to run this standalone: it tells us how much disk the Render job needs
    to provision, which we don't otherwise know until we've seen the real archive.

    Returns the number of bytes written.
    """
    with requests.get(url, headers=HEADERS, stream=True, timeout=120) as r:
        r.raise_for_status()

        content_length = r.headers.get("Content-Length")
        clen = int(content_length) if content_length is not None else None
        print(f"Archive URL     : {url}")
        print(f"Content-Length  : {content_length} ({_human_bytes(clen)})")
        print(f"Streaming to    : {dest}")

        written = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK_BYTES):
                if chunk:  # filter keep-alive chunks
                    f.write(chunk)
                    written += len(chunk)

    on_disk = os.path.getsize(dest)
    print(f"Wrote           : {written} bytes ({_human_bytes(written)})")
    # A short byte count almost always means a truncated download or an error page
    # served with a 200 — flag it rather than let the archive reader fail cryptically.
    if clen is not None and on_disk != clen:
        print(f"WARNING: on-disk size {on_disk} != Content-Length {clen} — "
              f"download may be incomplete.")
    return written


# ---------------------------------------------------------------------------
# Read individual companies out of the archive
# ---------------------------------------------------------------------------

class CompanyFactsArchive:
    """Random access to one company's facts inside companyfacts.zip.

    Opens the zip's central directory once (cheap — no data is decompressed) and
    decompresses exactly one member on each .get(), so we never extract the whole
    multi-GB archive to disk.

    Members are indexed by the DIGITS in each filename, not by assuming a fixed
    name like "CIK0000320193.json". If the SEC changes the zero-padding, drops the
    "CIK" prefix, or nests files under a directory, `int(<digits>)` still lands on
    the right entry. That robustness is deliberate: the archive layout is the SEC's
    to change, and a hard-coded format string would break silently on the next
    rebuild.
    """

    def __init__(self, path):
        self.path = path
        self._zip = zipfile.ZipFile(path)
        self._index = {}  # int(cik) -> zip member name
        for info in self._zip.infolist():
            if info.is_dir():
                continue
            # Match on the basename only, so a directory prefix that happens to
            # contain digits can't poison the CIK we key on.
            base = os.path.basename(info.filename)
            digits = re.sub(r"\D", "", base)
            if not digits:
                continue
            # Last writer wins if two members somehow map to the same CIK; in a
            # well-formed archive there's exactly one file per filer.
            self._index[int(digits)] = info.filename

    def __contains__(self, cik):
        return int(cik) in self._index

    def __len__(self):
        return len(self._index)

    def ciks(self):
        """The set of CIKs (as ints) present in the archive."""
        return set(self._index.keys())

    def get(self, cik):
        """Return the parsed companyfacts dict for `cik`, or None if the archive
        has no member for it. None on absence deliberately mirrors what
        refresh_data.fetch_json() returns on a 404, so downstream code — including
        extract_from_facts() — can't tell the API and the archive apart."""
        if cik is None:
            return None
        member = self._index.get(int(cik))
        if member is None:
            return None
        with self._zip.open(member) as fh:
            return json.load(fh)

    def close(self):
        self._zip.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# Script entry point — fetch the archive and report its size
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", default="companyfacts.zip",
                    help="where to stream the archive (default: ./companyfacts.zip)")
    args = ap.parse_args()

    download_archive(args.dest)

    # Report how many companies the archive actually indexes — a quick sanity
    # check that the zip opened and the digit-indexing found members.
    with CompanyFactsArchive(args.dest) as arc:
        print(f"Companies indexed: {len(arc)}")


if __name__ == "__main__":
    main()
