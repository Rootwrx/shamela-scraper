#!/usr/bin/env python3

import shutil
import sqlite3
import tempfile
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Live Firefox cookies database (psd)
COOKIE_DB = Path(
    "/run/user/1000/psd/hex0r-firefox-qoa4ljye.default-release/cookies.sqlite"
)

OUTPUT = Path.home() / "shamela" / "cf.txt"

LAST_VALUE = None


def get_cf_clearance():
    """Safely read cf_clearance from Firefox cookies."""
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


def save_cookie():
    global LAST_VALUE

    try:
        cookie = get_cf_clearance()

        if not cookie:
            return

        if cookie != LAST_VALUE:
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT.write_text(cookie)

            LAST_VALUE = cookie
            print("✓ cf_clearance updated")

    except Exception as e:
        print("Error:", e)


class CookieWatcher(FileSystemEventHandler):
    def on_modified(self, event):
        if Path(event.src_path).name == "cookies.sqlite":
            save_cookie()

    def on_created(self, event):
        if Path(event.src_path).name == "cookies.sqlite":
            save_cookie()

    def on_moved(self, event):
        if Path(event.dest_path).name == "cookies.sqlite":
            save_cookie()


if __name__ == "__main__":
    save_cookie()  # Initial write

    observer = Observer()
    observer.schedule(
        CookieWatcher(),
        str(COOKIE_DB.parent),
        recursive=False,
    )
    observer.start()

    print("Watching Firefox cookies...")

    try:
        observer.join()
    except KeyboardInterrupt:
        observer.stop()
        observer.join()
