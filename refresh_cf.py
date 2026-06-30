#!/usr/bin/env python3
"""One-shot: extract cf_clearance from Firefox cookies and write to ~/shamela/cf.txt."""

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

COOKIE_DB = Path(
    "/run/user/1000/psd/hex0r-firefox-qoa4ljye.default-release/cookies.sqlite"
)
OUTPUT = Path.home() / "shamela" / "cf.txt"


def get_cf_clearance() -> str | None:
    """Safely read cf_clearance from Firefox cookies (copy DB first)."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        shutil.copy2(COOKIE_DB, tmp.name)
        conn = sqlite3.connect(tmp.name)
        row = conn.execute(
            """
            SELECT value
            FROM moz_cookies
            WHERE host='.shamela.ws'
              AND name='cf_clearance'
            ORDER BY lastAccessed DESC
            LIMIT 1
            """
        ).fetchone()
        conn.close()
    return row[0] if row else None


def main():
    cookie = get_cf_clearance()
    if not cookie:
        print("cf_clearance: not found in Firefox cookies", file=sys.stderr)
        sys.exit(1)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(cookie)
    print(f"cf_clearance written to {OUTPUT}")


if __name__ == "__main__":
    main()
