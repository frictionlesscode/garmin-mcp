"""Cron entry point: sync GarminDB from Garmin Connect into local SQLite.
Runs GarminDB's CLI from its own isolated venv (../.garmindb-venv) so
GarminDB's pinned garminconnect version never touches the MCP server's
dependencies (garmindb requires a different garminconnect release than the
server is pinned to -- installing it into the same venv silently upgrades
and breaks the server's tested behavior).

Usage:  python scripts/sync_garmindb.py [config_dir] [--full]

config_dir defaults to data/garth -- the same directory the MCP server uses
for GARMIN_TOKEN_DIR, so GarminDB logs in with the existing token store
instead of needing its own copy or a plaintext password on disk.

Two sync modes, meant to run on separate schedules:

--latest (default): fast, incremental, only checks forward from the newest
date already in the local DB. Good for frequent runs (every 20 min).

--full: re-scans the whole configured window (currently ~180 days --
GarminConnectConfig.json's *_start_date fields) WITH --overwrite, forcing
GarminDB to re-download every day's file instead of skipping ones it already
has cached on disk. Both parts matter, confirmed live: --latest only ever
looks forward from the newest date already in the local DB, so a weigh-in
logged for a *past* date after that pointer has advanced is invisible to it
forever, not just delayed (BUG-1/BUG-2: 6 of 9 sequential log_weight calls
across different dates never appeared in get_body_trend, though every one
was correctly written to Garmin Connect). But re-scanning the window WITHOUT
--overwrite doesn't fix that either -- GarminDB caches each day's downloaded
JSON and skips re-fetching it by default, so a day that was checked *before*
a backfilled weigh-in existed keeps serving that stale empty cache forever,
even across repeated full resyncs. Confirmed by deleting one stale cached
file by hand: the very next sync fetched it correctly. --overwrite is what
makes a "full" resync actually re-fetch instead of re-verifying nothing.
Slower (~3 min at Garmin's ~1 req/sec pace, and every file is a fresh
request now, not just missing ones) -- run on a slower cadence (daily).
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_ROOT / ".garmindb-venv"
BIN_DIR = VENV_DIR / ("Scripts" if os.name == "nt" else "bin")
PYTHON = BIN_DIR / ("python.exe" if os.name == "nt" else "python")
GARMINDB_CLI = BIN_DIR / "garmindb_cli.py"
DEFAULT_CONFIG_DIR = REPO_ROOT / "data" / "garth"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config_dir", nargs="?", default=str(DEFAULT_CONFIG_DIR))
    parser.add_argument(
        "--full", action="store_true",
        help="Re-scan the whole configured window instead of only the latest data.",
    )
    args = parser.parse_args()

    if not PYTHON.exists():
        print(
            f"GarminDB venv not found at {VENV_DIR}.\n"
            "Set it up once with:\n"
            f"  python -m venv {VENV_DIR.name}\n"
            f"  {BIN_DIR.name}/pip install garmindb",
            file=sys.stderr,
        )
        sys.exit(2)

    cmd = [str(PYTHON), str(GARMINDB_CLI), "-f", args.config_dir, "--all", "--download", "--import", "--analyze"]
    if args.full:
        cmd.append("--overwrite")
    else:
        cmd.append("--latest")

    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
