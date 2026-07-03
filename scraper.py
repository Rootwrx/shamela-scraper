#!/usr/bin/env python3
"""
shamela_scraper.py — Scrape book(s) from shamela.ws and export to structured PDF + DOCX + JSON.

Supports resumable downloads, parallel page-fetching, multi-volume books,
deeply-nested table-of-contents with lazy-loaded node expansion, Cloudflare
cookie auto-refresh, and a global manifest across runs.

Output tree (per book)
──────────────────────
    <out_dir>/<category>_<name>/<author_id>_<name>/<book_id>_<title>/
        book_<id>.pdf           (or book_<id>_<juz>.pdf for multi-volume books)
        book_<id>.docx          (only with --docx)
        book_<id>.json          (combined export — disabled by default for large books)
        meta.json               (title, author, publisher, TOC tree, volumes, ...)
        author_info.json        (biography + other books by same author)
        pages.jsonl             (raw scraped pages, append-only, source of truth)
        progress.json           (resume checkpoint for interrupted runs)
        pages_resolved.jsonl    (pages with TOC headings anchored inline)
    <out_dir>/manifest.json     (global cross-run tracking)

Output formats
──────────────
  • PDF  — A4, ornate gold/teal Islamic cover, WeasyPrint-rendered.
           Per-juz splitting for multi-volume books (default).
           Optional: --single_pdf, --columns 2 (two-column layout), --fast.
  • DOCX — Native Word .docx with outline navigation, per-level heading
           colors, hamesh footnotes, RTL page layout.  Requires python-docx.
           Optional: --single_docx, --also_pdf (build both formats).

Core features
─────────────
  • Resumable — pages.jsonl is the source of truth.  Re-run any command
    to resume interrupted books; finished books are skipped.
  • Parallel — configurable concurrent page fetches (--workers, default 4).
  • TOC — recursive tree parsing for deeply-nested indices; lazy-loaded
    [+] nodes expanded automatically via AJAX.
  • Volumes — multi-juz books split into per-juz PDFs/DOCX files by default,
    with heading numbering reset per volume.
  • Cloudflare — automatic cookie refresh via refresh_cf.py when a 403 or
    challenge page is detected.  Only one process runs the refresh at a time.
  • Headings — TOC entries anchored to inline body text; auto-detected
    bracket headings (e.g. [الباب الأول]) numbered as sub-headings.

Usage
─────
    # single book (legacy)
    python shamela_scraper.py --book_id 12762

    # several explicit books
    python shamela_scraper.py --book_ids 12762,667,151109

    # everything in one or more categories
    python shamela_scraper.py --category_ids 13,33,40

    # mix & match, custom output dir
    python shamela_scraper.py --category_ids 13 --book_ids 667 --out_dir ./library

    # re-run to resume incomplete books, skip finished ones
    python shamela_scraper.py --category_ids 13

    # force re-scrape even if marked done
    python shamela_scraper.py --book_id 12762 --force

    # only rebuild outputs from cached data (no network)
    python shamela_scraper.py --book_id 12762 --pdf_only

    # build DOCX instead of PDF
    python shamela_scraper.py --book_id 12762 --docx

    # build both DOCX and PDF
    python shamela_scraper.py --book_id 12762 --docx --also_pdf

    # faster concurrency (16 workers, shorter delay)
    python shamela_scraper.py --book_ids 12762 --workers 16 --delay 0.3

    # two-column newspaper-style PDF layout
    python shamela_scraper.py --book_id 12762 --columns 2

    # drop kashida justification for faster PDF rendering
    python shamela_scraper.py --book_id 12762 --fast

    # single combined PDF even for multi-volume books
    python shamela_scraper.py --book_id 12762 --single_pdf

    # single combined PDF without juz numbering in headings
    python shamela_scraper.py --book_id 12762 --single_pdf --no_juz_outline

    # specific volume only (1-based)
    python shamela_scraper.py --book_id 12762 --juz 3

    # single combined DOCX for multi-volume books (PDF untouched)
    python shamela_scraper.py --book_id 12762 --single_docx

    # per-juz PDFs + combined DOCX
    python shamela_scraper.py --book_id 12762 --also_pdf --single_docx

    # JSON only — no PDF/DOCX output
    python shamela_scraper.py --book_id 12762 --json_only

    # scrape only first 50 pages per book
    python shamela_scraper.py --category_ids 13 --limit 50

    # continue from a specific page ID
    python shamela_scraper.py --book_id 12762 --start_page 200

    # refresh cached category listings
    python shamela_scraper.py --category_ids 13 --refresh_categories

    # process at most 5 books (testing)
    python shamela_scraper.py --category_ids 13 --max_books 5

    # quick status summary
    python shamela_scraper.py --out_dir ./library --status
"""

import argparse
import datetime
import fcntl
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests

# ── Style configuration ────────────────────────────────────────────────
# Loaded from style_config.json (or custom path via --style_config).
# This global is populated once at startup; all PDF/DOCX styling reads
# from it.  Any missing key falls back to the DEFAULT_STYLE_CFG below.
STYLE_CFG: dict = {}

DEFAULT_STYLE_CFG = {
    "fonts": {"heading": "Scheherazade New", "body": "Scheherazade New"},
    "body_size_pdf": 14,
    "body_size_docx": 15,
    "palette": {
        "gold": "#c99200",
        "gold_light": "#e8b830",
        "gold_dark": "#6b4f10",
        "teal": "#256080",
        "rust": "#b8451a",
        "sepia": "#805830",
        "ink": "#1a1008",
        "cream": "#fefcf7",
        "parchment": "#fcf8ef",
        "brown": "#3e2712",
    },
    "shamela_span_colors": {
        "c1": "#cc3333",
        "c2": "#1a7a1a",
        "c3": "#8a8a20",
        "c4": "#909090",
        "c5": "#1a7aa0",
    },
    "heading_levels": [
        {"level": 0, "size_pdf": 18, "size_docx": 19, "color": "#e8b830", "shape": "۞"},
        {"level": 1, "size_pdf": 17, "size_docx": 18, "color": "#b8451a", "shape": "❖"},
        {"level": 2, "size_pdf": 17, "size_docx": 17, "color": "#256080", "shape": "◈"},
        {"level": 3, "size_pdf": 17, "size_docx": 16, "color": "#805830", "shape": "◆"},
        {"level": 4, "size_pdf": 17, "size_docx": 16, "color": "#2d7a4f", "shape": "●"},
        {"level": 5, "size_pdf": 17, "size_docx": 16, "color": "#7b2d8e", "shape": "★"},
        {"level": 6, "size_pdf": 17, "size_docx": 16, "color": "#c07a1a", "shape": "✦"},
        {"level": 7, "size_pdf": 17, "size_docx": 16, "color": "#c0392b", "shape": "◇"},
        {"level": 8, "size_pdf": 17, "size_docx": 16, "color": "#1e7a9e", "shape": "◎"},
        {"level": 9, "size_pdf": 17, "size_docx": 16, "color": "#c06080", "shape": "▪"},
    ],
    "delimiter_colors": {
        "brace": "#FF0000",
        "verse": "#FF0000",
    },
}


def load_style_config(path: str | None = None) -> dict:
    """Load style_config.json, merge with defaults, store in STYLE_CFG."""
    cfg = dict(DEFAULT_STYLE_CFG)
    if path and Path(path).exists():
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        _deep_merge(cfg, user)
    STYLE_CFG.clear()
    STYLE_CFG.update(cfg)

    # ── Sync module-level globals from config ────────────────────────
    global _DOCX_FONT_BODY, _DOCX_FONT_HEAD
    global _DOCX_GOLD, _DOCX_GOLD_L, _DOCX_GOLD_D, _DOCX_TEAL, _DOCX_RUST, _DOCX_SEPIA, _DOCX_INK
    global _DOCX_LEVEL_COLORS, _HEADING_SHAPES

    pal = cfg["palette"]
    _DOCX_GOLD = pal["gold"].lstrip("#").upper()
    _DOCX_GOLD_L = pal["gold_light"].lstrip("#").upper()
    _DOCX_GOLD_D = pal["gold_dark"].lstrip("#").upper()
    _DOCX_TEAL = pal["teal"].lstrip("#").upper()
    _DOCX_RUST = pal["rust"].lstrip("#").upper()
    _DOCX_SEPIA = pal["sepia"].lstrip("#").upper()
    _DOCX_INK = pal["ink"].lstrip("#").upper()

    fonts = cfg["fonts"]
    _DOCX_FONT_BODY = fonts["body"]
    _DOCX_FONT_HEAD = fonts["heading"]

    _HEADING_SHAPES = {hl["level"]: hl["shape"] for hl in cfg["heading_levels"]}
    _DOCX_LEVEL_COLORS = {
        hl["level"]: (hl["color"].lstrip("#").upper(), hl["size_docx"])
        for hl in cfg["heading_levels"]
    }

    return cfg


def _deep_merge(base: dict, overlay: dict) -> None:
    """Recursive dict merge — overlay values win at every level."""
    for k, v in overlay.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v

try:
    from weasyprint import HTML as WeasyprintHTML
except ImportError:  # pdf building simply unavailable until installed
    WeasyprintHTML = None

try:
    import docx as _docxlib
    from docx.shared import Pt, Cm, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
except ImportError:  # docx building simply unavailable until `pip install python-docx`
    _docxlib = None

try:
    import orjson as _orjson

    def _fast_json(data, indent: bool = True) -> str:
        opts = _orjson.OPT_APPEND_NEWLINE
        if indent:
            opts |= _orjson.OPT_INDENT_2
        return _orjson.dumps(data, option=opts).decode()
except ImportError:

    def _fast_json(data, indent: bool = True) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2 if indent else None)


# ─────────────────────────────────────────────────────────────────────────────
BASE = "https://shamela.ws"

# Must match the browser that generated cf_clearance.
# These are for Chrome 124 on Windows — adjust if you used a different browser.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.5",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "Connection": "keep-alive",
    "DNT": "1",
}

# ═══════════════════════════════════════════════════════════════════════════
# SMALL UTILITIES
# ═══════════════════════════════════════════════════════════════════════════

_ILLEGAL_CHARS = re.compile(
    r"[^\w\s\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF-]"
)


def sanitize_filename(name: str, max_len: int = 80, fallback: str = "unknown") -> str:
    """Make a string safe to use as a single path component (keeps Arabic)."""
    if not name:
        return fallback
    cleaned = _ILLEGAL_CHARS.sub("_", str(name)).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned


def atomic_write_json(path, data):
    """Write JSON to disk via a temp file + rename, so a crash never leaves
    a half-written / corrupted progress or manifest file."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_fast_json(data), encoding="utf-8")
    tmp.replace(path)


def load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class Manifest:
    """Global, cross-run tracking of every book/author/category processed."""

    def __init__(self, out_dir: Path):
        self.path = Path(out_dir) / "manifest.json"
        self.data = load_json(self.path, None) or {
            "books": {},
            "categories": {},
            "authors": {},
        }

    def save(self):
        atomic_write_json(self.path, self.data)

    def book(self, book_id) -> dict:
        return self.data["books"].setdefault(str(book_id), {})


# ═══════════════════════════════════════════════════════════════════════════
# SESSION
# ═══════════════════════════════════════════════════════════════════════════


def get_session(user_agent: str = None) -> requests.Session:
    s = requests.Session(use_thread_local_curl=True)

    if user_agent:
        ua_lower = user_agent.lower()
        if "firefox/" in ua_lower:
            s.impersonate = "firefox147"
        elif "chrome/" in ua_lower or "edg/" in ua_lower:
            s.impersonate = "chrome124"
        else:
            s.impersonate = "firefox147"
        s.headers.update({**HEADERS, "User-Agent": user_agent})
    else:
        s.impersonate = "firefox147"
        s.headers.update(HEADERS)

    return s


# ═══════════════════════════════════════════════════════════════════════════
# CLOUDFLARE COOKIE MANAGER
# ═══════════════════════════════════════════════════════════════════════════


class CloudflareCookieManager:
    """Transparent wrapper around a curl_cffi Session with auto CF-cookie refresh.

    Calling ``.get()`` (or ``.request()``) will:

    1. Load the current ``cf_clearance`` from ``cf.txt`` and inject it into
       the session cookies.
    2. Make the request.
    3. If the response indicates a Cloudflare failure (403, challenge page):
       a) Acquire a **cross-process file lock** so only one process runs the
          refresh helper.
       b) Run ``refresh_cf.py`` (sibling script) to copy the fresh cookie
          from Firefox's cookie store into ``~/shamela/cf.txt``.
       c) Reload the cookie from ``cf.txt``.
       d) Retry the request **exactly once**.
    4. Return the response.

    Duck-types as a :class:`curl_cffi.requests.Session`: exposes ``.get()``,
    ``.request()``, ``.cookies``, ``.headers``, ``.impersonate``.  Any
    attribute not explicitly defined falls through to the underlying session
    via :meth:`__getattr__`.
    """

    _LOCK_PATH = Path("/tmp/shamela_cf_refresh.lock")

    def __init__(self, session, cf_txt: str | Path | None = None):
        self._session = session
        self._cf_txt = (
            Path(cf_txt or Path.home() / "shamela" / "cf.txt").expanduser().resolve()
        )
        self._refresh_lock = threading.Lock()
        self._load_cookie()

    # ── cookie I/O ────────────────────────────────────────────────────

    def _load_cookie(self):
        """Read ``cf_txt`` and update the session cookie."""
        try:
            cf_value = self._cf_txt.read_text().strip()
        except FileNotFoundError:
            return
        if cf_value:
            self._session.cookies.set("cf_clearance", cf_value, domain="shamela.ws")

    # ── Cloudflare detection ──────────────────────────────────────────

    @staticmethod
    def _is_cf_failure(resp) -> bool:
        """Return ``True`` when *resp* looks like a Cloudflare block."""
        if resp.status_code == 403:
            return True
        if resp.status_code != 200:
            return False
        snippet = (resp.text or "")[:2000].lower()
        if "cf-browser-verification" in snippet:
            return True
        if "just a moment..." in snippet:
            return True
        return False

    # ── refresh orchestration ─────────────────────────────────────────

    def _refresh(self):
        """Run ``refresh_cf.py`` under a cross-process file lock.

        The lock ensures that when multiple workers/processes detect a
        Cloudflare failure simultaneously, only **one** of them actually
        runs the refresh helper.  The others wait until the lock is
        released, then check the mtime of ``cf.txt`` — if it was modified
        within the last 10 seconds they skip the script and just reload
        the cookie.
        """
        with self._refresh_lock:
            with open(self._LOCK_PATH, "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    # Has another process already refreshed cf.txt
                    # while we were waiting for the lock?
                    if self._cf_txt.exists():
                        age = time.time() - self._cf_txt.stat().st_mtime
                        if age < 10:
                            self._load_cookie()
                            return

                    script = Path(__file__).parent / "refresh_cf.py"
                    subprocess.run(
                        [sys.executable, str(script)],
                        check=True,
                        timeout=120,
                    )
                    self._load_cookie()
                except (subprocess.SubprocessError, FileNotFoundError) as exc:
                    raise RuntimeError(
                        f"Cloudflare cookie refresh failed: {exc}"
                    ) from exc
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    # ── public request API ────────────────────────────────────────────

    def request(self, method, url, **kwargs):
        """Make a request with transparent CF-cookie refresh + single retry."""
        self._load_cookie()
        resp = self._session.request(method, url, **kwargs)
        if self._is_cf_failure(resp):
            print("  [!] Cloudflare challenge detected — refreshing cookie ...")
            self._refresh()
            resp = self._session.request(method, url, **kwargs)
            if self._is_cf_failure(resp):
                raise RuntimeError(
                    "Cloudflare: request still failing after cookie refresh"
                )
        return resp

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    # ── duck-typing: look like a curl_cffi Session ────────────────────

    @property
    def cookies(self):
        return self._session.cookies

    @cookies.setter
    def cookies(self, value):
        self._session.cookies = value

    @property
    def headers(self):
        return self._session.headers

    @headers.setter
    def headers(self, value):
        self._session.headers = value

    @property
    def impersonate(self):
        return self._session.impersonate

    @impersonate.setter
    def impersonate(self, value):
        self._session.impersonate = value

    def __getattr__(self, name):
        """Delegate any unknown attribute to the underlying session."""
        return getattr(self._session, name)


# ═══════════════════════════════════════════════════════════════════════════
# TABLE OF CONTENTS — nested-tree parsing (handles deep / multi-level TOCs)
# ═══════════════════════════════════════════════════════════════════════════


def parse_toc_tree(ul_tag, level: int = 0) -> list[dict]:
    """
    Recursively parse a <ul> of TOC <li><a>...</a><ul>...nested...</ul></li>
    into a tree:  [{"label":..., "page_id":..., "level":..., "children":[...]}]
    Works for flat (single-level) TOCs and arbitrarily deep nested ones.
    """
    nodes = []
    if ul_tag is None:
        return nodes

    for li in ul_tag.find_all("li", recursive=False):
        # Find the actual title link, skipping the [+] expand button
        # which has class "exp_bu" and href="javascript:;"
        a = None
        exp_bu_tag = None
        for tag in li.find_all("a"):
            if "exp_bu" in tag.get("class", []):
                exp_bu_tag = tag
            elif tag.get("href"):
                if a is None:
                    a = tag
        node = {"label": None, "page_id": None, "level": level}
        if a:
            node["label"] = a.get_text(strip=True)
            m = re.search(r"/book/\d+/(\d+)", a.get("href", ""))
            if m:
                node["page_id"] = int(m.group(1))
        # Capture the lazy-load node id from the [+] button so fetch_toc_children
        # can later retrieve collapsed children via /node/<book_id>/<exp_bu_id>
        if exp_bu_tag:
            data_id = exp_bu_tag.get("data-id")
            if data_id:
                node["_exp_bu_id"] = data_id
        child_ul = li.find("ul", recursive=False)
        children = parse_toc_tree(child_ul, level + 1) if child_ul else []
        if children:
            node["children"] = children
        nodes.append(node)
    return nodes


def flatten_toc(tree: list[dict]) -> list[dict]:
    """Depth-first flat list, each entry keeping its original nesting `level`."""
    flat = []
    for node in tree:
        flat.append(
            {
                "label": node.get("label"),
                "page_id": node.get("page_id"),
                "level": node.get("level", 0),
            }
        )
        if node.get("children"):
            flat.extend(flatten_toc(node["children"]))
    return flat


def _toc_has_lazy_nodes(tree: list[dict]) -> bool:
    """Return True if any node in the TOC tree has a lazy-loaded [+] button."""
    for node in tree:
        if "_exp_bu_id" in node and not node.get("children"):
            return True
        if node.get("children") and _toc_has_lazy_nodes(node["children"]):
            return True
    return False


def fetch_toc_children(
    session: requests.Session, book_id: int, tree: list[dict], delay: float = 0.3
) -> None:
    """
    Walk the TOC tree and, for every node that has an ``_exp_bu_id`` but no
    children yet, fetch its children from Shamela's AJAX endpoint:

        GET https://shamela.ws/ajax/titlechilds/<book_id>/<data-id>

    The endpoint returns a bare ``<ul>`` HTML fragment identical to what the
    browser injects after the user clicks a [+] button.  We parse it with
    ``parse_toc_tree`` and attach the result as the node's ``children``.

    The ``_exp_bu_id`` internal key is stripped from every node before
    returning so it does not leak into the final JSON.

    Mutates *tree* in-place; returns nothing.
    """
    for node in tree:
        exp_id = node.pop(
            "_exp_bu_id", None
        )  # always clean up, even if no fetch needed

        has_children = bool(node.get("children"))

        if exp_id and not has_children:
            url = f"{BASE}/ajax/titlechilds/{book_id}/{exp_id}"
            try:
                resp = session.get(url, timeout=15)
                resp.raise_for_status()
                frag = BeautifulSoup(resp.text, "lxml")
                # The response is a bare HTML fragment; find the outermost <ul>
                ul = frag.find("ul")
                if ul:
                    children = parse_toc_tree(ul, level=node["level"] + 1)
                    if children:
                        node["children"] = children
                time.sleep(delay)
            except Exception as e:
                # Non-fatal: log and keep going with whatever we have
                print(
                    f"    [toc] warn: could not fetch node {exp_id} for book {book_id}: {e}"
                )

        # Recurse into already-present children (they may themselves have
        # _exp_bu_ids for grandchildren that were lazy-loaded)
        if node.get("children"):
            fetch_toc_children(session, book_id, node["children"], delay=delay)


def toc_summary_stats(tree: list[dict]) -> dict:
    flat = flatten_toc(tree)
    if not flat:
        return {"total_entries": 0, "top_level_entries": 0, "max_depth": 0}
    return {
        "total_entries": len(flat),
        "top_level_entries": len(tree),
        "max_depth": max(n["level"] for n in flat) + 1,
    }


# ═══════════════════════════════════════════════════════════════════════════
# BOOK METADATA
# ═══════════════════════════════════════════════════════════════════════════


def fetch_book_meta(session: requests.Session, book_id: int) -> dict:
    """Fetch book card (title, author, publisher, nested TOC, volume boundaries)."""
    url = f"{BASE}/book/{book_id}"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    meta = {"book_id": book_id, "url": url}

    # ── book card text ──────────────────────────────────────────────────
    nass = soup.find("div", class_="nass")
    if nass:
        raw = nass.get_text("\n", strip=True)
        for line in raw.splitlines():
            line = line.strip()
            parts = line.split(":", 1)
            if len(parts) < 2:
                continue
            label, value = parts[0].strip(), parts[1].strip()
            if label == "الكتاب":
                meta["title"] = value
            elif label == "المؤلف":
                meta["author"] = value
            elif label == "الناشر":
                meta["publisher"] = value
            elif label == "الطبعة":
                meta["edition"] = value
            elif label == "عدد الأجزاء":
                meta["volumes"] = value
            elif label == "عدد الصفحات":
                meta["pages"] = value
            elif label == "المحقق":
                meta["editor"] = value

    # ── category from breadcrumb ─────────────────────────────────────────
    cat_link = soup.select_one("ol.breadcrumb a[href*='/category/']")
    if cat_link:
        m = re.search(r"/category/(\d+)", cat_link["href"])
        if m:
            meta["category_id"] = int(m.group(1))
            meta["category_name"] = cat_link.get_text(strip=True)

    # ── author page link ────────────────────────────────────────────────
    author_link = soup.find("a", href=re.compile(r"/author/\d+"))
    if author_link:
        meta["author_url"] = (
            BASE + author_link["href"]
            if author_link["href"].startswith("/")
            else author_link["href"]
        )
        meta["author_id"] = re.search(r"/author/(\d+)", author_link["href"]).group(1)

    # ── TOC: parse from the book home page (fully expanded tree) ──────
    # CRITICAL INSIGHT: Shamela's [+] expand buttons are lazy-loaded.
    # On the book HOME page (/book/<id>), the "فهرس الموضوعات" section
    # renders the COMPLETE TOC tree with ALL [+] children already present
    # as nested <ul> elements in the server-rendered HTML.
    #
    # On CONTENT pages (/book/<id>/<page>), the "فصول الكتاب" s-nav sidebar
    # shows [+] entries but their child <ul>s are EMPTY — children are
    # injected by JavaScript when the user clicks [+].  The scraper never
    # clicks, so it gets an incomplete tree.  This caused sub-sections
    # (e.g. 4.6.1) to appear under the wrong parent (e.g. 4.5) because
    # the whole 4.6 branch was missing from the TOC.
    #
    # Fix: always extract the TOC from the home page using multiple fallback
    # selectors that cover the known Shamela HTML variants:
    #   1. div.betaka-index > ul        (classic layout)
    #   2. h4 "فهرس الموضوعات" → next ul (new layout)
    #   3. div with id containing "fihris" or "index"
    # Only fall back to the content-page s-nav when the home page yields
    # nothing useful (absent or bare page-number labels only).

    def _find_home_page_toc_ul(soup_obj):
        """Try all known selectors for the fully-expanded TOC on the home page."""
        # 1. Classic betaka-index div
        betaka = soup_obj.find("div", class_="betaka-index")
        if betaka:
            ul = betaka.find("ul", recursive=False)
            if ul:
                return ul

        # 2. New layout: h4 containing "فهرس" followed by a ul sibling
        for heading in soup_obj.find_all(["h3", "h4", "h5"]):
            if "فهرس" in heading.get_text():
                # Look for the next <ul> sibling (may be wrapped in a div)
                for sib in heading.next_siblings:
                    if not hasattr(sib, "name"):
                        continue
                    if sib.name == "ul":
                        return sib
                    if sib.name in ("div", "section"):
                        ul = sib.find("ul")
                        if ul:
                            return ul
                    if sib.name in ("h3", "h4", "h5"):
                        break  # hit next section header, stop

        # 3. Any div whose id or class suggests an index/fihris
        for div in soup_obj.find_all("div"):
            cls = " ".join(div.get("class", []))
            did = div.get("id", "")
            if any(
                k in cls.lower() or k in did.lower()
                for k in ("fihris", "index", "toc", "contents")
            ):
                ul = div.find("ul")
                if ul:
                    return ul

        return None

    top_ul = _find_home_page_toc_ul(soup)
    toc_tree = parse_toc_tree(top_ul)

    def _toc_looks_bare(tree: list[dict]) -> bool:
        """Return True if every label is just an Arabic numeral (bare page refs)."""
        flat = flatten_toc(tree)
        if not flat:
            return True
        bare = sum(
            1
            for n in flat
            if n.get("label")
            and re.fullmatch(r"[\u0660-\u0669٠-٩\d\s]+", n["label"].strip())
        )
        return bare >= len(flat) * 0.8  # 80 %+ bare → treat as bare

    if _toc_looks_bare(toc_tree):
        # Home page yielded nothing useful (absent or volume-selector only).
        # Fall back to s-nav on first content page — best-effort only;
        # [+] children will still be missing from expandable entries.
        if meta.get("volume_start_pages"):
            first_content_url = f"{BASE}/book/{book_id}/{meta['volume_start_pages'][0]}"
        else:
            first_content_url = f"{BASE}/book/{book_id}/1"
        try:
            r2 = session.get(first_content_url, timeout=15)
            r2.raise_for_status()
            soup2 = BeautifulSoup(r2.text, "lxml")
            snav = soup2.find("div", class_="s-nav")
            if snav:
                snav_ul = snav.find("ul", recursive=False)
                snav_tree = parse_toc_tree(snav_ul)
                if snav_tree and not _toc_looks_bare(snav_tree):
                    toc_tree = snav_tree
        except Exception:
            pass  # fall back to whatever we already have

    # Expand any lazy-loaded [+] nodes whose children weren't in the HTML
    # (this is a no-op for books where the home page already had all children,
    # and essential for books like Quran tafsirs with verse-level sub-entries)
    if _toc_has_lazy_nodes(toc_tree):
        print(f"    [toc] expanding lazy-loaded nodes for book {book_id} ...")
        fetch_toc_children(session, book_id, toc_tree)

    meta["toc"] = toc_tree
    meta["toc_flat"] = flatten_toc(toc_tree)
    meta["toc_summary"] = toc_summary_stats(toc_tree)

    # ── derive volume start page IDs + labels from volume dropdown ──────
    # The dropdown only exists on the full page HTML (/book/{id}/{pgnum}),
    # NOT on the home page (/book/{id}).  Try the home page first (it works
    # for some books), then fall back to fetching page 1.
    def _extract_volumes(html_soup) -> tuple[list[int], list[str]] | None:
        vol_links = html_soup.select("ul.dropdown-menu a[href*='/book/']")
        vp, vl, seen = [], [], set()
        for a in vol_links:
            m = re.search(r"/book/\d+/(\d+)", a["href"])
            if m:
                pid = int(m.group(1))
                if pid not in seen:
                    seen.add(pid)
                    vp.append(pid)
                    vl.append(a.get_text(strip=True))
        return (vp, vl) if vp else None

    vol_data = _extract_volumes(soup)
    if vol_data is None:
        # Home page didn't have the dropdown — fetch page 1's full HTML
        try:
            r3 = session.get(f"{BASE}/book/{book_id}/1", timeout=15)
            r3.raise_for_status()
            soup3 = BeautifulSoup(r3.text, "lxml")
            vol_data = _extract_volumes(soup3)
        except Exception:
            pass  # no volume info available; single PDF as before
    if vol_data is not None:
        meta["volume_start_pages"], meta["volume_labels"] = vol_data

    return meta


def fetch_author_info(session: requests.Session, author_id: int) -> dict:
    """Fetch author page: bio + books list."""
    url = f"{BASE}/author/{author_id}"
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        info = {"author_id": author_id, "url": url}

        # bio: find h4 "تعريف بالمؤلف" then grab the next div.alert sibling
        alert = None
        for h4 in soup.find_all("h4"):
            if "تعريف" in h4.get_text():
                for sib in h4.parent.next_siblings:
                    if hasattr(sib, "get") and "alert" in sib.get("class", []):
                        alert = sib
                        break
                break
        # fallback: last div.alert (avoids donation banners at top)
        if not alert:
            alerts = soup.find_all("div", class_="alert")
            if alerts:
                alert = alerts[-1]
        if alert:
            raw = alert.get_text("\n", strip=True)
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            if lines:
                info["full_name"] = lines[0]
            info["bio"] = raw

        # books list: each div.book_item
        books = []
        for item in soup.select("div.book_item"):
            book = {}
            a = item.find("a", href=True)
            if a:
                book["url"] = a["href"]
                m = re.search(r"/book/(\d+)", a["href"])
                if m:
                    book["book_id"] = int(m.group(1))
            title_span = item.find("span", class_="book_title")
            if title_span:
                book["title"] = title_span.get_text(strip=True)
            desc = item.find("p")
            if desc:
                for line in desc.get_text("\n", strip=True).splitlines():
                    for prefix, key in [
                        ("الكتاب:", "title"),
                        ("المؤلف:", "author"),
                        ("الناشر:", "publisher"),
                        ("الطبعة:", "edition"),
                        ("عدد الصفحات:", "pages"),
                        ("دراسة وتحقيق:", "editor"),
                    ]:
                        if line.strip().startswith(prefix):
                            book[key] = line.replace(prefix, "").strip()
            if book:
                books.append(book)
        if books:
            info["books"] = books

        return info
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# CATEGORY LISTING
# ═══════════════════════════════════════════════════════════════════════════


def fetch_category_books(
    session: requests.Session, category_id: int, delay: float = 0.5
) -> dict:
    """
    Fetch every book listed under a shamela.ws category page.
    Paginates defensively: keeps requesting ?page=N+1 while it keeps finding
    book ids it hasn't seen yet, stops as soon as a page contributes nothing
    new (covers both single-page categories and any paginated ones).
    """
    name = None
    seen = {}
    page = 1
    while True:
        url = f"{BASE}/category/{category_id}"
        params = {"page": page} if page > 1 else {}
        try:
            resp = session.get(url, params=params, timeout=20)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"\n[!] Error fetching category {category_id} (page {page}): {e}")
            break

        soup = BeautifulSoup(resp.text, "lxml")

        if name is None:
            h1 = soup.find("h1")
            if h1 and h1.get_text(strip=True):
                name = h1.get_text(strip=True)
            elif soup.title and soup.title.string:
                name = soup.title.string.split(" - ")[0].strip()

        items = soup.select("div.book_item")
        if not items:
            break

        new_count = 0
        for item in items:
            book_a = item.find("a", href=re.compile(r"/book/\d+"))
            if not book_a:
                continue
            m = re.search(r"/book/(\d+)", book_a["href"])
            if not m:
                continue
            book_id = int(m.group(1))
            if book_id in seen:
                continue

            book = {"book_id": book_id}
            title_span = item.find("span", class_="book_title")
            book["title"] = (
                title_span.get_text(strip=True)
                if title_span
                else book_a.get_text(strip=True)
            )

            author_a = item.find("a", href=re.compile(r"/author/\d+"))
            if author_a:
                am = re.search(r"/author/(\d+)", author_a["href"])
                if am:
                    book["author_id"] = int(am.group(1))
                book["author_name"] = author_a.get_text(strip=True).strip("[]").strip()

            seen[book_id] = book
            new_count += 1

        if new_count == 0:
            break

        page += 1
        if page > 200:  # sanity cap, avoids runaway loops on unexpected markup
            break
        time.sleep(delay)

    return {
        "category_id": category_id,
        "name": name or f"category_{category_id}",
        "url": f"{BASE}/category/{category_id}",
        "books": list(seen.values()),
    }


# ═══════════════════════════════════════════════════════════════════════════
# PAGE PARSING  (unchanged logic from original scraper)
# ═══════════════════════════════════════════════════════════════════════════


def _clean_inline(p_soup) -> str:
    """
    Return inner HTML of a <p> keeping only safe inline tags:
    span.cX (colored text) and <br>.
    Everything else is unwrapped to plain text.
    """
    COLORS = STYLE_CFG.get("shamela_span_colors", DEFAULT_STYLE_CFG["shamela_span_colors"])

    out = []
    for node in p_soup.children:
        if isinstance(node, str):
            out.append(_html_esc(str(node)))
        elif node.name == "br":
            out.append("<br>")
        elif node.name == "span":
            classes = node.get("class", [])
            color_class = next((c for c in classes if c in COLORS), None)
            if color_class:
                inner = node.get_text(" ", strip=False)
                out.append(
                    f'<span style="color:{COLORS[color_class]}">'
                    f"{_html_esc(inner)}</span>"
                )
            else:
                out.append(_html_esc(node.get_text(" ", strip=False)))
        else:
            out.append(_html_esc(node.get_text(" ", strip=False)))
    return "".join(out).strip()


def _html_esc(t: str) -> str:
    import html as _h

    return _h.escape(t)


def _strip_tags(html_str: str) -> str:
    """Plain text from an HTML fragment (for the flat .text field)."""
    return BeautifulSoup(html_str, "lxml").get_text(" ", strip=True)


def parse_page_html(html: str) -> list[dict]:
    """
    Extract page entries from a shamela page HTML blob.
    Each paragraph is {"type": "text"|"hamesh", "lines": [html_fragment, ...]}
    Lines are HTML fragments preserving span.cX color and <br> splits.
    """
    soup = BeautifulSoup(html, "lxml")
    results = []

    for div in soup.find_all("div", {"data-page-id": True}):
        pid = div.get("data-page-id", "")
        pnum = div.get("data-page-num", "")
        paragraphs = []

        for p in div.find_all("p"):
            classes = p.get("class", [])
            is_hamesh = "hamesh" in classes or "footnote" in classes
            is_heading = "b" in classes or "head" in classes or "title" in classes
            inner_html = _clean_inline(p)

            if not inner_html.strip():
                continue

            # Skip paragraphs that only contain "..." (ellipsis placeholders)
            plain = _strip_tags(inner_html).strip()
            if re.fullmatch(r"\.+", plain):
                continue

            if is_hamesh:
                lines = [
                    frag.strip()
                    for frag in re.split(r"<br\s*/?>", inner_html)
                    if frag.strip()
                ]
                if lines:
                    paragraphs.append({"type": "hamesh", "lines": lines})
            elif is_heading:
                paragraphs.append({"type": "heading", "lines": [inner_html]})
            else:
                paragraphs.append({"type": "text", "lines": [inner_html]})

        if paragraphs:
            results.append(
                {
                    "page_id": int(pid) if pid else None,
                    "page_num": int(pnum) if pnum else None,
                    "paragraphs": paragraphs,
                    # flat plain text for RAG / backward compat
                    "text": "\n\n".join(
                        "\n".join(_strip_tags(l) for l in p["lines"])
                        for p in paragraphs
                    ),
                }
            )

    # fallback
    if not results:
        nass = soup.find("div", class_="nass")
        if nass:
            for div in nass.find_all("div", recursive=False):
                t = div.get_text(" ", strip=True)
                if len(t) > 20:
                    results.append(
                        {
                            "page_id": None,
                            "page_num": None,
                            "paragraphs": [{"type": "text", "lines": [_html_esc(t)]}],
                            "text": t,
                        }
                    )

    return results


def get_next_page_id(html: str) -> int | None:
    """Extract next page ID from Ajax wrapper div or the 'load next' button."""
    soup = BeautifulSoup(html, "lxml")
    # Ajax response: data-next-id on the wrapper div[data-page-id]
    div = soup.find("div", {"data-page-id": True})
    if div and div.get("data-next-id"):
        nid = div["data-next-id"].strip()
        if nid:
            return int(nid)
    # Fallback: old full-page HTML with bu_load_next button
    btn = soup.find("button", {"id": "bu_load_next"})
    if btn and btn.get("data-next-id"):
        return int(btn["data-next-id"])
    return None


def _find_last_page_id(session: requests.Session, book_id: int) -> int | None:
    """Fetch page 1's full HTML and return the last page ID from the `>>` button."""
    try:
        url = f"{BASE}/book/{book_id}/1"
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        last_id = None
        pat = rf"/book/{book_id}/(\d+)"
        for a in soup.find_all("a", href=re.compile(pat)):
            m = re.search(pat, a["href"])
            if m:
                pid = int(m.group(1))
                if last_id is None or pid > last_id:
                    last_id = pid
        return last_id
    except Exception as e:
        print(f"    [warn] could not determine last page for book {book_id}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# RESUMABLE PAGE SCRAPING
# ═══════════════════════════════════════════════════════════════════════════


def load_pages_jsonl(pages_path: Path) -> list[dict]:
    """Load all pages into a list (used for JSON export only — not for PDF)."""
    pages = []
    if pages_path.exists():
        with open(pages_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    pages.append(json.loads(line))
    return pages


def iter_pages_jsonl(pages_path: Path):
    """Yield pages one at a time from a .jsonl file — O(1) memory per page."""
    if pages_path.exists():
        with open(pages_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)


def _merge_intro_volumes(
    vol_start_pages: list[int], vol_labels: list[str]
) -> tuple[list[int], list[str]]:
    """Merge consecutive intro+content volume pairs into single volumes.

    An "intro" volume has a non-numeric label (e.g. 'م 1') and the following
    "content" volume has a numeric label (e.g. '1').  They are collapsed into
    one volume that spans from the intro's start page to the content volume's
    end, keeping the content volume's label.

    This produces clean per-juz PDFs (one file per logical juz instead of two)
    and correct volume prefixes in the combined PDF.
    """
    merged_pages: list[int] = []
    merged_labels: list[str] = []
    i = 0
    n = len(vol_start_pages)
    while i < n:
        label = vol_labels[i]
        is_intro = not label.strip().isdigit()
        if is_intro and i + 1 < n and vol_labels[i + 1].strip().isdigit():
            merged_pages.append(vol_start_pages[i])
            merged_labels.append(vol_labels[i + 1])
            i += 2
        else:
            merged_pages.append(vol_start_pages[i])
            merged_labels.append(label)
            i += 1
    return merged_pages, merged_labels


def _pages_by_volume(pages_iter, vol_start_pages: list[int], vol_idx: int):
    """Yield only pages whose url_page_id falls within volume *vol_idx*.

    Volume boundaries are defined by *vol_start_pages* (e.g. [1, 183, 913, …]).
    Pages are assumed to be yielded in ascending page-id order — the generator
    stops early once it passes the volume's end.
    """
    lo = vol_start_pages[vol_idx]
    hi = (
        vol_start_pages[vol_idx + 1] - 1 if vol_idx + 1 < len(vol_start_pages) else None
    )
    for page in pages_iter:
        pid = page.get("url_page_id") or page.get("page_id")
        if pid is None:
            continue
        if pid < lo:
            continue
        if hi is not None and pid > hi:
            break
        yield page


def _find_volume_index(pid: int, vol_start_pages: list[int]) -> int:
    """Return the 0-based volume index that *pid* belongs to."""
    for vi, start in enumerate(vol_start_pages):
        if pid < start:
            return vi - 1
    return len(vol_start_pages) - 1


def _prefix_headings_by_juz(
    pages_iter, vol_start_pages: list[int], include_juz_prefix: bool = True
):
    """Prepend 1-based volume number to heading numbers, resetting
    all levels of numbering within each volume.

    Within each volume, headings are renumbered sequentially by their
    level and document order, so each volume starts at .1.

    Heading levels are normalised so the minimum level in each volume
    becomes 0 — the first heading always starts at 1 (or prefix.1).

    When *include_juz_prefix* is False (per-juz PDFs), heading numbers
    start from 1 without the volume prefix.
    """
    pages: list[dict] = list(pages_iter)

    current_vi = -1
    counters: list[int] = [0] * 20
    # Track auto heading texts to suppress duplicate implicit outline entries
    auto_texts_in_volume: set[str] = set()
    # Track the minimum heading level seen so far per volume.
    # This lets us normalise headings progressively: the first heading
    # always becomes level 0, and any subsequent shallower heading
    # (lower level number) becomes a new top-level section.
    min_level_seen: dict[int, int] = {}

    for page in pages:
        pid = page.get("url_page_id") or page.get("page_id")
        if pid is None:
            yield page
            continue

        vi = _find_volume_index(pid, vol_start_pages)
        if vi < 0:
            yield page
            continue

        if vi != current_vi:
            current_vi = vi
            counters = [0] * 20
            auto_texts_in_volume.clear()

        prefix = vi + 1
        base = min_level_seen.get(vi, 0)

        for h in page.get("resolved_headings", []):
            if not h.get("number"):
                continue
            raw_level = h.get("level", 0)

            # Update progressive minimum level for this volume
            if vi not in min_level_seen or raw_level < min_level_seen[vi]:
                min_level_seen[vi] = raw_level
                base = raw_level

            level = raw_level - base
            if level < 0:
                level = 0
            h["_nh_level"] = level
            # In single-PDF mode, headings at normalized level >= 2
            # (which would produce 4+ parts with the juz prefix)
            # are subtitles and should NOT be numbered at all.
            if include_juz_prefix and level > 1:
                h["number"] = ""
                continue
            if level >= len(counters):
                counters.extend([0] * (level - len(counters) + 1))

            # Auto headings (bracket-detected) don't consume counter slots:
            # they use the next available number without advancing the sequence.
            # This prevents an auto heading on a volume-boundary page from
            # shifting the numbering of the real TOC heading(s).
            if h.get("auto"):
                parts = []
                for i in range(level + 1):
                    if i == level:
                        parts.append(str(counters[i] + 1))
                    else:
                        parts.append(
                            str(counters[i] if counters[i] > 0 else 1)
                        )
            else:
                for i in range(level):
                    if counters[i] == 0:
                        counters[i] = 1
                        for j in range(i + 1, level + 1):
                            counters[j] = 0
                counters[level] += 1
                for i in range(level + 1, len(counters)):
                    counters[i] = 0
                parts = [str(counters[i]) for i in range(level + 1)]

            if include_juz_prefix:
                parts.insert(0, str(prefix))
            h["number"] = ".".join(parts)

            if h.get("auto") and h.get("text"):
                auto_texts_in_volume.add(h["text"])
            elif h.get("implicit") and h.get("text") and h["text"] in auto_texts_in_volume:
                h["_skip_outline"] = True

        for ue in page.get("unanchored_toc_entries", []):
            if ue.get("number"):
                for h in page.get("resolved_headings", []):
                    if (
                        h.get("implicit")
                        and h["text"] == ue["label"]
                        and h["level"] == ue["level"]
                    ):
                        ue["number"] = h["number"]
                        break

        # Rebuild toc_number from the (possibly capped) heading numbers
        nums = [
            h["number"]
            for h in page.get("resolved_headings", [])
            if h.get("number") and not h.get("auto")
        ]
        if nums:
            page["toc_number"] = f"{nums[0]}-{nums[-1]}" if len(nums) > 1 else nums[0]

        yield page


def _scan_pages_jsonl(pages_path: Path) -> tuple[int | None, int]:
    """
    Scan pages.jsonl (the real source of truth on disk) and return
    (max_page_id_found, total_line_count).

    progress.json's "next_page_id" cursor is only updated in-memory right
    after a contiguous batch is written, as a *separate* statement from the
    pf.write() calls that actually persist the lines. A KeyboardInterrupt
    (or crash) can land in that gap: lines are already on disk, but the
    cursor we'd save reflects an earlier point. If the next run trusts that
    stale cursor, it re-fetches and re-appends pages that are already
    present, creating duplicates. Scanning the file itself sidesteps that
    race entirely — it's always exactly as current as what's on disk.
    """
    max_id: int | None = None
    lines = 0
    if pages_path.exists():
        with open(pages_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                lines += 1
                try:
                    rec = json.loads(line)
                    pid = rec.get("url_page_id") or rec.get("page_id")
                except Exception:
                    pid = None
                if isinstance(pid, int) and (max_id is None or pid > max_id):
                    max_id = pid
    return max_id, lines


def _fetch_one(args_tuple) -> tuple[int, str | None, str | None]:
    """Worker target: (session, book_id, page_id, stagger_delay) → (page_id, html, err).
    Retries up to 3 times with backoff on transient errors (5xx, connection).

    Uses the Shamela Ajax endpoint (ajax/pageContent) which returns clean JSON
    with just the page content, navigation-free HTML, and pagination IDs.
    Falls back to the full-page HTML endpoint on any non-403 error.
    """
    session, book_id, page_id, stagger = args_tuple
    if stagger > 0:
        time.sleep(stagger)
    last_err: str | None = None

    # Primary: Ajax endpoint (clean JSON, no navigation shell)
    try:
        url = f"{BASE}/ajax/pageContent/{book_id}/{page_id}"
        resp = session.get(url, timeout=25)
        if resp.status_code == 403:
            return page_id, None, "403 Forbidden — try --cf_clearance"
        resp.raise_for_status()
        data = resp.json()
        pid = data.get("pageId", page_id)
        pnum = data.get("pageNum", "")
        nid = (data.get("nextId") or "").strip()
        nass = data.get("nass", "")
        html = f'<div data-page-id="{pid}" data-page-num="{pnum}" data-next-id="{nid}">{nass}</div>'
        return page_id, html, None
    except requests.exceptions.RequestException as e:
        last_err = str(e)

    # Fallback: full-page HTML endpoint
    for attempt in range(1, 4):
        try:
            url = f"{BASE}/book/{book_id}/{page_id}"
            resp = session.get(url, timeout=25)
            resp.raise_for_status()
            return page_id, resp.text, None
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            if attempt < 3:
                wait = attempt * 2
                time.sleep(wait)

    return page_id, None, last_err


def _chain_walk(
    session: requests.Session,
    book_id: int,
    start_id: int,
    workers: int,
    delay: float,
    progress: bool,
    label: str = "[ids]",
    stop_id: int | None = None,
) -> tuple[list[int], dict[int, str]]:
    """
    Walk the linked list of pages forward from start_id (inclusive) by
    following the "load next" button's data-next-id, using a **continuous
    sliding-window** pattern.

    Instead of submitting workers-sized batches and waiting for every page
    in the batch before advancing, this submits speculative fetches up to
    ~workers×4 ahead of current_id.  As soon as *any* page completes it is
    processed; if it happens to be the page current_id points to, the tip
    advances and any cached results behind the new tip are consumed
    immediately.  This eliminates the batch-synchronisation latency where
    a fast page sits idle waiting for a slow peer in the same batch.

    If *stop_id* is provided the walk stops as soon as the chain reaches
    or passes it (bounded gap resolution).  *start_id* itself is NOT
    included in the returned list when bounded (caller has it from the
    TOC).

    Returns (ids_in_order, html_cache).
    """
    ids_in_order: list[int] = []
    html_cache: dict[int, str] = {}

    current_id: int | None = start_id

    # ── safety cap for bounded walks ──────────────────────────────────
    max_fetches: int | None = None
    if stop_id is not None:
        max_fetches = max(50, (stop_id - start_id) * 3)

    from concurrent.futures import FIRST_COMPLETED
    from concurrent.futures import wait as _cf_wait

    executor = ThreadPoolExecutor(max_workers=workers)
    futures: dict = {}  # Future → page_id
    completed: dict[int, str] = {}  # page_id → html (fetched, tip not there yet)
    max_submitted: int | None = None
    n_fetched = 0
    stagger = delay / max(workers, 1)

    # ── helpers -------------------------------------------------------
    def _submit_next() -> bool:
        nonlocal max_submitted, n_fetched
        if current_id is None:
            return False
        if max_fetches is not None and n_fetched >= max_fetches:
            return False

        next_pid = max_submitted + 1 if max_submitted is not None else current_id
        if stop_id is not None and next_pid >= stop_id:
            return False
        # don't run too far ahead of current_id
        if next_pid > current_id + workers * 4:
            return False
        if next_pid in completed or any(p == next_pid for p in futures.values()):
            return True
        # only stagger the very first page to debounce the initial burst
        s = stagger if next_pid == current_id else 0
        futures[executor.submit(_fetch_one, (session, book_id, next_pid, s))] = next_pid
        max_submitted = next_pid
        n_fetched += 1
        return True

    def _consume_tip():
        nonlocal current_id, max_submitted
        while current_id in completed:
            html = completed.pop(current_id)
            next_id = get_next_page_id(html)

            # bounded: skip start_id (caller already has it)
            if stop_id is None or current_id != start_id:
                ids_in_order.append(current_id)
            html_cache[current_id] = html

            if next_id is None:
                current_id = None
                return
            if stop_id is not None and next_id >= stop_id:
                current_id = None
                return
            if next_id != current_id + 1:
                # non-sequential jump — scrap stale cache, reset submit tracker
                stale = [k for k in completed if k < next_id]
                for k in stale:
                    del completed[k]
                current_id = next_id
                max_submitted = current_id
                return
            current_id = next_id

    # ── main loop -----------------------------------------------------
    try:
        for _ in range(workers):
            if not _submit_next():
                break

        while futures:
            done_set, _ = _cf_wait(futures.keys(), return_when=FIRST_COMPLETED)
            for f in done_set:
                pid = futures.pop(f)
                _, html, err = f.result()
                if err or html is None:
                    if progress:
                        print(f"\n  [!] {label} error at {pid}: {err}")
                    for ff in futures:
                        ff.cancel()
                    futures.clear()
                    break
                completed[pid] = html
                _consume_tip()

            if not futures:
                break

            # refill the window with as many slots as just freed
            for _ in range(min(len(done_set), workers)):
                if not _submit_next():
                    break

            if progress:
                print(
                    f"\r  {label} discovered {len(ids_in_order)} page IDs …",
                    end="",
                    flush=True,
                )

    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if progress and ids_in_order:
        print(f"\r  {label} {len(ids_in_order)} page IDs discovered          ")

    return ids_in_order, html_cache


def _discover_page_ids(
    session: requests.Session,
    book_id: int,
    start_page: int,
    meta: dict,
    workers: int,
    delay: float,
    progress: bool,
    prog: dict | None = None,
) -> list[int]:
    """
    Return an ordered list of every page ID for the book.

    Strategy (fastest first):
      1. Derive IDs from meta["toc_flat"] directly — zero extra HTTP
         requests for the normal, regularly-spaced parts of the TOC.
         Any *individual* irregular gap (a section the TOC didn't label
         page-by-page) is chain-walked on its own, bounded to that gap —
         so one bad gap in an otherwise-good TOC no longer forces walking
         the entire book page-by-page (which used to be the fallback any
         time a single gap exceeded the threshold, and was the dominant
         cost on large books).
         Trailing pages past the final TOC entry are chain-walked once and
         cached in `prog["book_last_page_id"]` so later resumes don't repeat
         that walk.
      2. If the TOC has fewer than 2 usable anchors at all, fall back to a
         fully concurrent chain-walk from start_page.

    Returns the ordered list of page IDs starting from start_page.
    """
    GAP_FILL_THRESHOLD = 20

    # ── Method 1: derive from TOC ──────────────────────────────────────
    toc_flat = meta.get("toc_flat", [])
    toc_ids = sorted({e["page_id"] for e in toc_flat if e.get("page_id")})
    if len(toc_ids) >= 2:
        filled: list[int] = []
        extra_cache: dict[int, str] = {}
        walked_segments = 0
        walked_pages = 0

        # Pages before the first TOC entry (e.g. preface, cover, volume 1
        # content when the TOC starts in volume 2).  Fill linearly since
        # these form a continuous pre-TOC block with no TOC anchors.
        if toc_ids and start_page < toc_ids[0]:
            for pid in range(start_page, toc_ids[0]):
                filled.append(pid)

        for i, tid in enumerate(toc_ids):
            nxt = toc_ids[i + 1] if i < len(toc_ids) - 1 else None

            # This whole segment is before the resume point — nothing in
            # it would survive the final start_page filter, so skip it
            # entirely (including any gap-walk) rather than paying for it.
            if nxt is not None and nxt < start_page:
                continue

            if tid >= start_page:
                filled.append(tid)

            if nxt is None:
                continue

            gap = nxt - tid
            if gap <= 1:
                continue

            if gap - 1 <= GAP_FILL_THRESHOLD:
                # Small, regular gap — fill linearly, zero requests.
                for g in range(1, gap):
                    val = tid + g
                    if val >= start_page:
                        filled.append(val)
            else:
                # Irregular section — chain-walk just this one gap (bounded
                # to stop at `nxt`) instead of giving up on TOC-derivation
                # for the entire rest of the book.
                seg_ids, seg_cache = _chain_walk(
                    session,
                    book_id,
                    tid,
                    workers,
                    delay,
                    progress=False,
                    label="[ids:gap]",
                    stop_id=nxt,
                )
                walked_segments += 1
                walked_pages += len(seg_ids)
                extra_cache.update(seg_cache)
                for val in seg_ids:
                    if val >= start_page:
                        filled.append(val)

        if walked_segments and progress:
            print(
                f"  [ids] {walked_segments} irregular TOC gap(s) chain-walked "
                f"({walked_pages} pages) — rest derived directly from TOC"
            )

        # The gap-fill above only covers *between* TOC entries — trailing
        # pages after the last TOC anchor still need to be discovered.
        # Instead of chain-walking the entire tail (one request per page),
        # fetch page 1's navigation shell once and read the last page ID
        # from the `>>` button.
        book_last_id = _find_last_page_id(session, book_id)
        new_tail: list[int] = []
        if book_last_id is not None and book_last_id > toc_ids[-1]:
            new_tail = [
                x for x in range(toc_ids[-1] + 1, book_last_id + 1) if x >= start_page
            ]
        elif book_last_id is not None:
            pass  # last page is before or at last TOC entry — nothing to add
        else:
            # Failed to discover last page (e.g. network issue) — fall back
            # to linear fill up to a reasonable buffer past the last TOC id
            # so scraping can still proceed (the fetch phase will stop when
            # pages run out).  This case is rare.
            if progress:
                print(
                    "  [ids] warning: could not determine last page, "
                    "using estimated tail of 100 pages"
                )
            new_tail = [
                x for x in range(toc_ids[-1] + 1, toc_ids[-1] + 101) if x >= start_page
            ]

        filled.extend(new_tail)
        if filled:
            if progress:
                extra = (
                    f", +{len(new_tail)} trailing pages past last TOC entry"
                    if new_tail
                    else ""
                )
                print(f"  [ids] derived {len(filled)} page IDs from TOC{extra}")
            return filled, extra_cache

    # ── Method 2: fast concurrent chain-walk ───────────────────────────
    if progress:
        print(f"  [ids] walking page chain with {workers} workers …", flush=True)
    return _chain_walk(session, book_id, start_page, workers, delay, progress)


def scrape_book_pages_resumable(
    session: requests.Session,
    book_id: int,
    book_dir: Path,
    start_page: int,
    meta: dict = None,
    limit: int = None,
    delay: float = 0.5,
    force: bool = False,
    progress: bool = True,
    workers: int = 8,
) -> dict:
    """
    TRUE parallel scraper.

    Phase 1 — ID discovery (fast):
      • Tries to enumerate all page IDs from the TOC without any HTTP requests.
      • Falls back to a batched concurrent chain-walk (workers pages at a time).

    Phase 2 — Parallel fetch (fast):
      • All page IDs are known upfront → all fetches submitted to the pool at once.
      • Responses are collected as they arrive (any order) then sorted and written
        to pages.jsonl in the correct order.
      • Checkpoint written every `checkpoint_every` pages.

    Memory: only `workers × avg_page_size` bytes in flight at any moment.
    """
    progress_path = book_dir / "progress.json"
    pages_path = book_dir / "pages.jsonl"

    prog = load_json(progress_path, None)
    if prog and prog.get("status") == "done" and not force:
        if progress:
            print(
                f"  [skip] book {book_id}: already fully scraped "
                f"({prog.get('pages_scraped', 0)} pages)"
            )
        return prog

    already_scraped = 0
    resume_from_id = start_page

    if not prog or force:
        prog = {
            "book_id": book_id,
            "status": "in_progress",
            "next_page_id": start_page,
            "last_page_id": None,
            "pages_scraped": 0,
        }
        pages_path.write_text("", encoding="utf-8")
        atomic_write_json(progress_path, prog)
    elif prog.get("status") in ("in_progress", "paused", "error"):
        resume_from_id = prog.get("next_page_id") or start_page
        already_scraped = prog.get("pages_scraped", 0)

        # Self-heal against the write/checkpoint race described above:
        # trust pages.jsonl itself over progress.json's cursor whenever the
        # file shows more than the cursor claims.
        disk_max_id, disk_lines = _scan_pages_jsonl(pages_path)
        healed = False
        if disk_max_id is not None and disk_max_id + 1 > resume_from_id:
            resume_from_id = disk_max_id + 1
            healed = True
        if disk_lines > already_scraped:
            already_scraped = disk_lines
            healed = True

        if progress and healed:
            print(
                f"  [heal] progress.json was behind pages.jsonl on disk — "
                f"correcting resume point to page_id={resume_from_id} "
                f"({already_scraped} pages on disk)"
            )
        if progress:
            print(
                f"  [resume] from page_id={resume_from_id} "
                f"({already_scraped} pages already saved)"
            )

    # ── Phase 1: enumerate IDs ─────────────────────────────────────────
    meta = meta or {}
    discovery = _discover_page_ids(
        session, book_id, resume_from_id, meta, workers, delay, progress, prog
    )
    # _discover_page_ids may return (list, cache) or just list
    if isinstance(discovery, tuple):
        page_ids, html_cache = discovery
    else:
        page_ids, html_cache = discovery, {}

    if not page_ids:
        prog["status"] = "error"
        prog["error"] = "Could not discover any page IDs"
        atomic_write_json(progress_path, prog)
        return prog

    if limit is not None:
        page_ids = page_ids[:limit]

    total = len(page_ids)
    if progress:
        print(f"  [fetch] {total} pages to fetch with {workers} workers …", flush=True)

    # ── Phase 2: parallel fetch all pages ─────────────────────────────
    # Stagger only the initial burst across worker slots (i % workers) so
    # the first `workers` requests don't all fire in the same instant.
    # IMPORTANT: this must NOT scale with the global page index — an
    # earlier version used `i * stagger` over the full page_ids range,
    # which made every fetch sleep proportionally to its position in the
    # *whole book* before even sending its request (e.g. page 400 of 441
    # would sleep ~25s before requesting, regardless of how many workers
    # were free). That created a hard floor of roughly
    # total_pages × (delay / workers) seconds of pure idle sleep — easily
    # the dominant cost for any book of a few hundred+ pages, independent
    # of network speed or compute. The pool's `max_workers` already caps
    # real concurrency; we only need to debounce the initial burst.
    stagger = delay / max(workers, 1)
    checkpoint_every = max(workers * 4, 50)

    results_buf: dict[int, list[dict]] = {}  # page_id → parsed page data
    count = already_scraped
    error_seen: str | None = None

    write_cursor = 0  # index into page_ids of next page to write

    with open(pages_path, "a", encoding="utf-8") as pf:
        # NOTE: deliberately not using "with ThreadPoolExecutor(...) as pool:".
        # That context manager's __exit__ calls pool.shutdown(wait=True), which
        # blocks until every *already-submitted* future finishes — including
        # the hundreds still sitting in the queue behind the active `workers`.
        # On Ctrl+C that made shutdown appear to hang, which is why two
        # presses were needed and why Python's own interpreter-shutdown
        # thread-join (atexit) would then race with the second interrupt and
        # print "Exception ignored on threading shutdown". We manage shutdown
        # ourselves so a single Ctrl+C cancels the queued work immediately.
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            # Submit all fetches; pages already in html_cache skip the network
            # Stagger only the first `workers` pages to debounce the initial
            # burst — the equivalent of (i % workers) * stagger on *every*
            # page would add minutes/hours of pure sleep for large books.
            future_to_pid: dict = {}
            for i, pid in enumerate(page_ids):
                if pid in html_cache:
                    # Already fetched during discovery — wrap in a trivial future
                    html = html_cache.pop(pid)  # free memory as we go
                    f = pool.submit(lambda h=html, p=pid: (p, h, None))
                else:
                    s = (i % workers) * stagger if i < workers else 0
                    f = pool.submit(_fetch_one, (session, book_id, pid, s))
                future_to_pid[f] = pid

            # Collect results as they complete (any order), store in buffer
            completed_set: set[int] = set()

            for f in as_completed(future_to_pid):
                pid_result, html, err = f.result()

                if err:
                    error_seen = err
                    if progress:
                        print(f"\n  [!] Error on page {pid_result}: {err}")
                    # Don't abort — collect what we have; errors recorded below
                    completed_set.add(pid_result)
                    results_buf[pid_result] = []  # empty = skipped
                    continue

                page_data = parse_page_html(html)
                # Stamp the Shamela URL sequence ID (pid_result) onto every
                # sub-page so resolve_book_headings can compare against the
                # same ID space used by the TOC.  The page_id field from
                # data-page-id is the *printed* page number and lives in a
                # different namespace — matching TOC url-IDs against printed
                # page numbers causes headings to land on the wrong pages.
                for pg in page_data:
                    pg["url_page_id"] = pid_result
                results_buf[pid_result] = page_data
                completed_set.add(pid_result)

                # Write pages in order as soon as a contiguous prefix is ready
                flushed_any = False
                while write_cursor < len(page_ids):
                    next_pid = page_ids[write_cursor]
                    if next_pid not in completed_set:
                        break
                    for pg in results_buf.pop(next_pid, []):
                        pf.write(_fast_json(pg, indent=False) + "\n")
                        count += 1
                    write_cursor += 1
                    flushed_any = True

                if flushed_any:
                    pf.flush()
                    last_written = page_ids[write_cursor - 1]
                    next_pending = (
                        page_ids[write_cursor] if write_cursor < len(page_ids) else None
                    )
                    prog["last_page_id"] = last_written
                    prog["next_page_id"] = next_pending
                    prog["pages_scraped"] = count
                    prog["status"] = "in_progress"
                    # Checkpoint periodically (not every page — avoids I/O bottleneck)
                    if write_cursor % checkpoint_every == 0 or next_pending is None:
                        atomic_write_json(progress_path, prog)

                if progress:
                    pct = int(100 * len(completed_set) / total)
                    print(
                        f"\r  fetched {len(completed_set)}/{total} ({pct}%)  "
                        f"written {count - already_scraped}  ",
                        end="",
                        flush=True,
                    )
        except KeyboardInterrupt:
            if progress:
                print(
                    "\n  [!] Interrupted — cancelling queued fetches "
                    "(a few in-flight requests may still finish) …"
                )
            # cancel_futures=True drops every future that hasn't started yet
            # instead of waiting for the whole queue to drain. Only the
            # `workers` requests already in flight still need to finish (or
            # time out), so this returns almost immediately instead of
            # hanging on the full backlog.
            pool.shutdown(wait=False, cancel_futures=True)
            prog["pages_scraped"] = count
            prog["status"] = "paused"
            atomic_write_json(progress_path, prog)
            raise
        else:
            pool.shutdown(wait=True)

    if progress:
        print()

    if error_seen and count == already_scraped:
        prog["status"] = "error"
        prog["error"] = error_seen
    elif write_cursor < len(page_ids):
        prog["status"] = "paused"
    else:
        prog["status"] = "done"
        prog["next_page_id"] = None

    prog["pages_scraped"] = count
    atomic_write_json(progress_path, prog)
    return prog


# ═══════════════════════════════════════════════════════════════════════════
# PDF GENERATION  (weasyprint — Arabic renders via system fonts, no reshaping)
# ═══════════════════════════════════════════════════════════════════════════

import html as _html


def _e(text: str) -> str:
    """HTML-escape a string."""
    return _html.escape(str(text)) if text else ""


def _build_heading_level_css() -> str:
    """Generate CSS for .toc-nh-{0..9} from STYLE_CFG heading_levels."""
    parts = []
    for hl in STYLE_CFG.get("heading_levels", DEFAULT_STYLE_CFG["heading_levels"]):
        lvl = hl["level"]
        sz = hl["size_pdf"]
        col = hl["color"]
        parts.append(
            f"    .toc-numbered-heading.toc-nh-{lvl} {{\n"
            f"        font-size: {sz}pt;\n"
            f"        color: {col};\n"
            f"    }}"
        )
    return "\n".join(parts)


def _truncate(title: str, max_words: int = 5) -> str:
    """Truncate a title to max_words; if longer keeps first few + '...' + last word."""
    words = title.split()
    if len(words) <= max_words:
        return title
    return " ".join(words[: max_words - 1]) + " … " + words[-1]


# NOTE: Visible TOC rendering has been removed per prompt_toc.txt requirements.
# The PDF's native outline/bookmark feature (via WeasyPrint bookmark-level CSS)
# is used instead.  The TOC tree is still parsed and stored in meta["toc"] /
# meta["toc_flat"] for downstream use, but no visible TOC page is produced.


def _build_html_css(meta: dict, columns: int = 1, fast: bool = False) -> str:
    """Return the full <style> block CSS string (shared by stream and legacy paths).

    columns: number of text columns per page for the book-text body (1 or 2).
             2 gives a classic printed-book two-column layout via CSS
             multi-column (WeasyPrint supports `column-count`/`column-gap`).
    """
    import datetime as _dt  # noqa: F401 — kept for callers that need it

    font_body = f'"{STYLE_CFG["fonts"]["body"]}", "Noto Naskh Arabic", "Amiri Quran", "Amiri", serif'
    font_head = f'"{STYLE_CFG["fonts"]["heading"]}", "Noto Naskh Arabic", "Amiri Quran", "Amiri", serif'

    toc_flat = meta.get("toc_flat", [])
    max_toc_level = -1
    for entry in toc_flat:
        if entry.get("level", 0) > max_toc_level:
            max_toc_level = entry["level"]

    toc_bm_css_parts = []
    for lvl in range(max_toc_level + 1):
        bm = lvl + 2
        toc_bm_css_parts.append(
            f".toc-bm-{lvl}, .toc-numbered-heading.toc-bm-{lvl} "
            f"{{ bookmark-level: {bm}; bookmark-label: content(); "
            f"prince-bookmark-level: {bm}; prince-bookmark-label: content(); }}"
        )
    toc_bm_css = "\n    ".join(toc_bm_css_parts)

    # Islamic gold palette
    pal = STYLE_CFG["palette"]
    GOLD = pal["gold"]
    GOLD_L = pal["gold_light"]
    GOLD_D = pal["gold_dark"]
    TEAL = pal["teal"]
    RUST = pal["rust"]
    INK = pal["ink"]
    CREAM = pal["cream"]
    PARCHMENT = pal["parchment"]
    SEPIA = pal["sepia"]
    BROWN = pal["brown"]

    # kashida (Arabic letter-stretch justification) looks great but is a
    # major WeasyPrint performance sink — it runs a custom per-line glyph
    # pass for every justified line. --fast (or fast=True) drops it in
    # favor of plain justify, which is dramatically faster on big books.
    JUSTIFY_RULE = "" if fast else "text-justify: kashida;"

    css = f"""
    /* ═══════════════════════════════════════════════════════
       PAGE GEOMETRY  —  tight margins = maximum words per page
       ═══════════════════════════════════════════════════════ */

    /* Front-matter: roman numerals, no running header */
    @page front-matter {{
        size: A4;
        margin: 1cm 1.1cm 1.3cm 1.1cm;
        background: {CREAM};
        @bottom-center {{
            content: counter(page, lower-roman);
            font-family: {font_body};
            font-size: 8pt;
            font-weight: 500;
            color: {GOLD_L};
        }}
        @top-left  {{ content: none; }}
        @top-right {{ content: none; }}
    }}

    /* Book-text pages */
    @page book-text {{
        size: A4;
        margin: 1cm 1.1cm 1.3cm 1.1cm;
        background: {CREAM};

        @top-left {{
            content: counter(page);
            font-family: {font_body};
            font-size: 14pt;
            font-weight: 700;
            color: {RUST};
            padding-left: 0.5em;
        }}

        @top-center {{ content: none; }}

        @top-right {{ content: none; }}

        @bottom-center {{
            content: "— " counter(page) " —";
            font-family: {font_body};
            font-size: 14pt;
            font-weight: 700;
            color: {RUST};
        }}
        @bottom-left  {{ content: none; }}
        @bottom-right {{ content: none; }}
    }}

    /* Bare @page default (cover) */
    @page {{
        size: A4;
        margin: 1cm;
        background: linear-gradient(180deg, {PARCHMENT} 0%, {CREAM} 25%, #fffdf5 50%, {CREAM} 75%, {PARCHMENT} 100%);
    }}
    @page :first {{
        margin: 0;
        background: linear-gradient(180deg, {PARCHMENT} 0%, {CREAM} 25%, #fffdf5 50%, {CREAM} 75%, {PARCHMENT} 100%);
        @top-left     {{ content: none; }}
        @top-center   {{ content: none; }}
        @top-right    {{ content: none; }}
        @bottom-center {{ content: none; }}
        @bottom-left  {{ content: none; }}
        @bottom-right {{ content: none; }}
    }}

    /* ═══════════════════════════════════════════════════════
       GLOBAL TYPOGRAPHY
       ═══════════════════════════════════════════════════════ */
    * {{ box-sizing: border-box; }}

    body {{
        font-family: {font_body};
        font-size: {STYLE_CFG["body_size_pdf"]}pt;
        font-weight: 700;
        line-height: 1.9;
        direction: rtl;
        text-align: justify;
        {JUSTIFY_RULE}
        color: {INK};
        background: {CREAM};
    }}

    /* ═══════════════════════════════════════════════════════
       COVER PAGE — ornate multi-layer design
       ═══════════════════════════════════════════════════════ */
    .cover {{
        page-break-after: always;
        text-align: center;
        padding: 0;
        margin: 0;
        background: linear-gradient(180deg, {PARCHMENT} 0%, {CREAM} 25%, #fffdf5 50%, {CREAM} 75%, {PARCHMENT} 100%);
        height: 29.7cm;
        display: flex;
        flex-direction: column;
    }}

    .cover-frame-outer {{
        margin: 0;
        padding: 0.6cm;
        flex: 1;
        background: linear-gradient(180deg, #fdf8ee 0%, #fffef8 40%, #fdf8ee 100%);
        display: flex;
        flex-direction: column;
    }}

    .cover-frame-inner {{
        border: 2.5px solid {GOLD_L};
        padding: 1.2cm 1.8cm 1cm;
        flex: 1;
        position: relative;
        background: linear-gradient(180deg, #fffcf2 0%, #fff 50%, #fef9ed 100%);
        display: flex;
        flex-direction: column;
        justify-content: center;
    }}

    .cover-corner {{
        position: absolute;
        width: 2.5cm;
        height: 2.5cm;
        border-color: {GOLD_D};
        border-style: solid;
        border-width: 0;
        font-size: 22pt;
        color: {GOLD};
        line-height: 1;
    }}
    .cover-corner-tl {{
        top: 0.2cm; right: 0.2cm;
        border-top-width: 3px;
        border-right-width: 3px;
        padding-top: 0.15cm;
        padding-right: 0.15cm;
        text-align: right;
    }}
    .cover-corner-tr {{
        top: 0.2cm; left: 0.2cm;
        border-top-width: 3px;
        border-left-width: 3px;
        padding-top: 0.15cm;
        padding-left: 0.15cm;
        text-align: left;
    }}
    .cover-corner-bl {{
        bottom: 0.2cm; right: 0.2cm;
        border-bottom-width: 3px;
        border-right-width: 3px;
        padding-bottom: 0.15cm;
        padding-right: 0.15cm;
        text-align: right;
    }}
    .cover-corner-br {{
        bottom: 0.2cm; left: 0.2cm;
        border-bottom-width: 3px;
        border-left-width: 3px;
        padding-bottom: 0.15cm;
        padding-left: 0.15cm;
        text-align: left;
    }}

    .cover-ornament-top {{
        font-size: 14pt;
        color: {GOLD};
        letter-spacing: 0.5em;
        margin: 0.4em 0 0.2em;
    }}
    .cover-bismillah {{
        font-size: 16pt;
        color: {BROWN};
        font-weight: 700;
        margin: 0.6em 0 0.3em;
        letter-spacing: 0.08em;
    }}

    .cover-divider {{
        margin: 0.5em auto;
        width: 70%;
        color: {GOLD_L};
        font-size: 10pt;
        letter-spacing: 0.3em;
        border: none;
        overflow: hidden;
    }}

    .cover h1 {{
        font-size: 26pt;
        color: {SEPIA};
        margin: 0.4em 0.2em;
        line-height: 1.6;
        font-weight: 900;
        letter-spacing: 0.03em;
    }}

    .cover .meta-table {{
        margin: 1em auto;
        border-collapse: collapse;
        width: 75%;
        font-size: 11pt;
        color: {BROWN};
    }}
    .cover .meta-table td {{
        padding: 0.4em 0.8em;
        text-align: right;
        border-bottom: 1px dotted {GOLD_L};
        vertical-align: top;
    }}
    .cover .meta-label {{
        color: {SEPIA};
        font-weight: 800;
        white-space: nowrap;
        width: 28%;
    }}

    .cover-ornament-bottom {{
        font-size: 12pt;
        color: {GOLD};
        letter-spacing: 0.5em;
        margin: 0.6em 0 0.3em;
    }}

    .cover .source {{
        font-size: 8pt;
        color: #999;
        margin-top: 1.2em;
    }}

    /* ═══════════════════════════════════════════════════════
       SECTIONS
       ═══════════════════════════════════════════════════════ */
    .section       {{ page-break-before: always; }}
    .section-front {{
        page: front-matter;
        page-break-before: always;
    }}
    .section-text {{
        page: book-text;
        page-break-before: always;
        counter-reset: page 1;
    }}
    .section-text > h2:first-child {{
        margin-top: 0;
    }}

    /* ═══════════════════════════════════════════════════════
       MULTI-COLUMN BOOK TEXT (--columns 2)
       ═══════════════════════════════════════════════════════ */
    {f'''
    .section-text {{{{
        column-count: {columns};
        column-gap: 1cm;
        column-fill: auto;
    }}}}
    .section-text h2, .section-text h3, .section-text h4 {{{{
        column-span: all;
    }}}}
    .volume-divider {{{{
        column-span: all;
    }}}}
    ''' if columns and columns > 1 else ''}

    /* Volume divider splash */
    .volume-divider {{
        margin: 0 0 15px 0;
        padding: 0;
    }}
    .volume-divider h2 {{
        font-size: 18pt;
        color: #c0392b;
        border: none;
        margin: 0 0 10px 0;
        padding: 5px 0;
        letter-spacing: 0.05em;
    }}

    /* ═══════════════════════════════════════════════════════
       HEADINGS  (bigger + coloured)
       ═══════════════════════════════════════════════════════ */

    /* Section-level h2 — bold teal with gold underline */
    h2 {{
        font-family: {font_head};
        font-size: 20pt;
        font-weight: 900;
        color: {TEAL};
        border-bottom: 3px solid {GOLD};
        padding-bottom: 0.25em;
        margin-top: 1.6em;
        margin-bottom: 0.5em;
        bookmark-level: 1;
        bookmark-label: content();
        prince-bookmark-level: 1;
        prince-bookmark-label: content();
    }}

    h3 {{
        font-family: {font_head};
        font-size: 17pt;
        font-weight: 800;
        color: {TEAL};
        border-right: 5px solid {GOLD};
        padding-right: 0.5em;
        margin-top: 1em;
        margin-bottom: 0.35em;
    }}

    /* Inline chapter heading from scraped <p class="b"> or <p class="head"> */
    .chapter-heading {{
        font-family: {font_head};
        font-size: 17pt;
        font-weight: 900;
        color: {RUST};
        text-align: center;
        margin: 1em 0 0.4em;
        padding: 0.25em 0.5em;
        border-top: 2px solid {GOLD};
        border-bottom: 2px solid {GOLD};
        background: #fdf8ee;
        string-set: chapter-title content();
    }}

    /* ═══════════════════════════════════════════════════════
       TOC BOOKMARKS  (invisible anchors for PDF outline)
       ═══════════════════════════════════════════════════════ */
    .toc-bookmark-anchor {{
        height: 0;
        overflow: hidden;
        line-height: 0;
        font-size: 1pt;
        margin: 0;
        padding: 0;
    }}
    {toc_bm_css}

    /* ═══════════════════════════════════════════════════════
       NUMBERED HEADINGS  (visible TOC entry markers)
       ═══════════════════════════════════════════════════════ */
    .toc-numbered-heading {{
        font-family: {font_body};
        font-size: 17pt;
        font-weight: 700;
        margin: 0.25em 0 0.15em 0;
        padding: 0;
        line-height: 1.5;
        bookmark-level: none;
        prince-bookmark-level: none;
    }}
    /* Per-level heading colors + sizes from style config */
    {_build_heading_level_css()}
    /* Auto-detected bracket headings — italic */
    .toc-numbered-heading.toc-nh-auto {{
        font-style: italic;
    }}
    /* Level shape marker */
    .heading-shape {{
        font-size: 0.7em;
        margin-inline-end: 0.3em;
        user-select: none;
    }}


    /* ═══════════════════════════════════════════════════════
       AUTHOR BIO
       ═══════════════════════════════════════════════════════ */
    .author-bio {{
        font-size: 11pt;
        font-weight: 500;
        color: #222;
        line-height: 1.9;
        background: #f9f7f2;
        border-right: 5px solid {GOLD};
        padding: 0.7em 0.9em;
        margin: 0.4em 0;
    }}
    .author-bio p {{ margin: 0.25em 0; }}

    /* ═══════════════════════════════════════════════════════
       BODY TEXT  —  dense, bolder
       ═══════════════════════════════════════════════════════ */
    .page-entry {{
        margin-bottom: 0;
    }}
    .page-text p {{
        margin: 0.15em 0;          /* minimal vertical gap = more words per page */
        text-align: justify;
        {JUSTIFY_RULE}
        orphans: 2;
        widows: 2;
        font-weight: 500;
    }}

    /* ═══════════════════════════════════════════════════════
       FOOTNOTES (hamesh)
       ═══════════════════════════════════════════════════════ */
    .hamesh {{
        position: relative;
        border-top: none;
        margin-top: 0.8em;
        padding-top: 0.5em;
        font-size: 10pt;
        font-weight: 400;
        color: #555;
        line-height: 1.6;
    }}
    .hamesh::before {{
        content: "";
        display: block;
        width: 3.5cm;
        border-top: 1px dashed #999;
        margin-bottom: 0.5em;
    }}
    .hamesh p {{
        margin: 0.05em 0;
        text-align: right;
    }}
    """
    return css


def _plain_text(html_frag: str) -> str:
    """Strip HTML tags and unescape HTML entities from a fragment for text comparison."""
    import html as _html_mod

    stripped = re.sub(r"<[^>]+>", "", html_frag)
    return _html_mod.unescape(stripped)


_BRACKET_STRIP_CHARS = "-[]（）.「」【】《》〈〉«»‹›"
_TASHKEEL_RE = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")


def _normalize_ar(s: str) -> str:
    """Normalize Arabic for comparing page text against a TOC label."""
    s = _plain_text(s)
    # Strip parenthesized/bracketed footnote markers (e.g. (١), [2], (أ)) to prevent them breaking matches
    # Strip parenthesized footnote markers like (١) or (٢، ٣) (must contain a digit).
    # Does NOT strip content like (حرف الباء) (no digits).
    s = re.sub(r"\([\u0660-\u0669\d\s،,.\-\/أ-ي]*[\u0660-\u0669\d][\u0660-\u0669\d\s،,.\-\/أ-ي]*\)", "", s)
    # Strip single-letter markers like (أ) (no digit, just one letter)
    s = re.sub(r"\([\u0621-\u064A]\)", "", s)
    # Strip bracketed footnote/folio markers like [١] or [٤٧/أ] (must contain a digit).
    # Does NOT strip bracket headings like [التخامة في القبلة وفي الصلاة] (no digits).
    s = re.sub(r"\[[\u0660-\u0669\d\s،,.\-\/أ-ي]*[\u0660-\u0669\d][\u0660-\u0669\d\s،,.\-\/أ-ي]*\]", "", s)
    # Strip single-letter markers like [أ] (no digit, just one letter)
    s = re.sub(r"\[[\u0621-\u064A]\]", "", s)
    # Strip leading bracket annotations like [٤٧/أ] that are followed by
    # text (e.g. folio markers before a heading).  Bracket-only lines
    # like [الفصل الثالث] are untouched — they are stripped earlier by
    # _bracket_stripped before reaching this function.
    s = re.sub(r"^\[[^\]]*\]\s+", "", s)
    # Normalise common Arabic letter variants: alif forms → bare alif,
    # alif maqsura → yaa, taa marbouta → haa, hamza-on-waw → waw,
    # hamza-on-ya → yaa.  This catches OCR / spelling mismatches
    # like متآخرو vs متأخرو or رأي vs رأى.
    s = s.replace("\u0622", "\u0627")  # آ → ا
    s = s.replace("\u0623", "\u0627")  # أ → ا
    s = s.replace("\u0625", "\u0627")  # إ → ا
    s = s.replace("\u0649", "\u064a")  # ى → ي
    s = s.replace("\u0629", "\u0647")  # ة → ه
    s = s.replace("\u0624", "\u0648")  # ؤ → و
    s = s.replace("\u0626", "\u064a")  # ئ → ي
    s = _TASHKEEL_RE.sub("", s)
    # Replace Arabic punctuation with space so labels that differ only by
    # commas, dashes, etc. still match (e.g. TOC "طيب في نفسه صاحب خير" vs
    # body "طيب في نفسه، صاحب خير").
    s = re.sub(r"[،؟!\.\,\;\:\-\(\)\[\]\{\}«»‹›\/\"\'\u201c\u201d\u2018\u2019]", " ", s)
    s = s.strip(_BRACKET_STRIP_CHARS + " :،ـ")
    s = re.sub(r"\s+", " ", s).strip()
    # Strip leading Arabic-Indic / Latin digits followed by a separator
    # so eg body "١- رأى أرسطوطاليس..." matches TOC "رأي أرسطوطاليس...".
    s = re.sub(r"^[\u0660-\u06690-9]+[\s\-\.\)\]\}]*", "", s).strip()
    return s


def _bracket_stripped(line_html: str) -> str | None:
    plain = _plain_text(line_html).strip()
    m = re.match(r"^\[(.+)\]$", plain)
    if m:
        return m.group(1).strip(_BRACKET_STRIP_CHARS).strip()
    return None


def _strip_title_brackets(line_html: str, label: str) -> str:
    """
    Strips enclosing brackets from the title part of the line, 
    preserving footnotes (whether inside or outside the brackets).
    """
    stripped = line_html.strip()
    if not stripped.startswith("["):
        return line_html
        
    # Case 1: Entire line is wrapped in brackets, e.g. [تمهيد (١)] or [تمهيد]
    if stripped.endswith("]"):
        return stripped[1:-1].strip()
        
    # Case 2: Title is wrapped in brackets but followed by footnote/text, e.g. [تمهيد] (١)
    idx = stripped.find("]")
    if idx != -1:
        inside = stripped[1:idx]
        if _normalize_ar(inside) == _normalize_ar(label):
            return (inside + stripped[idx+1:]).strip()
            
    return line_html



def _extract_bracket_headings(paras: list[dict] | None) -> list[dict]:
    found: list[dict] = []
    if not paras:
        return found
    for pi, para in enumerate(paras):
        if para.get("type") == "hamesh":
            continue
        lines = para.get("lines", [])
        li = 0
        while li < len(lines):
            line = lines[li]
            stripped = _bracket_stripped(line)
            if stripped:
                found.append(
                    {
                        "para_idx": pi,
                        "line_idx": li,
                        "ptype": para.get("type"),
                        "text": stripped,
                    }
                )
                li += 1
                continue
            plain = _plain_text(line).strip()
            if plain.startswith("[") and "]" not in plain:
                joined_html = line
                joined_li = li
                li += 1
                while li < len(lines):
                    next_plain = _plain_text(lines[li]).strip()
                    joined_html += "<br>" + lines[li]
                    li += 1
                    if "]" in next_plain:
                        break
                stripped = _bracket_stripped(joined_html)
                if stripped:
                    found.append(
                        {
                            "para_idx": pi,
                            "line_idx": joined_li,
                            "ptype": para.get("type"),
                            "text": stripped,
                        }
                    )
                continue
            li += 1
    return found


def _has_extra_content(line_html: str, norm_label: str) -> bool:
    """
    Return True if the raw page line has content beyond what the TOC label
    covers — e.g. a footnote reference (١) or extra words that normalization
    stripped via tashkeel removal.

    Comparison is done on tashkeel-stripped plain text WITHOUT removing
    parenthesised groups, so "(١)" still counts as extra content.
    Bracket chars and edge punctuation are stripped to avoid false positives
    on lines like "[عنوان]" where the brackets are the only "extra" content.
    """
    plain = _plain_text(line_html).strip()
    
    # Strip footnote markers to see if there is other text
    plain_no_fn = re.sub(r"\([\u0660-\u0669\d\s،,.\-\/أ-ي]+\)", "", plain)
    plain_no_fn = re.sub(r"\[[\u0660-\u0669\d\s،,.\-\/أ-ي]+\]", "", plain_no_fn)
    
    # Normalize both
    plain_norm = _normalize_ar(plain_no_fn)
    label_norm = _normalize_ar(norm_label)
    
    # If the text after stripping footnotes matches the normalized TOC label,
    # then there is no *other* extra content. We can merge the footnote into the heading.
    if plain_norm == label_norm:
        return False

    # Align with _normalize_ar: replace punctuation (except parentheses,
    # which would break footnote detection like (١)) with spaces.
    plain = re.sub(r"[،؟!\.\,\;\:\-]", " ", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    plain_stripped = plain.strip(_BRACKET_STRIP_CHARS + " :،ـ")
    plain_no_tashkeel = _TASHKEEL_RE.sub("", plain_stripped).strip()
    norm_label_plain = _TASHKEEL_RE.sub("", norm_label).strip()
    return len(plain_no_tashkeel) > len(norm_label_plain) + 3


def _find_label_in_lines(
    norm_label: str, flat_lines: list[tuple[int, int, str]], cursor: int
) -> tuple[tuple[int, int] | None, bool]:
    """
    Find where a TOC label appears in page text.
    Returns (start, end) line span and keep_line (True when the matched line
    carries extra body text beyond the TOC label — show both heading and line).
    keep_line is always False here; the caller upgrades it via _has_extra_content
    after receiving the match.
    """
    if not norm_label:
        return None, False

    for start in range(cursor, len(flat_lines)):
        for span_len in (1, 2, 3):
            end = start + span_len
            if end > len(flat_lines):
                break
            joined = " ".join(flat_lines[i][2] for i in range(start, end))
            if joined == norm_label:
                return (start, end), False
            if (
                span_len == 1
                and len(norm_label) >= 4
                and flat_lines[start][2].startswith(norm_label)
            ):
                return (start, start + 1), flat_lines[start][2] != norm_label

    # Fallback: label appears at the start of a line followed by a
    # non-alphanumeric character.  This handles TOC labels that are
    # short (1-3 chars, e.g. Arabic verse numbers like "٦" matching
    # page content "٦ - إ...") without falsely matching "٦" against
    # "٦٧" (the next char "٧" is alphanumeric → skipped).
    for start in range(cursor, len(flat_lines)):
        line_norm = flat_lines[start][2]
        if (
            line_norm.startswith(norm_label)
            and len(line_norm) > len(norm_label)
            and not line_norm[len(norm_label)].isalnum()
        ):
            return (start, start + 1), True

    # Fallback: label starts with the body line (e.g. TOC
    # "مهمة: توجيه..." vs body "مهمة").  The label has more
    # content after a separator that was normalised to space.
    for start in range(cursor, len(flat_lines)):
        line_norm = flat_lines[start][2]
        if (
            len(norm_label) >= 4
            and len(line_norm) >= 3
            and len(line_norm) < len(norm_label)
            and norm_label.startswith(line_norm)
            and norm_label[len(line_norm)] == " "
        ):
            return (start, start + 1), False

    return None, False


def _locate_toc_headings_in_page(
    paras: list[dict] | None,
    toc_pool: list[tuple[str, int, str, str | None, str | None]],
) -> list[dict]:
    """
    Anchor TOC entries to inline body lines when the label appears in the
    text (``اسمُه:`` vs TOC ``اسمه``).  Unmatched entries and partial
    substring matches that sit after other page content go to the page top.

    Key ordering invariant: TOC entries must appear in the rendered output in
    the same order they appear in the TOC (parent before child).  When a
    parent entry has no text match (pending) but its child IS matched, the
    pending parents are placed inline *just before* the child — not at
    top_slot — so they render in the correct parent→child order.

    The page_top / top_slot mechanism is reserved for entries that precede
    all body text (cursor == 0 when they flush AND the child also starts at
    cursor == 0), or for entries that could not be matched at all and whose
    pending flush was triggered by the end of the toc_pool loop.
    """
    flat_lines: list[tuple[int, int, str]] = []
    for pi, para in enumerate(paras or []):
        if para.get("type") == "hamesh":
            continue
        for li, line in enumerate(para.get("lines", [])):
            stripped = _bracket_stripped(line)
            if stripped is not None:
                # Include bracket headings using their stripped text so that
                # TOC entries (e.g. "والعصر") can match the inline bracket
                # [والعصر] at its real position rather than falling through
                # to an implicit page-top heading placed before prior content.
                norm = _normalize_ar(stripped)
            else:
                norm = _normalize_ar(line)
            if norm:
                flat_lines.append((pi, li, norm))

    resolved: list[dict] = []
    pending: list[tuple[str, int, str, str | None, str | None]] = []
    top_slot = 0
    cursor = 0

    def _append_top(entry: dict):
        nonlocal top_slot
        entry["positions"] = [(0, top_slot)]
        entry["at_page_top"] = True
        resolved.append(entry)
        top_slot += 1

    def _flush_pending_as_top():
        """Flush pending entries to page-top (truly unmatched, end-of-pool)."""
        nonlocal pending
        for label, level, number, parent_text, parent_number in pending:
            _append_top(
                {
                    "text": label,
                    "level": level,
                    "number": number,
                    "matched": False,
                    "auto": False,
                    "implicit": True,
                    "keep_line": False,
                    "suppress": [],
                    "toc_parent_text": parent_text,
                    "_uid": number,
                    "toc_parent_uid": parent_number,
                }
            )
        pending = []

    def _flush_pending_before_child(child_positions: list[tuple[int, int]]):
        """
        Flush pending (unmatched parent) entries inline, positioned just
        before the child's first line.  This keeps parent→child order in the
        rendered document even when the parent text was never present on the
        page (e.g. it was a Shamela [+] collapsible toggle, not real text).

        If the child itself is at the very start of the page (first real
        line, cursor == 0) we use top_slot so that ALL pre-body headings
        cluster together at the top — but only when no body text has been
        consumed yet.
        """
        nonlocal pending, top_slot
        if not pending:
            return
        child_pi, child_li = child_positions[0]
        # If the child is at the page top (no body lines consumed before it),
        # treat parents as top-slot entries too — they'll render before the child.
        if (
            cursor == 0
            and child_pi == flat_lines[0][0]
            and child_li == flat_lines[0][1]
        ):
            for label, level, number, parent_text, parent_number in pending:
                _append_top(
                    {
                        "text": label,
                        "level": level,
                        "number": number,
                        "matched": False,
                        "auto": False,
                        "implicit": True,
                        "keep_line": False,
                        "suppress": [],
                        "toc_parent_text": parent_text,
                        "_uid": number,
                        "toc_parent_uid": parent_number,
                    }
                )
        else:
            for idx, (label, level, number, parent_text, parent_number) in enumerate(
                pending
            ):
                resolved.append(
                    {
                        "positions": [(child_pi, child_li)],
                        "text": label,
                        "level": level,
                        "number": number,
                        "matched": False,
                        "auto": False,
                        "implicit": True,
                        "keep_line": False,
                        "suppress": [],
                        "at_page_top": False,
                        "before_child": True,
                        "_before_child_slot": idx,
                        "toc_parent_text": parent_text,
                        "_uid": number,
                        "toc_parent_uid": parent_number,
                    }
                )
        pending = []

    for label, level, number, parent_text, parent_number in toc_pool:
        norm_label = _normalize_ar(label)
        found_span, keep_line = _find_label_in_lines(norm_label, flat_lines, cursor)
        if not found_span:
            # Search from page start — handles children whose text
            # appears before their parent's position on the page
            # (e.g. ملخص البحث before المقدمة in book 909).
            found_span, keep_line = _find_label_in_lines(norm_label, flat_lines, 0)
            if found_span and found_span[0] >= cursor:
                found_span = None  # only accept matches before cursor
        if found_span:
            start, end = found_span
            positions = [
                (flat_lines[i][0], flat_lines[i][1]) for i in range(start, end)
            ]

            # Post-check: determine keep_line based on actual extra content.
            # The _find_label_in_lines return value can be wrong when
            # punctuation like "-" inflates the raw line length even though
            # the only real extra content is trivial (e.g. a period).
            if end - start == 1:
                pi, li = positions[0]
                raw_line = (
                    (paras[pi].get("lines") or [])[li]
                    if paras and pi < len(paras)
                    else ""
                )
                if raw_line:
                    keep_line = _has_extra_content(raw_line, norm_label)
                    plain_line = _plain_text(raw_line)
                    is_bracket_heading = _bracket_stripped(raw_line) is not None
                    if is_bracket_heading:
                        has_fn = False
                    else:
                        has_fn = bool(re.search(r"\([\u0660-\u0669\d\s،,.\-\/أ-ي]+\)", plain_line) or 
                                      re.search(r"\[[\u0660-\u0669\d\s،,.\-\/أ-ي]+\]", plain_line))
                else:
                    has_fn = False
            else:
                raw_line = ""
                has_fn = False

            # Only hoist to page-top when the heading is literally the first
            # content on the page (start == cursor == 0).
            # "start > cursor and cursor == 0" must NOT hoist: body text before
            # the heading belongs to the continued prior section and must render
            # first; hoisting caused headings to appear inside the previous
            # section's content in the PDF.
            if start == 0 and cursor == 0:
                _flush_pending_as_top()
                h_obj = {
                    "text": label,
                    "level": level,
                    "number": number,
                    "matched": True,
                    "auto": False,
                    "implicit": False,
                    "keep_line": keep_line,
                    "suppress": [list(p) for p in positions],
                    "toc_parent_text": parent_text,
                    "_uid": number,
                    "toc_parent_uid": parent_number,
                }
                if raw_line and has_fn and not keep_line:
                    h_obj["raw_line"] = _strip_title_brackets(raw_line, label)
                _append_top(h_obj)
            else:
                # Heading found mid-page (after prior body text or a prior
                # heading).  Keep inline so content flows between sections.
                _flush_pending_before_child(positions)
                h_obj = {
                    "positions": positions,
                    "text": label,
                    "level": level,
                    "number": number,
                    "matched": True,
                    "auto": False,
                    "implicit": False,
                    "keep_line": keep_line,
                    "suppress": [],
                    "at_page_top": False,
                    "toc_parent_text": parent_text,
                    "_uid": number,
                    "toc_parent_uid": parent_number,
                }
                if raw_line and has_fn and not keep_line:
                    h_obj["raw_line"] = _strip_title_brackets(raw_line, label)
                resolved.append(h_obj)
            cursor = end
        else:
            pending.append((label, level, number, parent_text, parent_number))

    _flush_pending_as_top()

    # ── Post-pass: fix parent/child ordering when a matched heading was placed
    # inline at position (first_line) but its children ended up at page-top.
    # This happens when a TOC parent spans the first N paragraphs (matched,
    # start==cursor==0 → goes inline) but its child has no text match (→ top).
    # Page-top headings render before ALL inline content, so the child would
    # appear before the parent in the PDF.  Fix: any inline heading anchored
    # at the very first body line gets promoted to page-top, placed before its
    # page-top children.
    if flat_lines and any(h.get("at_page_top") for h in resolved):
        first_pi, first_li, _ = flat_lines[0]
        promoted = []
        remaining = []
        for h in resolved:
            if (
                not h.get("at_page_top")
                and not h.get("before_child")
                and h["positions"][0] == (first_pi, first_li)
            ):
                promoted.append(h)
            else:
                remaining.append(h)
        if promoted:
            n = len(promoted)
            # Shift existing top-slot li values up by n to make room
            for h in remaining:
                if h.get("at_page_top"):
                    pi, li = h["positions"][0]
                    h["positions"] = [(pi, li + n)]
            for slot, h in enumerate(promoted):
                h["positions"] = [(0, slot)]
                h["at_page_top"] = True
                # Do NOT clear suppress here — the original matched line
                # positions must stay suppressed so _extract_bracket_headings
                # doesn't re-emit the same label as an auto heading.
            resolved = promoted + remaining

    return resolved


def resolve_book_headings(meta: dict, pages_iter):
    """
    Enrich each page with toc_breadcrumb, toc_number, resolved_headings,
    and unanchored_toc_entries (TOC labels with no text match on the page).
    """
    toc_flat = meta.get("toc_flat", [])

    _stack: list[tuple[int, int]] = []
    # Precompute TOC parent label for each entry from the tree hierarchy
    toc_parent_map: dict[str, str | None] = {}
    for i, entry in enumerate(toc_flat):
        lvl = entry.get("level", 0)
        if not entry.get("label"):
            continue
        while _stack and _stack[-1][0] >= lvl:
            _stack.pop()
        pid = entry.get("page_id")
        if pid is not None:
            for plvl, pi in reversed(_stack):
                parent = toc_flat[pi]
                parent_pid = parent.get("page_id")
                if parent_pid is None:
                    parent["page_id"] = pid
                elif pid < parent_pid:
                    parent["page_id"] = pid
                else:
                    break
        toc_parent_map[entry["label"]] = (
            toc_flat[_stack[-1][1]].get("label") if _stack else None
        )
        _stack.append((lvl, i))

    sorted_toc = sorted(
        [e for e in toc_flat if e.get("page_id") is not None],
        key=lambda e: e["page_id"],
    )
    toc_idx = 0
    breadcrumb_stack: list[tuple[str, str, int]] = []
    counters = [0] * 20

    for page in pages_iter:
        # url_page_id is the Shamela URL sequence ID (same namespace as TOC
        # page_id).  page_id is the printed page number from data-page-id —
        # a different namespace that must NOT be used for TOC matching.
        # Fall back to page_id only for legacy data that pre-dates this fix.
        pid = page.get("url_page_id") or page.get("page_id")
        page_toc_new: list[tuple[str, int, str, str | None, str | None]] = []

        if pid is not None:
            while toc_idx < len(sorted_toc) and sorted_toc[toc_idx]["page_id"] <= pid:
                entry = sorted_toc[toc_idx]
                level = entry["level"]
                while breadcrumb_stack and breadcrumb_stack[-1][2] >= level:
                    breadcrumb_stack.pop()
                counters[level] += 1
                for i in range(level + 1, len(counters)):
                    counters[i] = 0
                number = ".".join(str(counters[i]) for i in range(level + 1))
                label = entry["label"]
                if label:
                    label = label.strip()
                    parent_text = toc_parent_map.get(label)
                    parent_number = (
                        breadcrumb_stack[-1][0] if breadcrumb_stack else None
                    )
                    breadcrumb_stack.append((number, label, level))
                    page_toc_new.append(
                        (label, level, number, parent_text, parent_number)
                    )
                toc_idx += 1

        if page_toc_new:
            labels = [l for l, _, _, _, _ in page_toc_new]
            numbers = [n for _, _, n, _, _ in page_toc_new]
            page["toc_breadcrumb"] = " • ".join(labels)
            page["toc_number"] = (
                f"{numbers[0]}-{numbers[-1]}" if len(numbers) > 1 else numbers[0]
            )
        elif breadcrumb_stack:
            page["toc_number"] = breadcrumb_stack[-1][0]
            page["toc_breadcrumb"] = breadcrumb_stack[-1][1]
        else:
            page["toc_number"] = ""
            page["toc_breadcrumb"] = ""

        located = _locate_toc_headings_in_page(page.get("paragraphs"), page_toc_new)
        consumed_positions: set[tuple[int, int]] = set()
        for h in located:
            for pos in h.get("suppress", []):
                consumed_positions.add(tuple(pos))

        # Pre-extract bracket heading positions BEFORE echo suppression so
        # bracket-detected headings are never consumed by echo suppression
        # (which would prevent the heading from being rendered).  Without this,
        # a line like "[الفصل الثالث]" that appears twice on the same page would
        # be echo-suppressed on the second occurrence and never rendered as a
        # heading, leaving a gap in the output.
        bracket_headings = _extract_bracket_headings(page.get("paragraphs"))
        bracket_positions: set[tuple[int, int]] = set()
        for bh in bracket_headings:
            bracket_positions.add((bh["para_idx"], bh["line_idx"]))

        # Also suppress any plain-text line whose normalized form matches an
        # already-resolved heading label — these are unbracketed echoes of
        # bracket headings (e.g. "الأصْلُ الثَّانِي" after "[الأصل الثاني...]").
        # Exception: never echo-suppress a line that keep_line marked as having
        # extra content (footnote refs etc.) — those must stay visible.
        # Also skip positions already flagged as bracket headings — they have
        # their own rendering path below.
        resolved_norms: set[str] = {_normalize_ar(h["text"]) for h in located}
        keep_line_positions: set[tuple[int, int]] = {
            tuple(pos)
            for h in located
            if h.get("keep_line")
            for pos in h.get("positions", [])
        }
        echo_suppressed: list[list[int]] = []
        paras_here = page.get("paragraphs") or []
        for pi, para in enumerate(paras_here):
            if para.get("type") == "hamesh":
                continue
            for li, line in enumerate(para.get("lines", [])):
                if (pi, li) in consumed_positions:
                    continue
                if (pi, li) in keep_line_positions:
                    continue
                if (pi, li) in bracket_positions:
                    continue
                if _normalize_ar(line) in resolved_norms:
                    consumed_positions.add((pi, li))
                    echo_suppressed.append([pi, li])

        # Collect normalized texts of all already-resolved TOC headings so
        # we can skip bracket headings that duplicate them.  Prevents a TOC
        # entry whose label isn't found in the text (implicit) from being
        # echoed a second time via bracket auto-detection of the same text.
        located_norms: set[str] = {_normalize_ar(h["text"]) for h in located}

        resolved: list[dict] = list(located)
        for bh in bracket_headings:
            pos = (bh["para_idx"], bh["line_idx"])
            if pos in consumed_positions:
                continue
            if _normalize_ar(bh["text"]) in located_norms:
                # Bracket heading duplicates a located TOC heading — suppress
                # the bracket line so it doesn't appear as a bare unnumbered
                # paragraph alongside the numbered heading.
                consumed_positions.add(pos)
                for h in resolved:
                    if not h.get("auto") and _normalize_ar(h["text"]) == _normalize_ar(
                        bh["text"]
                    ):
                        if pos not in {tuple(p) for p in h.get("suppress", [])}:
                            h.setdefault("suppress", []).append(list(pos))
                        break
                continue
            # Bracket heading not in TOC — treat as body text, not a heading.
            # Its position is NOT added to consumed_positions so the line
            # renders normally as body text rather than being echo-suppressed.
            continue

        # Mark headings whose first position coincides with a bracket heading
        # as "bracket_matched" — these are real content headings, not just
        # TOC structural labels. Used by the renumber function.
        for h in resolved:
            if not h.get("auto") and not h.get("implicit"):
                if tuple(h["positions"][0]) in bracket_positions:
                    h["bracket_matched"] = True

        resolved.sort(key=lambda h: h["positions"][0])

        # Same-page rendering fix: when a child appears before its TOC parent
        # in content order, but the parent is NOT a real content heading
        # (not bracket_matched), promote BOTH to page-top with parent first.
        # Walk backwards so nested moves don't break indices.
        for i in range(len(resolved) - 1, -1, -1):
            h = resolved[i]
            parent_uid = h.get("toc_parent_uid")
            if not parent_uid:
                continue
            for j, ph in enumerate(resolved):
                if (
                    ph.get("_uid") == parent_uid
                    and not ph.get("bracket_matched")
                    and not ph.get("auto")
                    and not ph.get("implicit")
                ):
                    if i < j:
                        # Child before parent → promote both to page-top,
                        # parent before child
                        ph["at_page_top"] = True
                        h["at_page_top"] = True
                        resolved.insert(i, resolved.pop(j))
                    break

        page["resolved_headings"] = resolved
        page["echo_suppressed_positions"] = echo_suppressed  # render path uses this
        unanchored = [
            {"label": h["text"], "level": h["level"], "number": h["number"]}
            for h in resolved
            if h.get("implicit")
        ]
        page["unanchored_toc_entries"] = unanchored

        if unanchored:
            titles = ", ".join(f'"{u["number"]}. {u["label"]}"' for u in unanchored)
            print(f"  [implicit] page {pid}: {titles}")

        yield page


def materialize_resolved_pages(
    meta: dict, pages_path: Path, resolved_path: Path
) -> int:
    count = 0
    all_implicits: list[dict] = []
    with open(resolved_path, "w", encoding="utf-8") as out_fh:
        for page in resolve_book_headings(meta, iter_pages_jsonl(pages_path)):
            out_fh.write(_fast_json(page, indent=False) + "\n")
            count += 1
            pid = page.get("url_page_id") or page.get("page_id")
            for u in (page.get("unanchored_toc_entries") or []):
                all_implicits.append({
                    "page": pid,
                    "number": u["number"],
                    "label": u["label"],
                })
    if all_implicits:
        implicits_path = resolved_path.with_name("implicits.json")
        with open(implicits_path, "w", encoding="utf-8") as f:
            json.dump(all_implicits, f, ensure_ascii=False, indent=2)
        print(f"    → {len(all_implicits)} implicit headings saved → {implicits_path}")
    return count


def _renumber_headings_by_document_order(resolved_path: Path) -> None:
    """Renumber headings by content order when a real parent is
    bracket_matched and on a different page from its child.

    Books without any bracket_matched parent heading keep their original
    TOC-based numbering (no change).
    """
    pages: list[dict] = []
    with open(resolved_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                pages.append(json.loads(line))

    # Guard: renumber when any heading's TOC parent is bracket_matched AND
    # the heading appears BEFORE its parent in content order (i.e. the TOC
    # hierarchy doesn't match content flow).  The rendering-order fix in
    # resolve_book_headings handles same-page reordering, but it cannot
    # fix numbers when a heading's parent relationship is inconsistent
    # with the actual content order.
    heading_by_uid: dict[str, tuple[int, dict]] = {}
    for page_idx, page in enumerate(pages):
        for h in page.get("resolved_headings", []):
            uid = h.get("_uid")
            if uid:
                heading_by_uid[uid] = (page_idx, h)
    needs_renumber = False

    # Check 1: parent-child inversion — child appears before its parent in
    # content order but the TOC hierarchy says parent comes first.
    for page_idx, page in enumerate(pages):
        for h in page.get("resolved_headings", []):
            parent_uid = h.get("toc_parent_uid")
            if not parent_uid:
                continue
            parent_info = heading_by_uid.get(parent_uid)
            if not parent_info:
                continue
            pp_idx, ph = parent_info
            if not ph.get("bracket_matched") or ph.get("implicit"):
                continue
            child_pos = (page_idx, h["positions"][0][0], h["positions"][0][1])
            parent_pos = (pp_idx, ph["positions"][0][0], ph["positions"][0][1])
            if child_pos < parent_pos:
                needs_renumber = True
                break
        if needs_renumber:
            break

    # Check 2: sibling inversion — headings at the same level appear in a
    # different order in the content vs the TOC (common when Shamela's TOC
    # lists subtitles in wrong sequence but the actual page content is
    # correct).  Compare consecutive non-auto headings in content order:
    # if a later heading has a numerically smaller TOC number than an
    # earlier one, the TOC order is wrong and renumbering is needed.
    # Implicit headings are included: they can be TOC-placed entries that
    # appear at a page position before their numbered siblings.
    if not needs_renumber:
        valid: list[dict] = []
        for page_idx, page in enumerate(pages):
            for h in page.get("resolved_headings", []):
                if not h.get("auto"):
                    valid.append((page_idx, h))
        for i in range(len(valid) - 1):
            _, h_a = valid[i]
            _, h_b = valid[i + 1]
            na = [int(x) for x in h_a["number"].split(".")]
            nb = [int(x) for x in h_b["number"].split(".")]
            if nb < na:
                needs_renumber = True
                break

    if not needs_renumber:
        return  # preserve original numbering

    # ── Cross-page implicit-auto heading matching ───────────────────────
    # A TOC heading on page X may have its actual bracket heading on page
    # Y (when Shamela's TOC page pointer is off).  Match them here so the
    # heading appears at the correct content position instead of creating
    # two separate entries (an implicit at the wrong page + an auto orphan).
    implicit_by_norm: dict[str, list[tuple[int, dict]]] = {}
    auto_by_norm: dict[str, list[tuple[int, dict]]] = {}
    for page_idx, page in enumerate(pages):
        for h in page.get("resolved_headings", []):
            norm = _normalize_ar(h["text"])
            if h.get("implicit") and not h.get("auto"):
                implicit_by_norm.setdefault(norm, []).append((page_idx, h))
            elif h.get("auto") and not h.get("implicit"):
                auto_by_norm.setdefault(norm, []).append((page_idx, h))

    for norm, auto_entries in auto_by_norm.items():
        implicit_entries = implicit_by_norm.get(norm)
        if not implicit_entries:
            continue
        for auto_page_idx, auto_h in auto_entries:
            # Find the nearest implicit heading on a DIFFERENT page
            best = None
            best_dist = None
            for imp_page_idx, imp_h in implicit_entries:
                if imp_page_idx == auto_page_idx:
                    continue
                dist = abs(imp_page_idx - auto_page_idx)
                if best_dist is None or dist < best_dist:
                    best = (imp_page_idx, imp_h)
                    best_dist = dist
            if best is None:
                continue
            imp_page_idx, imp_h = best
            auto_pos = auto_h["positions"][0]
            # Move the implicit heading object from its original page to
            # the auto heading's page so document-order sorting uses the
            # correct page index.
            imp_page = pages[imp_page_idx]
            imp_page["resolved_headings"] = [
                h for h in imp_page["resolved_headings"] if h is not imp_h
            ]
            auto_page = pages[auto_page_idx]
            auto_page["resolved_headings"].append(imp_h)
            # Set its position to the auto heading's position (where the
            # actual content starts).  Replace the position list entirely
            # — the old TOC position from the original page would be
            # meaningless (and harmful) on the new page because it would
            # suppress a real paragraph at that slot.
            imp_h["positions"] = [auto_pos]
            # Clear page-top flag — the heading now has a real inline
            # position within the page's paragraph flow so it renders
            # after preceding paragraphs (e.g. continuation of the
            # previous section).
            imp_h["at_page_top"] = False
            # Suppress the auto heading's bracket line so it doesn't
            # render as a bare paragraph.
            auto_pos_list = list(auto_pos)
            if auto_pos_list not in imp_h.get("suppress", []):
                imp_h.setdefault("suppress", []).append(auto_pos_list)
            # Remove the auto heading from its page
            auto_page = pages[auto_page_idx]
            auto_page["resolved_headings"] = [
                h for h in auto_page["resolved_headings"] if h is not auto_h
            ]

    flat: list[tuple[int, int, int, int, dict]] = []
    for page_idx, page in enumerate(pages):
        for h in page.get("resolved_headings", []):
            pos = h["positions"][0]
            flat.append((page_idx, pos[0], pos[1], h["level"], h))
    if not flat:
        return

    flat.sort(key=lambda x: (x[0], x[1], x[2]))

    # Build heading-by-uid lookup so we can find a heading's parent
    heading_by_uid: dict[str, tuple[int, dict]] = {}
    for page_idx, pi, li, level, heading in flat:
        uid = heading.get("_uid")
        if uid:
            heading_by_uid[uid] = (page_idx, heading)

    # Prepass: promote children that appear before their bracket_matched
    # parent on the SAME page in content order.  These children are demoted
    # in the TOC hierarchy but their content position shows they should be
    # siblings, not children.  Only promote same-page — cross-page is
    # handled by page_id propagation in resolve_book_headings.
    # Never promote auto/implicit headings to root level.
    for idx, (page_idx, pi, li, level, heading) in enumerate(flat):
        parent_uid = heading.get("toc_parent_uid")
        if not parent_uid or level == 0:
            continue
        parent_info = heading_by_uid.get(parent_uid)
        if not parent_info:
            continue
        pp_idx, ph = parent_info
        if not ph.get("bracket_matched") or ph.get("implicit"):
            continue
        if pp_idx != page_idx:
            continue
        parent_pos = (pp_idx, ph["positions"][0][0], ph["positions"][0][1])
        child_pos = (page_idx, pi, li)
        if child_pos < parent_pos:
            parent_level = ph["level"]
            if parent_level >= level:
                continue
            # Never promote auto/implicit headings to root level
            if parent_level == 0 and (heading.get("auto") or heading.get("implicit")):
                continue
            heading["_level_promoted"] = True
            heading["level"] = parent_level
            heading["toc_parent_uid"] = ph.get("toc_parent_uid")
            heading["toc_parent_text"] = ph.get("toc_parent_text")
            flat[idx] = (page_idx, pi, li, parent_level, heading)
            break

    # Sort roots first so children always see their parent's final renumbered
    # number (avoids orphaned references when a child appears before its
    # parent in content order — only possible with UID-based lookup).
    flat.sort(key=lambda x: (x[3], x[0], x[1], x[2]))  # level, page_idx, pi, li

    counters = [0] * 20
    stack: list[tuple[int, str]] = []  # (level, uid)
    # Track last counter values per parent (by uid) so re-entering a parent
    # continues from where it left off.
    parent_last_counts: dict[str, list[int]] = {}

    for page_idx, pi, li, orig_level, heading in flat:
        heading_uid = heading.get("_uid") or ""
        heading_parent_uid = heading.get("toc_parent_uid")

        if heading["level"] == 0:
            stack = [(0, heading_uid)]
            parent_parts = [int(x) for x in heading["number"].split(".")]
            if heading.get("_level_promoted"):
                counters[0] += 1
                heading["number"] = str(counters[0])
            else:
                old_c0 = counters[0]
                for i, val in enumerate(parent_parts):
                    counters[i] = val
                for i in range(len(parent_parts), len(counters)):
                    counters[i] = 0
                if old_c0 >= parent_parts[0] and old_c0 > 0:
                    counters[0] = old_c0 + 1
                    heading["number"] = str(counters[0])
            if heading_uid not in parent_last_counts:
                parent_last_counts[heading_uid] = list(counters)
            continue

        # Pop stack entries at same or deeper level
        while stack and stack[-1][0] >= heading["level"]:
            stack.pop()

        # If the remaining stack top's uid doesn't match this heading's TOC
        # parent, it's not a genuine parent — continue popping.
        while stack and stack[-1][1] != heading_parent_uid:
            stack.pop()

        # Determine effective level and optionally load parent counters.
        parent_in_stack = bool(
            heading_parent_uid and stack and stack[-1][1] == heading_parent_uid
        )
        if parent_in_stack:
            eff_level = stack[-1][0] + 1
        elif heading_parent_uid:
            # Parent not in stack — look it up and load its counters
            # so children from different TOC parents get independent
            # numbering even if interleaved in content order.
            parent_info = heading_by_uid.get(heading_parent_uid)
            if parent_info:
                parent_heading = parent_info[1]
                parent_num = parent_heading.get("number", "")
                if parent_num:
                    parent_parts = [int(x) for x in parent_num.split(".")]
                    if heading_parent_uid in parent_last_counts:
                        counters = list(parent_last_counts[heading_parent_uid])
                    else:
                        for i, val in enumerate(parent_parts):
                            counters[i] = val
                        for i in range(len(parent_parts), len(counters)):
                            counters[i] = 0
                    parent_level = len(parent_parts) - 1
                    stack = [(pl, lv) for pl, lv in stack if pl <= parent_level]
                    if not stack or stack[-1][0] < parent_level:
                        stack.append((parent_level, heading_parent_uid))
                    eff_level = stack[-1][0] + 1
                else:
                    eff_level = stack[-1][0] + 1 if stack else 0
            else:
                eff_level = stack[-1][0] + 1 if stack else 0
        else:
            eff_level = stack[-1][0] + 1 if stack else 0

        counters[eff_level] += 1
        for i in range(eff_level + 1, len(counters)):
            counters[i] = 0

        heading["number"] = ".".join(str(counters[i]) for i in range(eff_level + 1))
        stack.append((eff_level, heading_uid))

        # Save counter state for the current parent
        if heading_parent_uid and heading_parent_uid in heading_by_uid:
            parent_last_counts[heading_parent_uid] = list(counters)

    # Sync unanchored_toc_entries numbers to match renumbered headings
    for page in pages:
        for ue in page.get("unanchored_toc_entries", []):
            for h in page.get("resolved_headings", []):
                if (
                    h.get("implicit")
                    and h["text"] == ue["label"]
                    and h["level"] == ue["level"]
                ):
                    ue["number"] = h["number"]
                    break

    with open(resolved_path, "w", encoding="utf-8") as f:
        for page in pages:
            f.write(_fast_json(page, indent=False) + "\n")


def _render_page_html(
    page: dict,
    toc_by_page: dict,
    vol_boundaries: set,
    vol_num_ref: list,
    max_toc_level: int = 3,
) -> tuple[str, int]:
    parts = []
    pid = page.get("page_id")
    vol_num = vol_num_ref[0]
    page_breadcrumb = page.get("toc_breadcrumb", "")
    page_toc_number = page.get("toc_number", "")
    resolved_headings = page.get("resolved_headings") or []

    if page_breadcrumb:
        parts.append(
            f'<div class="bm-setter" '
            f'style="position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;overflow:hidden;font-size:0;string-set:chapter-title content()">'
            f"{_e(page_breadcrumb)}</div>\n"
        )
    if page_toc_number:
        parts.append(
            f'<div class="bm-setter" '
            f'style="position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;overflow:hidden;font-size:0;string-set:toc-number content()">'
            f"{_e(page_toc_number)}</div>\n"
        )

    paras = page.get("paragraphs")
    # heading_by_start: position → ordered list of headings at that position.
    # A position can hold multiple headings when an unmatched parent is placed
    # "before_child" at the same (pi, li) as its matched child.
    heading_by_start: dict[tuple[int, int], list[dict]] = {}
    suppressed_positions: set[tuple[int, int]] = set()

    page_start_headings: list[dict] = []
    for h in resolved_headings:
        pi, li = h["positions"][0]
        for pos in h.get("suppress", []):
            if h.get("at_page_top") and h.get("keep_line") and tuple(pos) == (pi, li):
                continue
            suppressed_positions.add(tuple(pos))
        if h.get("at_page_top"):
            page_start_headings.append(h)
        else:
            heading_by_start.setdefault((pi, li), []).append(h)
        for pos in h["positions"][1:]:
            suppressed_positions.add(tuple(pos))

    # Echo-suppressed: plain-text lines that duplicate a resolved heading label
    for pos in page.get("echo_suppressed_positions") or []:
        suppressed_positions.add(tuple(pos))

    # Sort each position bucket: before_child parents first (by slot), then child
    for pos, hlist in heading_by_start.items():
        hlist.sort(
            key=lambda h: (
                0 if h.get("before_child") else 1,
                h.get("_before_child_slot", 0),
            )
        )

    # ── Render every heading visibly with per-level color ──────
    # All matched headings render as visible styled headings.
    # For keep_line=True: body line has extra text beyond the label,
    #   so the body shows as well (with Shamela's teal <span class="c5">
    #   stripped to avoid duplicate coloring).
    # For keep_line=False: body IS the label → suppress the body line.
    # implicit/auto/before_child → artificial placement; text differs
    #   from the body at that position → body shows as-is.

    def _heading_display_text(text: str) -> str:
        """Strip outer brackets from heading text for clean display,
        e.g. ``[التخامة في القبلة وفي الصلاة]`` → ``التخامة في القبلة وفي الصلاة``."""
        t = text.strip()
        if t.startswith("[") and t.endswith("]"):
            return t[1:-1].strip()
        return t

    def _heading_html(h: dict) -> str:
        num = h.get("number", "")
        level = h["level"]
        nh_level = h.get("_nh_level", level)
        display = _heading_display_text(h["text"])
        shape = _HEADING_SHAPES.get(nh_level, "")
        shape_prefix = f'<span class="heading-shape">{shape}</span> ' if shape else ""
        if h.get("auto"):
            label_text = _e(display)
            bm_label = f"{num}. {label_text}" if num else label_text
            return (
                f'<p class="toc-bookmark-anchor toc-bm-{nh_level}">{bm_label}</p>\n'
                f'<p class="toc-numbered-heading toc-nh-{nh_level}">{shape_prefix}{label_text}</p>\n'
            )
        if h.get("implicit"):
            if h.get("_skip_outline"):
                return ""
            label_text = f"{num}. {_e(display)}" if num else _e(display)
            return f'<p class="toc-bookmark-anchor toc-bm-{nh_level}">{label_text}</p>\n'
        
        # Use raw_line (containing footnote/spans) if available
        if h.get("raw_line"):
            label_html = h['raw_line']
        else:
            label_html = _e(display)
            
        bm_label = f"{num}. {_e(display)}" if num else _e(display)
        return (
            f'<p class="toc-bookmark-anchor toc-bm-{nh_level}">{bm_label}</p>\n'
            f'<p class="toc-numbered-heading toc-nh-{nh_level}">{shape_prefix}{label_html}</p>\n'
        )

    def _color_delimited_text(text: str) -> str:
        """Wrap text between `{` and `}` or `﴾` and `﴿` in colored span."""
        dcol = STYLE_CFG.get("delimiter_colors", DEFAULT_STYLE_CFG["delimiter_colors"])
        brace_col = dcol.get("brace", "#FF0000")
        verse_col = dcol.get("verse", "#FF0000")
        text = re.sub(
            r"(\{[^}]*\})",
            f'<span style="color:{brace_col}">\\1</span>',
            text,
        )
        text = re.sub(
            r"(\uFD3E[^\uFD3E\uFD3F]*\uFD3F|\uFD3F[^\uFD3E\uFD3F]*\uFD3E)",
            f'<span style="color:{verse_col}">\\1</span>',
            text,
        )
        return text

    def _strip_body_color(body_line: str) -> str:
        """Strip leading Shamela teal span (c5 class or #005c81) from body line."""
        import re

        cleaned = re.sub(
            r'^<span[^>]*(?:class="?c5"?|color:#005c81)[^>]*>.*?</span>\s*',
            "",
            body_line,
            count=1,
        ).strip()
        return cleaned

    def _strip_heading_prefix(body: str, heading_text: str) -> str:
        """Strip the heading label from the start of the body text (if present)
        so the body doesn't repeat the number and first words."""
        ht = heading_text.strip()
        if body.startswith(ht):
            remaining = body[len(ht) :].strip().lstrip(" ,.-،:")
            return remaining
        # Try normalized comparison for minor differences (commas, shadda, etc.)
        nbody = _normalize_ar(body)
        nht = _normalize_ar(ht)
        if nbody.startswith(nht):
            # Walk raw body char by char, skipping characters that
            # normalise away (e.g. tashkeel, punctuation), and skipping
            # spaces in the normalised heading (they come from punctuation
            # that was normalised away), matching the rest in order.
            i = 0
            matched = 0
            while i < len(body) and matched < len(nht):
                if nht[matched] == " ":
                    matched += 1
                    continue
                chunk = _normalize_ar(body[i])
                if chunk:
                    if chunk[0] == nht[matched]:
                        matched += 1
                    else:
                        return body
                i += 1
            remaining = body[i:].strip().lstrip(" ,.-،:")
            return remaining
        return body

    entry_open = [False]

    def _open_entry():
        if not entry_open[0]:
            parts.append('<div class="page-entry"><div class="page-text">')
            entry_open[0] = True

    def _close_if_open():
        if entry_open[0]:
            parts.append("</div></div>")
            entry_open[0] = False

    has_body = paras or page.get("text")
    if has_body:
        _open_entry()
        for h in page_start_headings:
            parts.append(_heading_html(h))

    if paras:
        for pi, para in enumerate(paras):
            ptype = para["type"]
            if ptype == "hamesh":
                parts.append('<div class="hamesh">')
                for line in para["lines"]:
                    if line.strip():
                        parts.append(f"<p>{_color_delimited_text(line.strip())}</p>")
                parts.append("</div>")
                continue

            for li, line in enumerate(para["lines"]):
                if not line.strip():
                    continue
                headings_here = heading_by_start.get((pi, li))
                if headings_here:
                    for heading in headings_here:
                        parts.append(_heading_html(heading))
                    real_headings = [
                        h
                        for h in headings_here
                        if not (h.get("implicit") or h.get("before_child"))
                    ]
                    if real_headings:
                        if all(
                            not h.get("keep_line", not h.get("auto", False))
                            for h in real_headings
                        ):
                            pass  # body IS the label → suppress
                        else:
                            body_line = _strip_body_color(line.strip())
                            if body_line:
                                # Strip the heading label prefix so the body
                                # doesn't repeat the number and first words.
                                for rh in real_headings:
                                    stripped = _strip_heading_prefix(
                                        body_line, rh["text"]
                                    )
                                    if stripped and stripped != body_line:
                                        body_line = stripped
                                        break
                                if body_line:
                                    tag = (
                                        'p class="chapter-heading"'
                                        if ptype == "heading"
                                        else "p"
                                    )
                                    parts.append(
                                        f"<{tag}>{_color_delimited_text(body_line)}</{tag.split()[0]}>"
                                    )
                        continue
                    # All artificial headings → show body as-is
                    body_line = line.strip()
                    if body_line:
                        tag = 'p class="chapter-heading"' if ptype == "heading" else "p"
                        parts.append(f"<{tag}>{_color_delimited_text(body_line)}</{tag.split()[0]}>")
                    continue
                elif (pi, li) in suppressed_positions:
                    continue
                body_line = line.strip()
                if ptype == "heading":
                    parts.append(f'<p class="chapter-heading">{body_line}</p>')
                else:
                    parts.append(f"<p>{_color_delimited_text(body_line)}</p>")
    elif page.get("text"):
        for chunk in page["text"].split("\n\n"):
            if chunk.strip():
                parts.append(f"<p>{_e(chunk.strip())}</p>")

    _close_if_open()
    return "".join(parts), vol_num


def _ensure_resolved(meta: dict, pages_iter):
    pages_iter = iter(pages_iter)
    try:
        first = next(pages_iter)
    except StopIteration:
        return

    chained = itertools.chain([first], pages_iter)
    seen_ids: set[int] = set()

    if "resolved_headings" in first:
        pages = chained
    else:
        pages = resolve_book_headings(meta, chained)

    for page in pages:
        pid = page.get("url_page_id") or page.get("page_id")
        if pid is not None:
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
        yield page


def build_html_to_file(
    meta: dict,
    author_info: dict,
    pages_iter,
    out_path: Path,
    flush_every: int = 50,
    volume_label: str | None = None,
    columns: int = 1,
    fast: bool = False,
) -> None:
    """
    Stream-write the full HTML document to *out_path* one page at a time.
    Memory usage is O(flush_every pages) instead of O(all pages).

    pages_iter may be a list or any iterable of page dicts.
    flush_every: write buffer to disk every N pages (default 50).
    volume_label: when set (e.g. "1", "المقدمة"), this is a per-juz PDF —
                  show the juz label on the cover and skip volume dividers.
    columns: number of text columns per page (1 = normal, 2 = two-column
             newspaper-style layout for the book text).
    fast: drop kashida justification (major WeasyPrint speed win on big
          books) in favor of plain justify.
    """
    import datetime

    css = _build_html_css(meta, columns=columns, fast=fast)

    # Build page_id -> [(label, toc_level), ...] lookup
    toc_flat = meta.get("toc_flat", [])
    toc_by_page: dict = {}
    max_toc_level = -1
    for entry in toc_flat:
        lvl = entry.get("level", 0)
        if lvl > max_toc_level:
            max_toc_level = lvl
        pid = entry.get("page_id")
        if pid is not None:
            toc_by_page.setdefault(pid, []).append((entry.get("label", ""), lvl))

    if volume_label is None:
        vol_starts_list = sorted(meta.get("volume_start_pages", []))
        vol_boundaries = set(vol_starts_list)
        vol_labels_list = meta.get("volume_labels", [])
    else:
        vol_starts_list = []
        vol_boundaries = set()
        vol_labels_list = []
    vol_num = [1]  # mutable reference (1-indexed)

    scraped_date = datetime.date.today().strftime("%Y-%m-%d")

    with open(
        out_path, "w", encoding="utf-8", buffering=1 << 20
    ) as fh:  # 1 MB write buffer
        # ── DOCTYPE + CSS ──────────────────────────────────────────────
        fh.write(
            f'<!DOCTYPE html><html lang="ar" dir="rtl"><head>'
            f'<meta charset="UTF-8">'
            f"<style>{css}</style></head><body>\n"
        )

        # Hidden span: sets book-title and chapter-title to book title (fallback)
        book_title = meta.get("title", "كتاب")
        fh.write(
            f'<span style="display:none; string-set: book-title content(), chapter-title content()">'
            f"{_e(_truncate(book_title))}</span>\n"
        )
        fh.write(
            f'<span style="display:none; string-set: toc-number content()"></span>\n'
        )

        # ── COVER (ornate multi-layer design) ─────────────────────────
        fh.write('<div class="cover">\n')
        fh.write('<div class="cover-frame-outer">\n')
        fh.write('<div class="cover-frame-inner">\n')

        # Corner ornaments
        fh.write('<div class="cover-corner cover-corner-tl">&#10022;</div>\n')
        fh.write('<div class="cover-corner cover-corner-tr">&#10022;</div>\n')
        fh.write('<div class="cover-corner cover-corner-bl">&#10022;</div>\n')
        fh.write('<div class="cover-corner cover-corner-br">&#10022;</div>\n')

        # Top ornament row
        fh.write(
            '<div class="cover-ornament-top">&#10022; &#10023; &#10022; &#10023; &#10022;</div>\n'
        )

        # Bismillah
        fh.write('<div class="cover-bismillah">بِسْمِ اللَّهِ الرَّحْمَنِ الرَّحِيمِ</div>\n')

        # Decorative divider
        fh.write(
            '<div class="cover-divider">&#9473;&#9473;&#9473; &#10022; &#9473;&#9473;&#9473;</div>\n'
        )

        # Book title
        fh.write(f"<h1>{_e(meta.get('title', 'كتاب'))}</h1>\n")

        # Juz label on per-volume covers
        if volume_label:
            fh.write(
                f'<h2 style="text-align:center;color:#c9a84c;'
                f'margin:15px 0 5px 0;font-size:16pt;">'
                f"الجزء {_e(volume_label)}</h2>\n"
            )

        # Meta info table
        rows = []
        for label, key in [
            ("المؤلف", "author"),
            ("الناشر", "publisher"),
            ("الطبعة", "edition"),
            ("عدد الأجزاء", "volumes"),
        ]:
            if meta.get(key):
                rows.append(
                    f'<tr><td class="meta-label">{_e(label)}</td>'
                    f"<td>{_e(meta[key])}</td></tr>"
                )
        if rows:
            fh.write(f'<table class="meta-table">{"".join(rows)}</table>\n')

        # Bottom ornament
        fh.write(
            '<div class="cover-ornament-bottom">&#10022; &#10023; &#10022; &#10023; &#10022;</div>\n'
        )

        # Source
        fh.write(
            f'<p class="source">المصدر: {_e(meta.get("url", ""))} | '
            f"تاريخ التحميل: {scraped_date}</p>\n"
        )
        fh.write(
            '<p style="color:#c0392b;font-size:10pt;'
            'text-align:center;margin-top:10px;">'
            "مُستَخرَج من المكتبة الشاملة</p>\n"
        )

        fh.write("</div><!-- cover-frame-inner -->\n")
        fh.write("</div><!-- cover-frame-outer -->\n")
        fh.write("</div><!-- cover -->\n")

        # ── AUTHOR INFO ────────────────────────────────────────────────
        if author_info and not author_info.get("error") and author_info.get("bio"):
            fh.write('<div class="section-front">\n')
            fh.write("<h2>ترجمة المؤلف</h2>\n")
            if author_info.get("bio"):
                fh.write('<div class="author-bio">\n')
                for line in author_info["bio"].splitlines():
                    if line.strip():
                        fh.write(f"<p>{_e(line.strip())}</p>\n")
                fh.write("</div>\n")
            if author_info.get("url"):
                fh.write(
                    f'<p style="font-size:8pt;color:#aaa">'
                    f"المصدر: {_e(author_info['url'])}</p>\n"
                )
            fh.write("</div>\n")

        # ── BOOK PAGES ─────────────────────────────────────────────────
        fh.write('<div class="section-text">\n')
        fh.write("<h2>نص الكتاب</h2>\n")

        buf: list[str] = []
        first_page = True

        # First section (juz 1) — no page break, flows after نص الكتاب heading
        buf.append('<div class="section" style="page-break-before:auto;">\n')
        # If volumes exist, insert juz 1 label before first content
        if vol_starts_list and vol_labels_list:
            buf.append(
                f'<div class="volume-divider">'
                f"<h2>الجزء {_e(vol_labels_list[0])}</h2></div>\n"
            )

        for page in _ensure_resolved(meta, pages_iter):
            pid = page.get("page_id")

            if pid and pid in vol_boundaries and not first_page:
                buf.append("</div>\n")
                buf.append('<div class="section">\n')
                vol_label = (
                    vol_labels_list[vol_num[0]]
                    if vol_labels_list
                    else str(vol_num[0] + 1)
                )
                buf.append(
                    f'<div class="volume-divider">'
                    f"<h2>الجزء {_e(vol_label)}</h2></div>\n"
                )
                vol_num[0] += 1

            frag, _ = _render_page_html(
                page, toc_by_page, vol_boundaries, vol_num, max_toc_level
            )
            buf.append(frag)
            first_page = False

            if len(buf) >= flush_every:
                fh.write("".join(buf))
                buf.clear()

        if buf:
            fh.write("".join(buf))

        fh.write("</div>\n")
        fh.write("</body></html>\n")


def build_pdf(
    meta: dict,
    author_info: dict,
    pages_iter,
    out_path: str,
    flush_every: int = 50,
    volume_label: str | None = None,
    columns: int = 1,
    fast: bool = False,
):
    """
    Build a PDF from *pages_iter* (list or generator of page dicts).

    *volume_label* is forwarded to build_html_to_file for per-juz covers.
    *columns* selects the page layout: 1 (default) = single-column,
    2 = two-column layout for the book-text body.
    *fast* drops kashida justification for a large WeasyPrint speed win.

    Strategy (memory-safe):
      1. Stream-write HTML to <out_path>.html — O(flush_every) memory.
      2. Hand WeasyPrint a file:// URL instead of a string — it can
         parse/stream from disk rather than holding the whole HTML in RAM.
      3. The intermediate HTML file is kept for debugging; delete it
         manually if disk space is a concern.
    """
    html_path = Path(out_path).with_suffix(".html")

    print(f"  [html] streaming HTML → {html_path} ...")
    build_html_to_file(
        meta,
        author_info,
        pages_iter,
        html_path,
        flush_every=flush_every,
        volume_label=volume_label,
        columns=columns,
        fast=fast,
    )
    print(f"  [html] done ({html_path.stat().st_size // 1024} KB)")

    if WeasyprintHTML is None:
        print("  [!] weasyprint not installed — HTML saved, PDF skipped.")
        return

    import urllib.request

    file_url = urllib.request.pathname2url(str(html_path.resolve()))
    if not file_url.startswith("///"):
        file_url = "//" + file_url  # ensure absolute file:// URL on Linux
    file_url = "file:" + file_url

    print(f"  [pdf] rendering via WeasyPrint (file URL) ...")
    WeasyprintHTML(filename=str(html_path.resolve())).write_pdf(out_path)
    print(f"  [pdf] saved → {out_path}")


# ═══════════════════════════════════════════════════════════════════════════
# DOCX GENERATION  (python-docx — mirrors the PDF's cover/TOC/colors/hamesh)
# ═══════════════════════════════════════════════════════════════════════════
#
# Reuses _render_page_html verbatim for each page (same heading-resolution,
# numbering, suppression logic as the PDF) and converts the small resulting
# HTML fragment into native docx paragraphs/runs. This guarantees the two
# formats never drift apart — there is exactly one heading-layout algorithm,
# not two competing implementations.

_DOCX_FONT_BODY = "Scheherazade New"
_DOCX_FONT_HEAD = "Scheherazade New"

# Same Islamic-gold palette + per-level heading colors as the PDF CSS.
_DOCX_GOLD = "C99200"
_DOCX_GOLD_L = "E8B830"
_DOCX_TEAL = "256080"
_DOCX_RUST = "B8451A"
_DOCX_SEPIA = "805830"
_DOCX_INK = "1A1008"

_HEADING_SHAPES = {
    0: "۞",
    1: "❖",
    2: "◈",
    3: "◆",
    4: "●",
    5: "★",
    6: "✦",
    7: "◇",
    8: "◎",
    9: "▪",
}

_DOCX_LEVEL_COLORS = {
    0: (_DOCX_GOLD_L, 19),
    1: (_DOCX_RUST, 18),
    2: (_DOCX_TEAL, 17),
    3: (_DOCX_SEPIA, 16),
    4: ("2D7A4F", 16),
    5: ("7B2D8E", 16),
    6: ("C07A1A", 16),
    7: ("C0392B", 16),
    8: ("1E7A9E", 16),
    9: ("C06080", 16),
}


# OOXML requires child elements of <w:pPr>/<w:rPr>/<w:sectPr> to appear in
# a fixed schema order. Blindly .append()-ing a manually built element (as
# an earlier version of this code did for w:bidi/w:rFonts/w:pBdr) puts it
# out of order, which Word/LibreOffice tolerate inconsistently — the visible
# symptom is exactly the garbled/interleaved RTL text seen in practice.
# These two lists + helper insert elements at the schema-correct position
# regardless of what's already been set on the paragraph/run.
_PPR_TAG_SEQ = (
    "w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:framePr",
    "w:widowControl", "w:numPr", "w:suppressLineNumbers", "w:pBdr", "w:shd",
    "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap", "w:overflowPunct",
    "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi", "w:adjustRightInd",
    "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents",
    "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
    "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr",
    "w:sectPr", "w:pPrChange",
)
_SECTPR_TAG_SEQ = (
    "w:headerReference", "w:footerReference", "w:footnotePr", "w:endnotePr",
    "w:type", "w:pgSz", "w:pgMar", "w:paperSrc", "w:pgBorders", "w:lnNumType",
    "w:pgNumType", "w:cols", "w:formProt", "w:vAlign", "w:noEndnote", "w:titlePg",
    "w:textDirection", "w:bidi", "w:rtlGutter", "w:docGrid", "w:printerSettings",
    "w:sectPrChange",
)


def _oxml_insert_ordered(parent, new_elm, tag: str, tag_seq: tuple) -> None:
    """Insert *new_elm* (tag *tag*) into *parent* at the position required by
    *tag_seq*, using python-docx's own insert_element_before (so it lands
    before whichever later-in-sequence sibling already exists, or at the end
    if none do)."""
    successors = tag_seq[tag_seq.index(tag) + 1 :]
    parent.insert_element_before(new_elm, *successors)


def _docx_zero_indent(paragraph_format) -> None:
    """Force left/right/first-line indent to 0 so nothing is ever nudged
    away from the page margin (e.g. inherited indents from built-in
    Heading styles), keeping titles/text flush against the margin."""
    paragraph_format.left_indent = Pt(0)
    paragraph_format.right_indent = Pt(0)
    paragraph_format.first_line_indent = Pt(0)


def _docx_page_break_before(paragraph) -> None:
    """Mark *paragraph* to always start on a new page, via the
    page-break-before paragraph property rather than inserting a separate
    paragraph containing only a manual page-break run. The latter pattern
    can leave a stray fully-blank page behind whenever natural pagination
    already pushes that empty paragraph onto its own page — this avoids
    that entirely since there's no orphan paragraph to begin with."""
    paragraph.paragraph_format.page_break_before = True


def _docx_set_bidi(paragraph) -> None:
    """Mark a paragraph right-to-left (w:bidi) so Word lays it out as Arabic,
    and strip any indent so it sits flush against the right margin."""
    _docx_zero_indent(paragraph.paragraph_format)
    pPr = paragraph._p.get_or_add_pPr()
    if pPr.find(qn("w:bidi")) is not None:
        return
    bidi = OxmlElement("w:bidi")
    _oxml_insert_ordered(pPr, bidi, "w:bidi", _PPR_TAG_SEQ)


def _docx_style_set_bidi(style) -> None:
    """Same as _docx_set_bidi but for a paragraph *style* definition
    (<w:style><w:pPr> uses the identical child-order rules as a paragraph's
    own <w:pPr>)."""
    _docx_zero_indent(style.paragraph_format)
    pPr = style.element.get_or_add_pPr()
    if pPr.find(qn("w:bidi")) is not None:
        return
    bidi = OxmlElement("w:bidi")
    _oxml_insert_ordered(pPr, bidi, "w:bidi", _PPR_TAG_SEQ)


def _docx_set_run_rtl_font(run, font_name: str = _DOCX_FONT_BODY) -> None:
    """Set both the ascii and complex-script (cs) font so Arabic actually
    picks up *font_name* instead of falling back to a Latin default font."""
    run.font.rtl = True
    run.font.complex_script = True
    rPr = run._r.get_or_add_rPr()
    rFonts = rPr.get_or_add_rFonts()  # schema-ordered accessor, not a raw append
    rFonts.set(qn("w:cs"), font_name)
    rFonts.set(qn("w:ascii"), font_name)
    rFonts.set(qn("w:hAnsi"), font_name)


def _docx_add_run(
    paragraph,
    text: str,
    *,
    font_name: str = _DOCX_FONT_BODY,
    size: float | None = None,
    bold: bool = False,
    color_hex: str | None = None,
) -> None:
    run = paragraph.add_run(text)
    _docx_set_run_rtl_font(run, font_name)
    if size:
        run.font.size = Pt(size)
    run.font.bold = bold
    if color_hex:
        run.font.color.rgb = RGBColor.from_string(color_hex.lstrip("#").upper())


def _docx_add_inline_html(paragraph, node, **run_kwargs) -> None:
    """Walk a <p>-like bs4 node, emitting runs for text / span.color / <br>."""
    from bs4 import NavigableString

    for child in node.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text:
                _docx_add_run(paragraph, text, **run_kwargs)
        elif child.name == "br":
            run = paragraph.add_run()
            _docx_set_run_rtl_font(run, run_kwargs.get("font_name", _DOCX_FONT_BODY))
            run.add_break(WD_BREAK.LINE)
        elif child.name == "span":
            style = child.get("style", "")
            m = re.search(r"color:\s*(#[0-9a-fA-F]{6})", style)
            color = m.group(1) if m else run_kwargs.get("color_hex")
            bold = bool(re.search(r"font-weight:\s*bold", style))
            kw = dict(run_kwargs)
            kw["color_hex"] = color
            if bold:
                kw["bold"] = True
            _docx_add_inline_html(paragraph, child, **kw)
        else:
            _docx_add_inline_html(paragraph, child, **run_kwargs)


def _docx_add_body_paragraph(doc, p_tag, *, style_name: str = "Shamela Body") -> None:
    para = doc.add_paragraph(style=style_name)
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _docx_set_bidi(para)
    bsz = STYLE_CFG.get("body_size_docx", DEFAULT_STYLE_CFG["body_size_docx"])
    _docx_add_inline_html(para, p_tag, font_name=_DOCX_FONT_BODY, size=bsz, bold=True)


def _docx_add_chapter_heading(doc, p_tag) -> None:
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _docx_set_bidi(para)
    para.paragraph_format.space_before = Pt(10)
    para.paragraph_format.space_after = Pt(6)
    text = p_tag.get_text(" ", strip=True)
    _docx_add_run(
        para, text, font_name=_DOCX_FONT_HEAD, size=17, bold=True, color_hex=_DOCX_RUST
    )


def _docx_add_visible_heading(doc, text: str, level_num: int) -> None:
    """Visible, styled heading paragraph for on-page display only.

    Deliberately NOT a Word "Heading N" style, so it does not create an
    entry in the Navigation Pane / outline — exactly like the PDF's
    `.toc-numbered-heading`, which explicitly sets `bookmark-level: none`.
    The outline entry (carrying the numbering) comes solely from the
    paired invisible anchor via `_docx_add_invisible_heading`, so numbers
    never appear in the visible content, only in the outline.
    """
    if not text.strip():
        return
    color, size = _DOCX_LEVEL_COLORS.get(min(level_num, 9), _DOCX_LEVEL_COLORS[9])
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _docx_set_bidi(para)
    para.paragraph_format.space_before = Pt(8)
    para.paragraph_format.space_after = Pt(4)
    shape = _HEADING_SHAPES.get(level_num, "")
    if shape:
        _docx_add_run(para, shape + " ", font_name=_DOCX_FONT_HEAD, size=max(11, size - 4), bold=True, color_hex=color)
    _docx_add_run(para, text, font_name=_DOCX_FONT_HEAD, size=size, bold=True, color_hex=color)


_DOCX_NUM_PREFIX_RE = re.compile(r"^\s*[\d٠-٩]+(?:[.\u066B][\d٠-٩]+)*[.\u066B]?\s+")


def _docx_strip_number_prefix(text: str) -> str:
    """Strip a leading 'N. ' / '1.2. ' style number label off heading text,
    so the visible on-page paragraph never repeats the outline's numbering."""
    return _DOCX_NUM_PREFIX_RE.sub("", text, count=1)


def _docx_add_invisible_heading(doc, text: str, level_num: int) -> None:
    """A heading that exists purely for outline/navigation purposes.

    Mirrors the PDF's `.toc-bookmark-anchor` paragraphs: in the PDF, every
    outline/bookmark entry is generated from these anchors specifically
    (bookmark-level lives on `.toc-bm-N`, while the *visible*
    `.toc-numbered-heading` explicitly sets `bookmark-level: none`). For
    "implicit" TOC entries — headings the scraper knows about from the
    book's TOC but never found matching visible text for on the page —
    the PDF renders *only* this anchor (1pt, height 0, invisible on the
    page) so the title still shows up in the PDF's outline.

    Word has no equivalent of an invisible-but-indexed element, so we add
    a real "Heading N" paragraph (so it lands in the Navigation Pane / any
    TOC field) but shrink it to be visually negligible on the page, same
    intent as the PDF's invisible anchor.
    """
    if not text.strip():
        return
    word_level = min(level_num + 1, 9)
    para = doc.add_heading(level=word_level)
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _docx_set_bidi(para)
    para.paragraph_format.space_before = Pt(0)
    para.paragraph_format.space_after = Pt(0)
    para.paragraph_format.line_spacing = Pt(1)
    run = para.add_run(text)
    _docx_set_run_rtl_font(run, _DOCX_FONT_HEAD)
    run.font.size = Pt(1)
    run.font.color.rgb = RGBColor.from_string("FFFFFF")


def _docx_add_hamesh(doc, div_tag) -> None:
    # Dashed top border to echo the PDF's `.hamesh::before` rule line.
    # The paragraph carrying the border has no visible text, but Word still
    # reserves a full line of height for it based on the *document default*
    # font size unless we shrink that explicitly — that was producing the
    # big empty gap around the line. Pinning line_spacing to an exact small
    # value + a tiny hidden run collapses it down to a hairline.
    rule = doc.add_paragraph()
    rule.paragraph_format.space_before = Pt(6)
    rule.paragraph_format.space_after = Pt(0)
    rule.paragraph_format.line_spacing = Pt(1)
    pPr = rule._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    top = OxmlElement("w:top")
    top.set(qn("w:val"), "dashed")
    top.set(qn("w:sz"), "6")
    top.set(qn("w:space"), "1")
    top.set(qn("w:color"), "999999")
    pBdr.append(top)
    _oxml_insert_ordered(pPr, pBdr, "w:pBdr", _PPR_TAG_SEQ)
    # Hidden 1pt run so the paragraph's line box shrinks to match the exact
    # line_spacing above instead of inheriting the doc's base font size.
    hidden_run = rule.add_run("")
    hidden_run.font.size = Pt(1)
    for p in div_tag.find_all("p", recursive=False):
        para = doc.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _docx_set_bidi(para)
        para.paragraph_format.space_before = Pt(0)
        para.paragraph_format.space_after = Pt(2)
        _docx_add_inline_html(para, p, font_name=_DOCX_FONT_BODY, size=10, color_hex="555555")


def _docx_emit_fragment(doc, container, max_toc_level: int = -1) -> None:
    """Recursively walk the HTML fragment from _render_page_html and emit
    the equivalent native docx content.

    max_toc_level: the deepest level present in the book's real TOC
    (meta["toc_flat"]). Mirrors the PDF's `toc_bm_css`, which only defines
    a `bookmark-level` CSS rule for levels 0..max_toc_level — anything
    deeper (auto-detected bracket headings with no real TOC entry) is
    rendered as a plain styled heading on the page but never reaches the
    PDF's bookmark pane. -1 means "unknown / no TOC data", in which case
    every heading is still added to the outline (safe fallback)."""
    elements = [c for c in container.children if getattr(c, "name", None)]
    for idx, child in enumerate(elements):
        classes = child.get("class", []) if hasattr(child, "get") else []
        if child.name == "div":
            if "bm-setter" in classes:
                continue  # invisible PDF-only bookmark marker, no docx equivalent needed
            if "hamesh" in classes:
                _docx_add_hamesh(doc, child)
                continue
            # structural wrapper (page-entry / page-text) → recurse
            _docx_emit_fragment(doc, child, max_toc_level)
            continue
        if child.name == "p":
            if "toc-bookmark-anchor" in classes:
                nxt = elements[idx + 1] if idx + 1 < len(elements) else None
                nxt_classes = (
                    nxt.get("class", []) if nxt is not None and hasattr(nxt, "get") else []
                )
                has_visible_pair = (
                    nxt is not None and nxt.name == "p" and "toc-numbered-heading" in nxt_classes
                )
                m = re.search(r"toc-bm-(\d+)", " ".join(classes))
                level_num = min(int(m.group(1)), 9) if m else 6
                anchor_text = child.get_text(" ", strip=True)
                # In-range → this level genuinely exists in the book's real
                # TOC, so it gets a Word outline entry (numbered), same as
                # the PDF's bookmark pane. Out-of-range (deeper than the
                # real TOC ever goes) → an auto-detected heading with no
                # TOC backing, so no outline entry — visible content only.
                in_toc_range = max_toc_level < 0 or level_num <= max_toc_level
                if has_visible_pair:
                    # Source the visible text from the anchor (full,
                    # correctly TOC-matched title) rather than the literal
                    # on-page paragraph, which can be truncated — same
                    # reasoning as before — but strip the number so the
                    # numbering shows up ONLY in the outline, never in the
                    # visible page content.
                    visible_text = _docx_strip_number_prefix(anchor_text)
                    if in_toc_range:
                        _docx_add_invisible_heading(doc, anchor_text, level_num)
                    _docx_add_visible_heading(doc, visible_text, level_num)
                else:
                    # Standalone anchor (an "implicit" TOC entry with no
                    # visible text match on the page). Only add it to the
                    # outline if it's genuinely within the real TOC's depth.
                    if in_toc_range:
                        _docx_add_invisible_heading(doc, anchor_text, level_num)
                continue
            if "toc-numbered-heading" in classes:
                # Its title text was already emitted above (sourced from
                # the paired anchor); this paragraph would just duplicate
                # it with the shorter/rawer on-page wording, so skip it.
                continue
            if "chapter-heading" in classes:
                _docx_add_chapter_heading(doc, child)
                continue
            _docx_add_body_paragraph(doc, child)
            continue


def _docx_setup_styles(doc) -> None:
    """Configure page geometry + a justified RTL 'Shamela Body' style."""
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(1.3)
    section.bottom_margin = Cm(1.5)
    section.left_margin = Cm(1.5)
    section.right_margin = Cm(1.5)
    # section-level bidi so Word treats the whole page as RTL (margins mirror, etc.)
    sectPr = section._sectPr
    bidi = OxmlElement("w:bidi")
    _oxml_insert_ordered(sectPr, bidi, "w:bidi", _SECTPR_TAG_SEQ)

    normal = doc.styles["Normal"]
    normal.font.name = _DOCX_FONT_BODY
    normal.font.size = Pt(STYLE_CFG.get("body_size_docx", DEFAULT_STYLE_CFG["body_size_docx"]))
    normal.font.bold = True
    rpr = normal.element.get_or_add_rPr()
    rFonts = rpr.get_or_add_rFonts()
    rFonts.set(qn("w:cs"), _DOCX_FONT_BODY)
    _docx_style_set_bidi(normal)

    body_style = doc.styles.add_style("Shamela Body", WD_STYLE_TYPE.PARAGRAPH)
    body_style.base_style = normal
    body_style.paragraph_format.space_after = Pt(4)
    body_style.paragraph_format.line_spacing = 1.08
    body_style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _docx_style_set_bidi(body_style)

    # Belt-and-suspenders: every built-in Heading style Word ships with is
    # LTR by default. Direct per-paragraph formatting (set on every heading
    # we emit) already overrides this, but setting it at the style level too
    # means nothing can slip through un-mirrored.
    for lvl in range(1, 10):
        try:
            heading_style = doc.styles[f"Heading {lvl}"]
        except KeyError:
            continue
        heading_style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _docx_style_set_bidi(heading_style)
        hrpr = heading_style.element.get_or_add_rPr()
        hFonts = hrpr.get_or_add_rFonts()
        hFonts.set(qn("w:cs"), _DOCX_FONT_HEAD)


def _docx_add_page_number_footer(doc) -> None:
    section = doc.sections[0]
    footer = section.footer
    para = footer.paragraphs[0]
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _docx_set_bidi(para)
    run = para.add_run("— ")
    _docx_set_run_rtl_font(run, _DOCX_FONT_BODY)
    fldChar1 = OxmlElement("w:fldChar")
    fldChar1.set(qn("w:fldCharType"), "begin")
    instrText = OxmlElement("w:instrText")
    instrText.set(qn("xml:space"), "preserve")
    instrText.text = "PAGE"
    fldChar2 = OxmlElement("w:fldChar")
    fldChar2.set(qn("w:fldCharType"), "end")
    r = para.add_run()
    r._r.append(fldChar1)
    r._r.append(instrText)
    r._r.append(fldChar2)
    _docx_set_run_rtl_font(r, _DOCX_FONT_BODY)
    run2 = para.add_run(" —")
    _docx_set_run_rtl_font(run2, _DOCX_FONT_BODY)


def _docx_add_page_number_header(doc) -> None:
    """Small page-number field in the top-right corner of every page,
    in addition to the centered one in the footer."""
    section = doc.sections[0]
    header = section.header
    para = header.paragraphs[0]
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT  # renders top-right in this doc's RTL layout
    _docx_set_bidi(para)
    fldChar1 = OxmlElement("w:fldChar")
    fldChar1.set(qn("w:fldCharType"), "begin")
    instrText = OxmlElement("w:instrText")
    instrText.set(qn("xml:space"), "preserve")
    instrText.text = "PAGE"
    fldChar2 = OxmlElement("w:fldChar")
    fldChar2.set(qn("w:fldCharType"), "end")
    r = para.add_run()
    r._r.append(fldChar1)
    r._r.append(instrText)
    r._r.append(fldChar2)
    _docx_set_run_rtl_font(r, _DOCX_FONT_BODY)
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor.from_string("888888")


def build_docx(
    meta: dict,
    author_info: dict,
    pages_iter,
    out_path: str,
    volume_label: str | None = None,
) -> None:
    """Build a .docx from *pages_iter*, mirroring the PDF's cover, real Word
    Heading styles (for native outline navigation), per-level heading
    colors, hamesh footnote blocks, and colored inline spans.

    Reuses _render_page_html for the actual content so heading/numbering
    logic is identical to the PDF — this function only translates that
    HTML fragment into docx paragraphs/runs.
    """
    if _docxlib is None:
        print("  [!] python-docx not installed — DOCX skipped (pip install python-docx).")
        return

    doc = _docxlib.Document()
    _docx_setup_styles(doc)
    _docx_add_page_number_footer(doc)
    _docx_add_page_number_header(doc)

    # ── COVER ────────────────────────────────────────────────────────
    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _docx_set_bidi(title_p)
    _docx_add_run(
        title_p,
        "بِسْمِ اللَّهِ الرَّحْمَنِ الرَّحِيمِ",
        font_name=_DOCX_FONT_HEAD,
        size=13,
        color_hex=_DOCX_GOLD,
    )
    doc.add_paragraph()

    book_title_p = doc.add_paragraph()
    book_title_p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _docx_set_bidi(book_title_p)
    _docx_add_run(
        book_title_p,
        meta.get("title", "كتاب"),
        font_name=_DOCX_FONT_HEAD,
        size=28,
        bold=True,
        color_hex=_DOCX_INK,
    )

    if volume_label:
        vol_p = doc.add_paragraph()
        vol_p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _docx_set_bidi(vol_p)
        _docx_add_run(
            vol_p, f"الجزء {volume_label}", font_name=_DOCX_FONT_HEAD, size=17, color_hex=_DOCX_GOLD_L
        )

    doc.add_paragraph()
    meta_rows = [
        (label, meta[key])
        for label, key in [
            ("المؤلف", "author"),
            ("الناشر", "publisher"),
            ("الطبعة", "edition"),
            ("عدد الأجزاء", "volumes"),
        ]
        if meta.get(key)
    ]
    if meta_rows:
        table = doc.add_table(rows=len(meta_rows), cols=2)
        table.alignment = WD_ALIGN_PARAGRAPH.LEFT
        for i, (label, value) in enumerate(meta_rows):
            for cell, text, bold in (
                (table.cell(i, 1), label, True),
                (table.cell(i, 0), str(value), False),
            ):
                cp = cell.paragraphs[0]
                cp.alignment = WD_ALIGN_PARAGRAPH.LEFT
                _docx_set_bidi(cp)
                _docx_add_run(cp, text, font_name=_DOCX_FONT_BODY, size=11, bold=bold)

    src_p = doc.add_paragraph()
    src_p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _docx_set_bidi(src_p)
    scraped_date = datetime.date.today().strftime("%Y-%m-%d")
    _docx_add_run(
        src_p,
        f"المصدر: {meta.get('url', '')} | تاريخ التحميل: {scraped_date}",
        font_name=_DOCX_FONT_BODY,
        size=9,
        color_hex="888888",
    )
    pending_page_break = True  # next major heading (bio or body) starts a fresh page

    # ── AUTHOR BIO ───────────────────────────────────────────────────
    if author_info and not author_info.get("error") and author_info.get("bio"):
        bio_h = doc.add_heading(level=1)
        _docx_page_break_before(bio_h)
        pending_page_break = False
        bio_h.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _docx_set_bidi(bio_h)
        _docx_add_run(bio_h, "ترجمة المؤلف", font_name=_DOCX_FONT_HEAD, size=18, bold=True, color_hex=_DOCX_TEAL)
        for line in author_info["bio"].splitlines():
            if line.strip():
                bp = doc.add_paragraph()
                bp.alignment = WD_ALIGN_PARAGRAPH.LEFT
                _docx_set_bidi(bp)
                _docx_add_run(bp, line.strip(), font_name=_DOCX_FONT_BODY, size=15)
        pending_page_break = True  # body text starts a fresh page after the bio

    # ── BOOK TEXT ────────────────────────────────────────────────────
    body_h = doc.add_heading(level=1)
    if pending_page_break:
        _docx_page_break_before(body_h)
    body_h.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _docx_set_bidi(body_h)
    _docx_add_run(body_h, "نص الكتاب", font_name=_DOCX_FONT_HEAD, size=18, bold=True, color_hex=_DOCX_TEAL)

    toc_flat = meta.get("toc_flat", [])
    toc_by_page: dict = {}
    max_toc_level = -1
    for entry in toc_flat:
        lvl = entry.get("level", 0)
        if lvl > max_toc_level:
            max_toc_level = lvl
        pid = entry.get("page_id")
        if pid is not None:
            toc_by_page.setdefault(pid, []).append((entry.get("label", ""), lvl))

    if volume_label is None:
        vol_starts_list = sorted(meta.get("volume_start_pages", []))
        vol_boundaries = set(vol_starts_list)
        vol_labels_list = meta.get("volume_labels", [])
    else:
        vol_starts_list = []
        vol_boundaries = set()
        vol_labels_list = []
    vol_num = [1]

    if vol_starts_list and vol_labels_list:
        vp = doc.add_heading(level=2)
        vp.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _docx_set_bidi(vp)
        _docx_add_run(vp, f"الجزء {vol_labels_list[0]}", font_name=_DOCX_FONT_HEAD, size=17, bold=True, color_hex=_DOCX_GOLD)

    first_page = True
    n_pages = 0
    for page in _ensure_resolved(meta, pages_iter):
        pid = page.get("page_id")
        if pid and pid in vol_boundaries and not first_page:
            vol_label = (
                vol_labels_list[vol_num[0]] if vol_labels_list else str(vol_num[0] + 1)
            )
            vp = doc.add_heading(level=2)
            _docx_page_break_before(vp)
            vp.alignment = WD_ALIGN_PARAGRAPH.LEFT
            _docx_set_bidi(vp)
            _docx_add_run(vp, f"الجزء {vol_label}", font_name=_DOCX_FONT_HEAD, size=17, bold=True, color_hex=_DOCX_GOLD)
            vol_num[0] += 1

        frag, _ = _render_page_html(page, toc_by_page, vol_boundaries, vol_num, max_toc_level)
        first_page = False
        n_pages += 1
        if not frag.strip():
            continue
        soup = BeautifulSoup(frag, "lxml")
        _docx_emit_fragment(doc, soup.body, max_toc_level)

    doc.save(out_path)
    print(f"  [docx] saved → {out_path}  ({n_pages} pages)")



# ═══════════════════════════════════════════════════════════════════════════


def resolve_book_dir(
    out_dir: Path,
    manifest: Manifest,
    book_id: int,
    meta: dict,
    author_info: dict,
    category_label: str = None,
) -> Path:
    """
    Decide (once) where this book lives on disk:
        <out_dir>/<category_label or _uncategorized>/<author_id_author_name>/<book_id_title>/
    The chosen path is cached in the manifest so re-runs (possibly under a
    different category context, or before author info was known) always land
    in the same folder instead of duplicating it.
    """
    entry = manifest.book(book_id)
    if entry.get("dir"):
        d = Path(out_dir) / entry["dir"]
        d.mkdir(parents=True, exist_ok=True)
        return d

    author_id = meta.get("author_id", "unk")
    author_name = author_info.get("full_name") or meta.get("author") or "مؤلف_غير_معروف"
    title = meta.get("title") or f"book_{book_id}"

    if not category_label and meta.get("category_id"):
        category_label = meta.get("category_name", "category")
    cat_part = sanitize_filename(category_label) if category_label else "_uncategorized"
    author_part = sanitize_filename(f"{author_id}_{author_name}")
    book_part = sanitize_filename(f"{book_id}_{title}")

    rel = Path(cat_part) / author_part / book_part
    d = Path(out_dir) / rel
    d.mkdir(parents=True, exist_ok=True)

    entry["dir"] = str(rel)
    return d


# ═══════════════════════════════════════════════════════════════════════════
# PER-BOOK PIPELINE
# ═══════════════════════════════════════════════════════════════════════════


def process_book(
    session: requests.Session,
    book_id: int,
    out_dir: Path,
    manifest: Manifest,
    category_label: str,
    args,
) -> None:
    book_id = int(book_id)
    entry = manifest.book(book_id)

    # ── --pdf_only / --docx on an already-scraped book: rebuild output(s)
    # from cached data on disk, no network. Without this, a book that was
    # already marked "done" from a prior PDF-only run would get skipped
    # entirely by the "already done" check below, even when --docx is
    # asking for a format that was never actually built yet.
    if (args.pdf_only or args.docx) and entry.get("dir") and not args.force:
        bd = Path(out_dir) / entry["dir"]
        meta = load_json(bd / "meta.json")
        author_info = load_json(bd / "author_info.json")
        pages_path = bd / "pages.jsonl"
        has_pages = pages_path.exists() and pages_path.stat().st_size > 0
        if meta and author_info is not None and has_pages:
            book_dir = bd
            print(
                f"[*] Using cached data for book {book_id} ({meta.get('title', '?')})"
            )
            # Jump straight to heading resolution + output build (skip network)
            _build_book_outputs(
                meta, author_info, book_dir, book_id, args, entry, manifest,
                need_scrape=False,
            )
            return
        if meta and author_info is not None and not has_pages:
            print(
                f"  [!] --pdf_only/--docx requested for book {book_id} but no scraped "
                f"page data found on disk — scraping first, then building output(s) ..."
            )
        else:
            print(
                f"  [!] Cached data incomplete for book {book_id}, falling back to network"
            )

    if (
        entry.get("status") == "done"
        and not args.force
        and not args.pdf_only
        and not args.docx
    ):
        print(f"[skip] book {book_id} ({entry.get('title', '')}) — already done")
        return

    if args.force and entry.get("dir"):
        # With --force, clear the cached dir so resolve_book_dir recomputes
        # it from scratch (e.g. if the category is now known from meta).
        del entry["dir"]

    print(f"[*] Fetching metadata for book {book_id} ...")
    meta = fetch_book_meta(session, book_id)
    print(f"    title  : {meta.get('title', '?')}")
    print(f"    author : {meta.get('author', '?')}")
    print(
        f"    toc    : {meta['toc_summary']['total_entries']} entries, "
        f"max depth {meta['toc_summary']['max_depth']}"
    )

    # author info (cached across the whole run in the manifest)
    author_info = {}
    author_id = meta.get("author_id")
    if author_id:
        cached = manifest.data["authors"].get(str(author_id))
        if cached:
            author_info = cached
        else:
            print(f"[*] Fetching author info (id={author_id}) ...")
            author_info = fetch_author_info(session, int(author_id))
            manifest.data["authors"][str(author_id)] = author_info
            time.sleep(args.delay)

    book_dir = resolve_book_dir(
        out_dir, manifest, book_id, meta, author_info, category_label
    )
    # resolve_book_dir may have derived the label from meta; capture it.
    category_label = category_label or meta.get("category_name")

    entry["title"] = meta.get("title")
    entry["author"] = meta.get("author")
    if category_label:
        labels = entry.setdefault("category_labels", [])
        if category_label not in labels:
            labels.append(category_label)
    manifest.save()

    atomic_write_json(book_dir / "meta.json", meta)
    atomic_write_json(book_dir / "author_info.json", author_info)

    # ── determine first page id ─────────────────────────────────────────
    first_page = args.start_page
    if first_page == 1:
        vol_starts = meta.get("volume_start_pages")
        toc_first = next(
            (n["page_id"] for n in meta.get("toc_flat", []) if n.get("page_id")), None
        )
        # Prefer volume_start_pages[0] (always the true first page of the
        # book) over the first TOC entry's page_id, because the TOC may
        # start mid-book (e.g. volume 2) and skip all earlier content.
        candidates = [1]
        if vol_starts:
            candidates.append(vol_starts[0])
        if toc_first is not None:
            candidates.append(toc_first)
        first_page = min(candidates)

    # ── scrape (resumable) ──────────────────────────────────────────────
    _pages_check_path = book_dir / "pages.jsonl"
    _has_existing_pages = (
        _pages_check_path.exists() and _pages_check_path.stat().st_size > 0
    )
    _rebuild_only = args.pdf_only or args.docx
    need_scrape = not _rebuild_only or not _has_existing_pages
    if _rebuild_only and not _has_existing_pages:
        print(
            f"  [!] --pdf_only/--docx requested but no scraped data found for "
            f"book {book_id} yet — scraping now (one-time), then building output(s) ..."
        )
    if need_scrape:
        print(
            f"[*] Scraping pages for book {book_id} (starting at page_id={first_page}) ..."
        )
        prog = scrape_book_pages_resumable(
            session,
            book_id,
            book_dir,
            start_page=first_page,
            meta=meta,
            limit=args.limit,
            delay=args.delay,
            force=args.force,
            workers=getattr(args, "workers", 8),
        )
        entry["status"] = prog["status"]
        entry["last_page_id"] = prog.get("last_page_id")
        entry["next_page_id"] = prog.get("next_page_id")
        entry["pages_scraped"] = prog.get("pages_scraped")
        entry["updated_at"] = datetime.datetime.now().isoformat()
        manifest.save()
        if prog["status"] == "error":
            print(f"[!] book {book_id} stopped early: {prog.get('error')}")
            print(
                f"    re-run the same command later to resume from page_id={prog.get('next_page_id')}"
            )
            return

    _build_book_outputs(
        meta, author_info, book_dir, book_id, args, entry, manifest,
        need_scrape=need_scrape,
    )


def _build_book_outputs(
    meta: dict,
    author_info: dict,
    book_dir: Path,
    book_id: int,
    args,
    entry: dict,
    manifest: Manifest,
    need_scrape: bool = True,
) -> None:
    """Heading resolution → combined JSON → PDF (shared by scrape and --pdf_only paths)."""
    pages_path = book_dir / "pages.jsonl"
    resolved_path = book_dir / "pages_resolved.jsonl"
    print(f"[*] Resolving headings for book {book_id} ...")
    n_resolved = materialize_resolved_pages(meta, pages_path, resolved_path)
    print(f"    → {n_resolved} pages resolved → {resolved_path}")
    _renumber_headings_by_document_order(resolved_path)
    print(f"    → headings renumbered by content order")

    # ── assemble combined JSON ───────────────────────────────────────────
    # Enabled via --json flag.  Skipped by default to save RAM on large books
    # (17K-page Tafsir al-Tabari would consume several hundred MB).
    if args.save_json:
        pages = load_pages_jsonl(resolved_path)
        data = {
            "meta": meta,
            "toc_flat": meta.get("toc_flat", []),
            "toc": meta.get("toc", []),
            "pages": pages,
        }
        if author_info and not author_info.get("error") and author_info.get("bio"):
            data["author_info"] = author_info
        json_path = book_dir / f"book_{book_id}.json"
        atomic_write_json(json_path, data)
        print(f"  [json] saved → {json_path}  ({len(pages)} pages)")
        del pages
        import gc
        gc.collect()

    # ── PDF (per-juz if volumes exist, otherwise single) ──────────────────
    if not args.json_only:
        vol_pages = meta.get("volume_start_pages")
        vol_labels = meta.get("volume_labels")
        # Dedup in case old meta.json has duplicate entries (top+bottom dropdowns)
        if vol_pages and vol_labels:
            deduped_pages, deduped_labels = [], []
            seen = set()
            for p, l in zip(vol_pages, vol_labels):
                if p not in seen:
                    seen.add(p)
                    deduped_pages.append(p)
                    deduped_labels.append(l)
            if len(deduped_pages) != len(vol_pages):
                vol_pages, vol_labels = deduped_pages, deduped_labels
                meta["volume_start_pages"] = vol_pages
                meta["volume_labels"] = vol_labels
        # Merge intro volumes (non-digit labels like 'م 1') with their
        # following content volume (digit labels like '1') so each
        # logical juz is one unit — fewer per-juz PDFs and correct
        # volume prefixes in the combined PDF.
        if vol_pages and vol_labels:
            vol_pages, vol_labels = _merge_intro_volumes(vol_pages, vol_labels)
            meta["volume_start_pages"] = vol_pages
            meta["volume_labels"] = vol_labels
        # Validate --juz if given
        juz_arg = getattr(args, "juz", None)
        if juz_arg is not None:
            n_avail = len(vol_pages) if vol_pages else 0
            if n_avail < 2:
                print(
                    f"  [!] --juz {juz_arg} ignored: book has no volumes"
                )
            elif juz_arg < 1 or juz_arg > n_avail:
                print(
                    f"  [!] --juz {juz_arg} out of range (1–{n_avail}), ignoring"
                )
        # A PDF is built by default. --docx REPLACES the PDF build (no PDF)
        # unless --also_pdf is explicitly passed to request both formats.
        want_docx = getattr(args, "docx", False)
        want_pdf = (not want_docx) or getattr(args, "also_pdf", False)

        if want_pdf:
            has_volumes = (
                vol_pages
                and vol_labels
                and len(vol_pages) > 1
                and not getattr(args, "single_pdf", False)
            )
            if has_volumes:
                n_vols = len(vol_pages)
                for vi in range(n_vols):
                    if juz_arg is not None and vi + 1 != juz_arg:
                        continue
                    label = vol_labels[vi]
                    lo = vol_pages[vi]
                    hi = vol_pages[vi + 1] - 1 if vi + 1 < n_vols else "end"
                    vol_suffix = label.replace(" ", "_")
                    if not vol_suffix:
                        vol_suffix = str(vi + 1).zfill(3)
                    pdf_name = f"book_{book_id}_{vol_suffix}.pdf"
                    pdf_path = book_dir / pdf_name
                    print(
                        f"[*] Building PDF for book {book_id}, juz '{label}' ({lo}–{hi}) → {pdf_name} ..."
                    )
                    vol_iter = _pages_by_volume(
                        iter_pages_jsonl(resolved_path), vol_pages, vi
                    )
                    vol_iter = _prefix_headings_by_juz(
                        vol_iter, vol_pages, include_juz_prefix=False
                    )
                    build_pdf(
                        meta,
                        author_info,
                        vol_iter,
                        str(pdf_path),
                        flush_every=getattr(args, "flush_every", 50),
                        volume_label=label,
                        columns=getattr(args, "columns", 1),
                        fast=getattr(args, "fast", False),
                    )
            else:
                pdf_path = book_dir / f"book_{book_id}.pdf"
                pages_iter = iter_pages_jsonl(resolved_path)
                if vol_pages and len(vol_pages) > 1 and getattr(args, "single_pdf", False):
                    if getattr(args, "no_juz_outline", False):
                        print(
                            f"[*] Building combined PDF for book {book_id} (plain, no juz outline) ..."
                        )
                    else:
                        pages_iter = _prefix_headings_by_juz(pages_iter, vol_pages)
                        print(
                            f"[*] Building combined PDF for book {book_id} (with juz outline) ..."
                        )
                else:
                    print(f"[*] Building PDF for book {book_id} ...")
                build_pdf(
                    meta,
                    author_info,
                    pages_iter,
                    str(pdf_path),
                    flush_every=getattr(args, "flush_every", 50),
                    columns=getattr(args, "columns", 1),
                    fast=getattr(args, "fast", False),
                )
        elif want_docx:
            print(f"[*] --docx given without --also_pdf → skipping PDF build for book {book_id}")

        # ── DOCX (per-juz if volumes exist, otherwise single) ──────────────
        # Gated independently by --single_docx, NOT by --single_pdf, so you
        # can e.g. keep per-juz PDFs (via --also_pdf --single_pdf... ) while
        # still getting one combined DOCX.
        if want_docx:
            docx_has_volumes = (
                vol_pages
                and vol_labels
                and len(vol_pages) > 1
                and not getattr(args, "single_docx", False)
            )
            if docx_has_volumes:
                n_vols = len(vol_pages)
                for vi in range(n_vols):
                    if juz_arg is not None and vi + 1 != juz_arg:
                        continue
                    label = vol_labels[vi]
                    vol_suffix = label.replace(" ", "_")
                    if not vol_suffix:
                        vol_suffix = str(vi + 1).zfill(3)
                    docx_name = f"book_{book_id}_{vol_suffix}.docx"
                    docx_path = book_dir / docx_name
                    print(f"[*] Building DOCX for book {book_id}, juz '{label}' → {docx_name} ...")
                    docx_vol_iter = _pages_by_volume(
                        iter_pages_jsonl(resolved_path), vol_pages, vi
                    )
                    docx_vol_iter = _prefix_headings_by_juz(
                        docx_vol_iter, vol_pages, include_juz_prefix=False
                    )
                    build_docx(
                        meta, author_info, docx_vol_iter, str(docx_path), volume_label=label
                    )
            else:
                docx_path = book_dir / f"book_{book_id}.docx"
                docx_pages_iter = iter_pages_jsonl(resolved_path)
                if vol_pages and len(vol_pages) > 1 and getattr(args, "single_docx", False):
                    if getattr(args, "no_juz_outline", False):
                        print(
                            f"[*] Building combined DOCX for book {book_id} (plain, no juz outline) ..."
                        )
                    else:
                        docx_pages_iter = _prefix_headings_by_juz(docx_pages_iter, vol_pages)
                        print(
                            f"[*] Building combined DOCX for book {book_id} (with juz outline) ..."
                        )
                else:
                    print(f"[*] Building DOCX for book {book_id} ...")
                build_docx(meta, author_info, docx_pages_iter, str(docx_path))

    # Only mark "done" when scraping was actually performed and completed.
    # --pdf_only must never change the scraping status on its own, unless we
    # had to auto-scrape because no page data existed yet (need_scrape=True).
    if need_scrape:
        if entry.get("status") != "paused":
            entry["status"] = "done"
        entry["updated_at"] = datetime.datetime.now().isoformat()
        manifest.save()
    print(f"[✓] Book {book_id} → {book_dir}")


# ═══════════════════════════════════════════════════════════════════════════
# QUEUE BUILDING (category ids + explicit book ids)
# ═══════════════════════════════════════════════════════════════════════════


def build_book_queue(session: requests.Session, args, manifest: Manifest) -> list:
    queue = []  # list of (book_id, category_label_or_None)

    if args.category_ids:
        for cid in [c.strip() for c in args.category_ids.split(",") if c.strip()]:
            cid_int = int(cid)
            cached = manifest.data["categories"].get(str(cid_int))
            if cached and not args.refresh_categories:
                cat_info = cached
                print(
                    f"[*] Category {cid_int} ({cat_info.get('name')}) — using cached list "
                    f"({len(cat_info['books'])} books). Use --refresh_categories to re-fetch."
                )
            else:
                print(f"[*] Fetching category {cid_int} ...")
                cat_info = fetch_category_books(session, cid_int, delay=args.delay)
                manifest.data["categories"][str(cid_int)] = cat_info
                manifest.save()
                print(
                    f"    → {len(cat_info['books'])} books found in '{cat_info.get('name')}'"
                )

            label = f"{cid_int}_{cat_info.get('name') or 'category'}"
            for b in cat_info["books"]:
                queue.append((b["book_id"], label))

    if args.book_ids:
        for bid in [b.strip() for b in args.book_ids.split(",") if b.strip()]:
            queue.append((int(bid), None))

    if args.book_id and not args.book_ids and not args.category_ids:
        queue.append((args.book_id, None))

    # de-duplicate, keep first occurrence (and its category label)
    seen = set()
    final = []
    for bid, label in queue:
        if bid in seen:
            continue
        seen.add(bid)
        final.append((bid, label))
    return final


def print_status(manifest: Manifest):
    books = manifest.data.get("books", {})
    if not books:
        print("No books tracked yet in this manifest.")
        return
    counts = {}
    for b in books.values():
        counts[b.get("status", "?")] = counts.get(b.get("status", "?"), 0) + 1
    print(f"Tracked books: {len(books)}")
    for status, n in sorted(counts.items()):
        print(f"  {status:12s}: {n}")
    print()
    for bid, b in books.items():
        print(
            f"  [{b.get('status', '?'):10s}] {bid:>8s}  {b.get('title', '?')[:60]}  "
            f"({b.get('pages_scraped', 0)} pages)  → {b.get('dir', '?')}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="Scrape shamela.ws book(s)/categories to JSON + PDF"
    )
    parser.add_argument(
        "--book_id", type=int, default=None, help="Single Shamela book ID (legacy)"
    )
    parser.add_argument(
        "--book_ids",
        default=None,
        help="Comma-separated list of book IDs, e.g. 12762,667",
    )
    parser.add_argument(
        "--category_ids",
        default=None,
        help="Comma-separated list of category IDs, e.g. 13,33,40",
    )
    parser.add_argument(
        "--out_dir", default="./shamela_output", help="Root output directory"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1,  # 0.25
        help="Base courtesy delay in seconds (default 0.25; "
        "stagger only applied to first-batch pages)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,  # 16
        help="Concurrent page-fetch threads (default 16; "
        "increase for speed, decrease if rate-limited)",
    )
    parser.add_argument(
        "--start_page",
        type=int,
        default=1,
        help="First page ID to scrape (per book, if not resuming)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max pages to scrape PER BOOK"
    )
    parser.add_argument(
        "--cf_clearance", default=None, help="Cloudflare cf_clearance cookie value"
    )
    parser.add_argument(
        "--user-agent",
        "--ua",
        default=None,
        help="Your browser's User-Agent string. Must match the browser "
        "that generated cf_clearance.",
    )

    parser.add_argument(
        "--json_only",
        action="store_true",
        help="Only scrape + save the raw page data, skip building any output "
        "document (no PDF, no DOCX).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="save_json",
        help="Save a single combined JSON file (meta + TOC + all pages with "
        "resolved headings). Useful for post-processing or manual inspection. "
        "Generates book_<id>.json in the book directory.",
    )
    parser.add_argument(
        "--pdf_only",
        action="store_true",
        help="Rebuild output document(s) from already-scraped data on disk, "
        "skip network page-scraping. (Despite the name, this governs "
        "*scraping vs. rebuilding*, not which output format gets built — "
        "combine with --docx to rebuild a DOCX only, no network.)",
    )
    parser.add_argument(
        "--docx",
        action="store_true",
        help="Build a .docx instead of a PDF, with the same cover, native "
        "Word Heading styles (for outline navigation), per-level heading "
        "colors, and hamesh footnote blocks. By default this REPLACES the "
        "PDF build — no PDF is produced. Pass --also_pdf too if you want "
        "both. Requires `pip install python-docx`.",
    )
    parser.add_argument(
        "--also_pdf",
        action="store_true",
        help="Only meaningful together with --docx: also build the PDF "
        "alongside the DOCX (by default --docx alone builds DOCX only).",
    )
    parser.add_argument(
        "--single_pdf",
        action="store_true",
        help="Generate a single combined PDF even when the book has ajza'/volumes "
        "(applies only when a PDF is actually being built — see --docx).",
    )
    parser.add_argument(
        "--single_docx",
        action="store_true",
        help="Generate a single combined DOCX even when the book has ajza'/volumes "
        "(applies only when --docx is set; independent of --single_pdf — you "
        "can keep per-juz PDFs while still getting one combined DOCX, or vice "
        "versa, via --also_pdf).",
    )
    parser.add_argument(
        "--no_juz_outline",
        action="store_true",
        help="With --single_pdf and/or --single_docx: build the combined "
        "output without juz numbering in headings (old-style).",
    )
    parser.add_argument(
        "--juz",
        type=int,
        default=None,
        help="Generate output for a specific juz/volume only (1-based index). "
        "Applies to whichever format(s) are being built.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-scrape books even if already marked done",
    )
    parser.add_argument(
        "--refresh_categories",
        action="store_true",
        help="Re-fetch category book listings instead of using cached manifest copy",
    )
    parser.add_argument(
        "--max_books",
        type=int,
        default=None,
        help="Cap total number of books processed this run (testing)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print manifest status summary and exit (no scraping)",
    )
    parser.add_argument(
        "--columns",
        type=int,
        choices=[1, 2],
        default=1,
        help="Number of text columns per PDF page for the book body "
        "(1 = normal single-column, 2 = two-column newspaper-style layout). "
        "Default 1.",
    )
    parser.add_argument(
        "--flush_every",
        type=int,
        default=50,
        help="Flush HTML buffer to disk every N pages during PDF build (lower = less RAM, default 50)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Drop kashida justification when rendering the PDF. Kashida "
        "(Arabic letter-stretch justification) looks nicer but is a major "
        "WeasyPrint speed sink — this can cut PDF build time drastically "
        "on large books, at the cost of plain justify instead of kashida.",
    )
    parser.add_argument(
        "--style_config",
        type=str,
        default=None,
        help="Path to style_config.json. If not set, looks for "
        "style_config.json in the current directory, then falls back to "
        "built-in defaults.",
    )
    args = parser.parse_args()

    # ── Load style configuration ─────────────────────────────────────
    style_path = args.style_config
    if not style_path:
        local_cfg = Path("style_config.json")
        if local_cfg.exists():
            style_path = str(local_cfg)
    load_style_config(style_path)
    print(f"[*] Style config loaded{' from ' + style_path if style_path else ' (defaults)'}")

    if args.also_pdf and not args.docx:
        print("[!] --also_pdf has no effect without --docx (a PDF is always "
              "built unless --docx is given) — ignoring.")
    if args.single_docx and not args.docx:
        print("[!] --single_docx has no effect without --docx — ignoring.")
    if args.docx and args.json_only:
        print("[!] --json_only overrides --docx — no PDF or DOCX will be built.")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(out_dir)

    if args.status:
        print_status(manifest)
        return

    if not args.book_id and not args.book_ids and not args.category_ids:
        parser.error(
            "Provide at least one of --book_id, --book_ids, or --category_ids (or --status)"
        )

    session = CloudflareCookieManager(
        get_session(user_agent=args.user_agent),
    )

    if args.cf_clearance:
        cf_txt = Path.home() / "shamela" / "cf.txt"
        cf_txt.parent.mkdir(parents=True, exist_ok=True)
        cf_txt.write_text(args.cf_clearance)
        session._load_cookie()

    queue = build_book_queue(session, args, manifest)
    if args.max_books:
        queue = queue[: args.max_books]

    print(f"\n[*] {len(queue)} book(s) queued for processing\n")

    done = errors = 0
    for i, (book_id, label) in enumerate(queue, 1):
        print(f"[{i}/{len(queue)}] {'─' * 50}")
        try:
            process_book(session, book_id, out_dir, manifest, label, args)
        except KeyboardInterrupt:
            print("\n[!] Interrupted by user. Progress saved — re-run to resume.")
            sys.exit(1)
        except Exception as e:
            import traceback as _tb

            print(f"[!] Unhandled error on book {book_id}: {e}")
            _tb.print_exc()
            entry = manifest.book(book_id)
            entry["status"] = "error"
            entry["error"] = str(e)
            entry["traceback"] = _tb.format_exc()
            manifest.save()
        status = manifest.book(book_id).get("status")
        if status == "done":
            done += 1
        elif status in ("error", "paused", "in_progress"):
            errors += 1
        if i < len(queue):
            time.sleep(args.delay)

    print(
        f"\n[✓] Run finished. done={done}  error/incomplete={errors}  total={len(queue)}"
    )
    for bid, _ in queue:
        entry = manifest.book(bid)
        st = entry.get("status", "?")
        detail = f"  [{st}] book {bid}: {entry.get('title', '?')}"
        if st == "error":
            detail += f"\n         → {entry.get('error', '(no message)')}"
        elif st == "paused":
            detail += f"  (next_page_id={entry.get('next_page_id')}, pages_scraped={entry.get('pages_scraped')})"
        print(detail)
    print(f"    Manifest: {manifest.path}")
    print(
        f"    Run again with the same command at any time to resume incomplete books."
    )


if __name__ == "__main__":
    main()
