#!/usr/bin/env python3
"""Packs what another machine needs to run some of procmon's projects: procmon itself (its committed code), each
named project folder, and a SQL dump of each named Postgres schema. Read by deploy/windows/setup.ps1.

    python3 procmon/deploy/make_bundle.py OUT.zip --project amtrakker --schema amtrakker

Never packed: .env files (secrets go over separately), virtualenvs, node_modules, git history, caches, logs, and
machine-bound data (a Chrome profile, a Mac-built chromedriver, debug screenshots)."""
import argparse
import fnmatch
import os
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))      # ~/Development
SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git", ".idea", "browser_profile", "chromedriver",
             "debug_screenshots", ".pytest_cache", ".claude"}
SKIP_FILES = [".env", ".env.*", "*.log", "*.pyc", ".DS_Store", "*.db", "*.sqlite*"]
PG = os.environ.get("PROCMON_BUNDLE_PG", "postgresql://admin@127.0.0.1:5433/postgres")


def add_tree(z, src, arc):
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if any(fnmatch.fnmatch(name, pat) for pat in SKIP_FILES):
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            z.write(path, os.path.join(arc, os.path.relpath(path, src)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--project", action="append", default=[], help="folder under ~/Development")
    ap.add_argument("--schema", action="append", default=[], help="Postgres schema to dump")
    a = ap.parse_args()
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        with tempfile.TemporaryDirectory() as tmp:
            # procmon as committed, so local edits in progress never ship by accident
            tar = os.path.join(tmp, "procmon.tar")
            subprocess.run(["git", "-C", os.path.join(ROOT, "procmon"), "archive", "-o", tar, "HEAD"], check=True)
            pdir = os.path.join(tmp, "procmon")
            os.makedirs(pdir)
            subprocess.run(["tar", "-xf", tar, "-C", pdir], check=True)
            add_tree(z, pdir, "projects/procmon")
        for name in a.project:
            src = os.path.join(ROOT, name)
            if not os.path.isdir(src):
                sys.exit(f"no such project folder: {src}")
            add_tree(z, src, f"projects/{os.path.basename(name)}")
        for schema in a.schema:
            dump = subprocess.run(["pg_dump", PG, "--schema", schema, "--no-owner", "--no-privileges"],
                                  check=True, capture_output=True).stdout
            z.writestr(f"db/{schema}.sql", dump)
    print(a.out, os.path.getsize(a.out), "bytes")


if __name__ == "__main__":
    main()
