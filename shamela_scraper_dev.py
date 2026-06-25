#!/usr/bin/env python3
"""
shamela_scraper.py — Scrape book(s) from shamela.ws and export to structured PDF + JSON.

NEW IN THIS VERSION
────────────────────
  • Download by category id(s):           --category_ids 123,45,355
  • Download by one or many book ids:      --book_ids 12762,667
  • Resumable downloads — exact page-level checkpointing per book via
    <book_dir>/progress.json + <book_dir>/pages.jsonl (append-only).
    Re-running the same command later picks up exactly where it left off.
  • Structured output tree:
        <out_dir>/<category_id>_<category_name>/<author_id>_<author_name>/<book_id>_<title>/
            book_<id>.json
            book_<id>.pdf
            meta.json
            author_info.json
            pages.jsonl        (raw, append-only — source of truth while scraping)
            progress.json      (resume checkpoint)
  • Global manifest at <out_dir>/manifest.json tracks every book/author/category
    ever processed (status, last page scraped, folder location, ...).
  • Deeply-nested table-of-contents support: the TOC is now parsed as a true
    tree (children follow nested <ul> elements) instead of a flat list, so
    multi-level betaka-index structures are no longer flattened/lost.
    `meta["toc"]` = nested tree, `meta["toc_flat"]` = flattened w/ level,
    `meta["toc_summary"]` = quick stats (entry count / max depth).

Usage
─────
    # single book (legacy, still works)
    python shamela_scraper.py --book_id 12762

    # several explicit books
    python shamela_scraper.py --book_ids 12762,667,151109

    # everything in one or more categories
    python shamela_scraper.py --category_ids 13,33,40

    # mix & match, custom output dir, resume automatically
    python shamela_scraper.py --category_ids 13 --book_ids 667 --out_dir ./library

    # re-run any of the above later — already-finished books are skipped,
    # partially-downloaded books resume from the exact next page id.

    # force re-download even if marked done
    python shamela_scraper.py --book_id 12762 --force

    # only rebuild PDFs from already-scraped data (no network for pages)
    python shamela_scraper.py --book_id 12762 --pdf_only

    # quick look at what has been downloaded so far
    python shamela_scraper.py --out_dir ./library --status
"""

import argparse
import datetime
import json
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from weasyprint import HTML as WeasyprintHTML
except ImportError:  # pdf building simply unavailable until installed
    WeasyprintHTML = None

# ─────────────────────────────────────────────────────────────────────────────
BASE = "https://shamela.ws"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en;q=0.9",
}


# ═══════════════════════════════════════════════════════════════════════════
# SMALL UTILITIES
# ═══════════════════════════════════════════════════════════════════════════

_ILLEGAL_CHARS = re.compile(r'[^\w\s\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF-]')


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
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
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
        self.data = load_json(self.path, None) or {"books": {}, "categories": {}, "authors": {}}

    def save(self):
        atomic_write_json(self.path, self.data)

    def book(self, book_id) -> dict:
        return self.data["books"].setdefault(str(book_id), {})


# ═══════════════════════════════════════════════════════════════════════════
# SESSION
# ═══════════════════════════════════════════════════════════════════════════

def get_session(cf_clearance: str = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    if cf_clearance:
        s.cookies.set("cf_clearance", cf_clearance, domain="shamela.ws")
    return s


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
        for tag in li.find_all("a", href=True):
            if "exp_bu" not in tag.get("class", []):
                a = tag
                break
        node = {"label": None, "page_id": None, "level": level}
        if a:
            node["label"] = a.get_text(strip=True)
            m = re.search(r"/book/\d+/(\d+)", a.get("href", ""))
            if m:
                node["page_id"] = int(m.group(1))
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
        flat.append({
            "label": node.get("label"),
            "page_id": node.get("page_id"),
            "level": node.get("level", 0),
        })
        if node.get("children"):
            flat.extend(flatten_toc(node["children"]))
    return flat


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
            if line.startswith("الكتاب:"):
                meta["title"] = line.replace("الكتاب:", "").strip()
            elif line.startswith("المؤلف:"):
                meta["author"] = line.replace("المؤلف:", "").strip()
            elif line.startswith("الناشر:"):
                meta["publisher"] = line.replace("الناشر:", "").strip()
            elif line.startswith("الطبعة:"):
                meta["edition"] = line.replace("الطبعة:", "").strip()
            elif line.startswith("عدد الأجزاء:"):
                meta["volumes"] = line.replace("عدد الأجزاء:", "").strip()

    # ── author page link ────────────────────────────────────────────────
    author_link = soup.find("a", href=re.compile(r"/author/\d+"))
    if author_link:
        meta["author_url"] = BASE + author_link["href"] if author_link["href"].startswith("/") else author_link["href"]
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
            if any(k in cls.lower() or k in did.lower()
                   for k in ("fihris", "index", "toc", "contents")):
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
            1 for n in flat
            if n.get("label") and re.fullmatch(r"[\u0660-\u0669٠-٩\d\s]+", n["label"].strip())
        )
        return bare >= len(flat) * 0.8   # 80 %+ bare → treat as bare

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

    meta["toc"] = toc_tree
    meta["toc_flat"] = flatten_toc(toc_tree)
    meta["toc_summary"] = toc_summary_stats(toc_tree)

    # ── derive volume start page IDs from volume dropdown (if present)──
    vol_links = soup.select("ul.dropdown-menu a[href*='/book/']")
    volumes_pages = []
    for a in vol_links:
        m = re.search(r"/book/\d+/(\d+)", a["href"])
        if m:
            volumes_pages.append(int(m.group(1)))
    if volumes_pages:
        meta["volume_start_pages"] = volumes_pages

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

def fetch_category_books(session: requests.Session, category_id: int, delay: float = 0.5) -> dict:
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
        except requests.RequestException as e:
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
            book["title"] = title_span.get_text(strip=True) if title_span else book_a.get_text(strip=True)

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
    COLORS = {"c1": "#5f0000", "c2": "#005300", "c3": "#686800",
              "c4": "#707070", "c5": "#005c81"}

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
                out.append(f'<span style="color:{COLORS[color_class]}">'
                           f'{_html_esc(inner)}</span>')
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
            is_hamesh  = "hamesh"   in classes or "footnote" in classes
            is_heading = "b"        in classes or "head"     in classes or "title" in classes
            inner_html = _clean_inline(p)

            if not inner_html.strip():
                continue

            # Skip paragraphs that only contain "..." (ellipsis placeholders)
            plain = _strip_tags(inner_html).strip()
            if re.fullmatch(r'\.+', plain):
                continue

            if is_hamesh:
                lines = [frag.strip() for frag in re.split(r"<br\s*/?>", inner_html) if frag.strip()]
                if lines:
                    paragraphs.append({"type": "hamesh", "lines": lines})
            elif is_heading:
                paragraphs.append({"type": "heading", "lines": [inner_html]})
            else:
                paragraphs.append({"type": "text", "lines": [inner_html]})

        if paragraphs:
            results.append({
                "page_id": int(pid) if pid else None,
                "page_num": int(pnum) if pnum else None,
                "paragraphs": paragraphs,
                # flat plain text for RAG / backward compat
                "text": "\n\n".join(
                    "\n".join(_strip_tags(l) for l in p["lines"])
                    for p in paragraphs
                ),
            })

    # fallback
    if not results:
        nass = soup.find("div", class_="nass")
        if nass:
            for div in nass.find_all("div", recursive=False):
                t = div.get_text(" ", strip=True)
                if len(t) > 20:
                    results.append({
                        "page_id": None, "page_num": None,
                        "paragraphs": [{"type": "text", "lines": [_html_esc(t)]}],
                        "text": t,
                    })

    return results


def get_next_page_id(html: str) -> int | None:
    """Extract next page ID from the 'load next' button."""
    soup = BeautifulSoup(html, "lxml")
    btn = soup.find("button", {"id": "bu_load_next"})
    if btn and btn.get("data-next-id"):
        return int(btn["data-next-id"])
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
    """Worker target: (session, book_id, page_id, stagger_delay) → (page_id, html, err)."""
    session, book_id, page_id, stagger = args_tuple
    if stagger > 0:
        time.sleep(stagger)
    url = f"{BASE}/book/{book_id}/{page_id}"
    try:
        resp = session.get(url, timeout=25)
        if resp.status_code == 403:
            return page_id, None, "403 Forbidden — try --cf_clearance"
        resp.raise_for_status()
        return page_id, resp.text, None
    except requests.RequestException as e:
        return page_id, None, str(e)


def _chain_walk(
    session: requests.Session,
    book_id: int,
    start_id: int,
    workers: int,
    delay: float,
    progress: bool,
    label: str = "[ids]",
) -> tuple[list[int], dict[int, str]]:
    """
    Walk the linked list of pages forward from start_id (inclusive) by
    following the "load next" button's data-next-id, in speculative
    concurrent batches of `workers`. Stops when the chain genuinely ends
    (no next id) or a fetch fails. Returns (ids_in_order, html_cache) where
    html_cache lets the caller skip re-fetching anything found here.
    """
    ids_in_order: list[int] = []
    html_cache: dict[int, str] = {}

    current_id: int | None = start_id
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while current_id is not None:
            speculative = [current_id + i for i in range(workers)]
            stagger = delay / max(workers, 1)
            futs = {
                pool.submit(_fetch_one, (session, book_id, pid, i * stagger)): pid
                for i, pid in enumerate(speculative)
            }
            results: dict[int, tuple[str | None, str | None]] = {}
            for f in as_completed(futs):
                pid = futs[f]
                _, html, err = f.result()
                results[pid] = (html, err)

            advanced = False
            for pid in speculative:
                html, err = results.get(pid, (None, "not fetched"))
                if err or html is None:
                    break
                next_id = get_next_page_id(html)
                ids_in_order.append(pid)
                html_cache[pid] = html
                current_id = next_id
                advanced = True
                if next_id is None:
                    current_id = None
                    break
                if next_id != pid + 1:
                    break

            if not advanced:
                html, err = results.get(current_id, (None, "no result"))
                if err or html is None:
                    if progress:
                        print(f"\n  [!] chain-walk error at {current_id}: {err}")
                    break
                ids_in_order.append(current_id)
                html_cache[current_id] = html
                current_id = get_next_page_id(html)

            if progress:
                print(f"\r  {label} discovered {len(ids_in_order)} page IDs …",
                      end="", flush=True)

    if progress and ids_in_order:
        print(f"\r  {label} {len(ids_in_order)} page IDs discovered          ")

    return ids_in_order, html_cache


def _chain_walk_until(
    session: requests.Session,
    book_id: int,
    start_id: int,
    stop_id: int,
    workers: int,
    delay: float,
    progress: bool,
    label: str = "[ids:gap]",
) -> tuple[list[int], dict[int, str]]:
    """
    Like _chain_walk, but bounded: walks forward from start_id (which the
    caller already knows is a valid page — e.g. a TOC anchor) and stops as
    soon as the chain reaches or passes stop_id (the next TOC anchor).

    Used to resolve one irregular gap in the TOC (a section the TOC didn't
    label page-by-page) without walking the rest of the book — cost scales
    with the size of *this* gap, not the whole book.

    Returns (ids_in_order, html_cache); start_id itself is NOT included in
    ids_in_order (the caller already has it from the TOC).
    """
    ids_in_order: list[int] = []
    html_cache: dict[int, str] = {}

    # Safety cap: a gap of size N shouldn't ever need to fetch more than a
    # small multiple of N pages to resolve, even with some non-sequential
    # jumps. Guards against an unexpected infinite/very-long walk.
    max_fetches = max(50, (stop_id - start_id) * 3)
    fetched = 0

    current_id: int | None = start_id
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while current_id is not None and current_id < stop_id and fetched < max_fetches:
            speculative = [pid for pid in (current_id + i for i in range(workers)) if pid < stop_id]
            if not speculative:
                break
            stagger = delay / max(workers, 1)
            futs = {
                pool.submit(_fetch_one, (session, book_id, pid, i * stagger)): pid
                for i, pid in enumerate(speculative)
            }
            results: dict[int, tuple[str | None, str | None]] = {}
            for f in as_completed(futs):
                pid = futs[f]
                _, html, err = f.result()
                results[pid] = (html, err)
            fetched += len(speculative)

            advanced = False
            for pid in speculative:
                html, err = results.get(pid, (None, "not fetched"))
                if err or html is None:
                    break
                next_id = get_next_page_id(html)
                if pid != start_id:
                    ids_in_order.append(pid)
                html_cache[pid] = html
                current_id = next_id
                advanced = True
                if next_id is None or next_id >= stop_id:
                    current_id = None
                    break
                if next_id != pid + 1:
                    break

            if not advanced:
                html, err = results.get(current_id, (None, "no result"))
                if err or html is None:
                    if progress:
                        print(f"\n  [!] {label} error at {current_id}: {err}")
                    break
                if current_id != start_id:
                    ids_in_order.append(current_id)
                html_cache[current_id] = html
                current_id = get_next_page_id(html)
                fetched += 1

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
    toc_ids  = sorted({e["page_id"] for e in toc_flat if e.get("page_id")})
    if len(toc_ids) >= 2:
        filled: list[int] = []
        extra_cache: dict[int, str] = {}
        walked_segments = 0
        walked_pages = 0

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
                seg_ids, seg_cache = _chain_walk_until(
                    session, book_id, tid, nxt, workers, delay,
                    progress=False, label="[ids:gap]",
                )
                walked_segments += 1
                walked_pages += len(seg_ids)
                extra_cache.update(seg_cache)
                for val in seg_ids:
                    if val >= start_page:
                        filled.append(val)

        if walked_segments and progress:
            print(f"  [ids] {walked_segments} irregular TOC gap(s) chain-walked "
                  f"({walked_pages} pages) — rest derived directly from TOC")

        # The gap-fill above only covers *between* TOC entries — the final
        # section's trailing pages (after the last TOC anchor, up to the
        # book's actual last page) are never touched.
        cached_last_id = (prog or {}).get("book_last_page_id")
        new_tail: list[int] = []
        if cached_last_id is not None and cached_last_id >= toc_ids[-1]:
            # Already discovered the book's true end on a previous run —
            # no need to re-walk it, just fill the known range.
            new_tail = [x for x in range(toc_ids[-1] + 1, cached_last_id + 1) if x >= start_page]
        else:
            tail_ids, tail_cache = _chain_walk(
                session, book_id, toc_ids[-1], workers, delay,
                progress=False, label="[ids:tail]",
            )
            full_new_tail = tail_ids[1:]   # tail_ids[0] is toc_ids[-1] itself
            new_tail = [x for x in full_new_tail if x >= start_page]
            extra_cache.update(tail_cache)
            if prog is not None:
                prog["book_last_page_id"] = max(toc_ids[-1], tail_ids[-1] if tail_ids else toc_ids[-1])

        filled.extend(new_tail)
        if filled:
            if progress:
                extra = f", +{len(new_tail)} trailing pages past last TOC entry" if new_tail else ""
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
    pages_path    = book_dir / "pages.jsonl"

    prog = load_json(progress_path, None)
    if prog and prog.get("status") == "done" and not force:
        if progress:
            print(f"  [skip] book {book_id}: already fully scraped "
                  f"({prog.get('pages_scraped', 0)} pages)")
        return prog

    already_scraped = 0
    resume_from_id  = start_page

    if not prog or force:
        prog = {
            "book_id": book_id, "status": "in_progress",
            "next_page_id": start_page, "last_page_id": None, "pages_scraped": 0,
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
            print(f"  [heal] progress.json was behind pages.jsonl on disk — "
                  f"correcting resume point to page_id={resume_from_id} "
                  f"({already_scraped} pages on disk)")
        if progress:
            print(f"  [resume] from page_id={resume_from_id} "
                  f"({already_scraped} pages already saved)")

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

    # Persist a newly-discovered book-end boundary right away so an
    # interrupt before any pages finish still saves the benefit of the
    # tail-walk (otherwise it'd be silently re-walked next run).
    if prog.get("book_last_page_id") is not None:
        atomic_write_json(progress_path, prog)

    if not page_ids:
        prog["status"] = "error"
        prog["error"]  = "Could not discover any page IDs"
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

    results_buf: dict[int, list[dict]] = {}   # page_id → parsed page data
    count = already_scraped
    error_seen: str | None = None

    write_cursor = 0   # index into page_ids of next page to write

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
            future_to_pid: dict = {}
            for i, pid in enumerate(page_ids):
                if pid in html_cache:
                    # Already fetched during discovery — wrap in a trivial future
                    html = html_cache.pop(pid)  # free memory as we go
                    f = pool.submit(lambda h=html, p=pid: (p, h, None))
                else:
                    f = pool.submit(_fetch_one,
                                    (session, book_id, pid, (i % workers) * stagger))
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
                    results_buf[pid_result] = []   # empty = skipped
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
                        pf.write(json.dumps(pg, ensure_ascii=False) + "\n")
                        count += 1
                    write_cursor += 1
                    flushed_any = True

                if flushed_any:
                    pf.flush()
                    last_written = page_ids[write_cursor - 1]
                    next_pending = page_ids[write_cursor] if write_cursor < len(page_ids) else None
                    prog["last_page_id"]  = last_written
                    prog["next_page_id"]  = next_pending
                    prog["pages_scraped"] = count
                    prog["status"]        = "in_progress"
                    # Checkpoint periodically (not every page — avoids I/O bottleneck)
                    if write_cursor % checkpoint_every == 0 or next_pending is None:
                        atomic_write_json(progress_path, prog)

                if progress:
                    pct = int(100 * len(completed_set) / total)
                    print(f"\r  fetched {len(completed_set)}/{total} ({pct}%)  "
                          f"written {count - already_scraped}  ",
                          end="", flush=True)
        except KeyboardInterrupt:
            if progress:
                print("\n  [!] Interrupted — cancelling queued fetches "
                      "(a few in-flight requests may still finish) …")
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
        prog["error"]  = error_seen
    elif write_cursor < len(page_ids):
        prog["status"] = "paused"
    else:
        prog["status"]       = "done"
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

def _truncate(title: str, max_words: int = 5) -> str:
    """Truncate a title to max_words; if longer keeps first few + '...' + last word."""
    words = title.split()
    if len(words) <= max_words:
        return title
    return ' '.join(words[:max_words - 1]) + ' … ' + words[-1]



# NOTE: Visible TOC rendering has been removed per prompt_toc.txt requirements.
# The PDF's native outline/bookmark feature (via WeasyPrint bookmark-level CSS)
# is used instead.  The TOC tree is still parsed and stored in meta["toc"] /
# meta["toc_flat"] for downstream use, but no visible TOC page is produced.


def _build_html_css(meta: dict) -> str:
    """Return the full <style> block CSS string (shared by stream and legacy paths)."""
    import datetime as _dt  # noqa: F401 — kept for callers that need it

    font_body = '"Scheherazade New", "Noto Naskh Arabic", "Amiri Quran", "Amiri", serif'
    font_head = '"Noto Kufi Arabic", "Scheherazade New", "Amiri", serif'

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
    GOLD       = "#b8860b"
    GOLD_L     = "#daa520"
    GOLD_D     = "#6b4f10"
    TEAL       = "#1a3a4a"
    RUST       = "#8b2e00"
    INK        = "#1a1008"
    CREAM      = "#fefcf7"
    PARCHMENT  = "#fcf8ef"
    SEPIA      = "#5c3d1a"
    BROWN      = "#3e2712"


    css = f"""
    /* ═══════════════════════════════════════════════════════
       PAGE GEOMETRY  —  tight margins = maximum words per page
       ═══════════════════════════════════════════════════════ */

    /* Front-matter: roman numerals, no running header */
    @page front-matter {{
        size: A4;
        margin: 1.6cm 1.8cm 2cm 1.8cm;
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
        margin: 1.6cm 1.8cm 2cm 1.8cm;
        background: {CREAM};

        @top-left {{
            content: counter(page);
            font-family: {font_body};
            font-size: 8pt;
            font-weight: 500;
            color: {RUST};
            padding-left: 0.5em;
        }}

        @top-center {{ content: none; }}

        @top-right {{ content: none; }}

        @bottom-center {{
            content: "— " counter(page) " —";
            font-family: {font_body};
            font-size: 9pt;
            font-weight: 500;
            color: {RUST};
        }}
        @bottom-left  {{ content: none; }}
        @bottom-right {{ content: none; }}
    }}

    /* Bare @page default (cover) */
    @page {{
        size: A4;
        margin: 1.5cm;
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
        font-size: 14pt;
        font-weight: 700;
        line-height: 1.9;
        direction: rtl;
        text-align: justify;
        text-justify: kashida;
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
        border: 5px double {SEPIA};
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

    /* Volume divider splash */
    .volume-divider {{
        page-break-before: always;
        page-break-after: always;
        text-align: center;
        padding-top: 7cm;
        background: #fdfaf4;
    }}
    .volume-divider h2 {{
        font-size: 30pt;
        color: {GOLD};
        border: none;
        letter-spacing: 0.08em;
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
        font-size: 15pt;
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
        font-size: 16pt;
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
        font-size: 15pt;
        font-weight: 800;
        color: {TEAL};
        margin: 0.25em 0 0.15em 0;
        padding: 0;
        line-height: 1.5;
        bookmark-level: none;
        prince-bookmark-level: none;
    }}
    .toc-numbered-heading.toc-nh-0 {{
        font-size: 20pt;
        color: {GOLD_L};
        margin-top: 0.1em;
    }}
    .toc-numbered-heading.toc-nh-1,
    .toc-numbered-heading.toc-nh-implicit {{
        font-size: 18pt;
        color: {RUST};
    }}
    .toc-numbered-heading.toc-nh-2 {{
        font-size: 16pt;
        color: {TEAL};
    }}
    .toc-numbered-heading.toc-nh-3 {{
        font-size: 15pt;
        color: {SEPIA};
    }}
    /* Auto-detected bracket headings with no matching TOC entry. */
    .toc-numbered-heading.toc-nh-auto {{
        font-style: italic;
        color: #8a8a8a;
        font-weight: 600;
        border-right: 3px dotted {GOLD_L};
        padding-right: 0.4em;
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
        text-justify: kashida;
        orphans: 2;
        widows: 2;
        font-weight: 500;
    }}

    /* ═══════════════════════════════════════════════════════
       FOOTNOTES (hamesh)
       ═══════════════════════════════════════════════════════ */
    .hamesh {{
        border-top: 1px solid {GOLD_L};
        margin-top: 0.8em;
        padding-top: 0.3em;
        font-size: 10pt;
        font-weight: 400;
        color: #555;
        line-height: 1.6;
    }}
    .hamesh p {{
        margin: 0.05em 0;
        text-align: right;
    }}
    """
    return css


def _plain_text(html_frag: str) -> str:
    """Strip HTML tags from a fragment for text comparison."""
    return re.sub(r'<[^>]+>', '', html_frag)


_BRACKET_STRIP_CHARS = "[]（）()「」【】《》〈〉"
_TASHKEEL_RE = re.compile(r'[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]')
_PAREN_GROUP_RE = re.compile(r'(\([^()]*\)|«[^«»]*»)')


def _normalize_ar(s: str) -> str:
    """Normalize Arabic for comparing page text against a TOC label."""
    s = _plain_text(s)
    s = _TASHKEEL_RE.sub("", s)
    s = _PAREN_GROUP_RE.sub("", s)
    s = s.strip(_BRACKET_STRIP_CHARS + " :،ـ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _bracket_stripped(line_html: str) -> str | None:
    plain = _plain_text(line_html).strip()
    if plain.startswith("[") and plain.endswith("]"):
        import html as _html_mod
        return _html_mod.unescape(plain.strip(_BRACKET_STRIP_CHARS).strip())
    return None


def _extract_bracket_headings(paras: list[dict] | None) -> list[dict]:
    found: list[dict] = []
    if not paras:
        return found
    for pi, para in enumerate(paras):
        if para.get("type") == "hamesh":
            continue
        for li, line in enumerate(para.get("lines", [])):
            stripped = _bracket_stripped(line)
            if stripped:
                found.append({
                    "para_idx": pi, "line_idx": li,
                    "ptype": para.get("type"), "text": stripped,
                })
    return found


def _label_words(norm_label: str) -> list[str]:
    return [w for w in norm_label.split() if len(w) >= 2]


def _find_label_in_lines(norm_label: str, flat_lines: list[tuple[int, int, str]],
                         cursor: int) -> tuple[tuple[int, int] | None, bool]:
    """
    Find where a TOC label appears in page text.
    Returns (start, end) line span and keep_line (True when the matched line
    carries extra body text beyond the TOC label — show both heading and line).
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
            if (span_len == 1 and len(norm_label) >= 4
                    and flat_lines[start][2].startswith(norm_label)):
                return (start, start + 1), flat_lines[start][2] != norm_label

    label_words = _label_words(norm_label)
    for start in range(cursor, len(flat_lines)):
        line_norm = flat_lines[start][2]
        if len(norm_label) >= 4 and norm_label in line_norm:
            return (start, start + 1), line_norm != norm_label
        if label_words and all(w in line_norm for w in label_words):
            return (start, start + 1), line_norm != norm_label

    return None, False


def _locate_toc_headings_in_page(paras: list[dict] | None,
                                  toc_pool: list[tuple[str, int, str]]
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
            norm = _normalize_ar(line)
            if norm:
                flat_lines.append((pi, li, norm))

    resolved: list[dict] = []
    pending: list[tuple[str, int, str]] = []
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
        for label, level, number in pending:
            _append_top({
                "text": label, "level": level, "number": number,
                "matched": False, "auto": False, "implicit": True,
                "keep_line": False, "suppress": [],
            })
        pending = []

    def _flush_pending_before_child(child_positions: list[tuple[int, int]]):
        """
        Flush pending (unmatched parent) entries, positioning them just before
        the child that triggered the flush.

        If the child is at the page top (first real line, cursor == 0) we use
        top_slot so that all pre-body headings cluster together at the top.

        If the child is mid-page we still send the parents to page-top — they
        have no text match on this page and belong at the top as section titles,
        not buried mid-page before an arbitrary sibling's position.
        """
        nonlocal pending, top_slot
        if not pending:
            return
        child_pi, child_li = child_positions[0]
        at_first_line = (cursor == 0
                         and child_pi == flat_lines[0][0]
                         and child_li == flat_lines[0][1])
        if at_first_line:
            # Child is the very first body content — use before_child inline
            # so the parent heading renders immediately above the child heading.
            for idx, (label, level, number) in enumerate(pending):
                resolved.append({
                    "positions": [(child_pi, child_li)],
                    "text": label, "level": level, "number": number,
                    "matched": False, "auto": False, "implicit": True,
                    "keep_line": False, "suppress": [],
                    "at_page_top": False,
                    "before_child": True,
                    "_before_child_slot": idx,
                })
        else:
            # Child is mid-page — send unmatched parents to page-top instead.
            # Placing them before a mid-page sibling would bury them in the
            # middle of the previous section's content.
            for label, level, number in pending:
                _append_top({
                    "text": label, "level": level, "number": number,
                    "matched": False, "auto": False, "implicit": True,
                    "keep_line": False, "suppress": [],
                })
        pending = []

    for label, level, number in toc_pool:
        norm_label = _normalize_ar(label)
        found_span, keep_line = _find_label_in_lines(norm_label, flat_lines, cursor)
        if found_span:
            start, end = found_span
            positions = [(flat_lines[i][0], flat_lines[i][1]) for i in range(start, end)]
            if start > cursor and cursor == 0:
                # Body text appears before the first matched heading on this
                # page — hoist both pending parents and this entry to page-top
                # so they title the whole page in the correct parent→child order.
                _flush_pending_as_top()
                _append_top({
                    "text": label, "level": level, "number": number,
                    "matched": True, "auto": False, "implicit": False,
                    "keep_line": False, "suppress": list(positions),
                })
            else:
                # cursor > 0: a prior heading already claimed the page top.
                # This is a sibling heading found mid-page after its predecessor's
                # content — keep it inline so content flows between siblings.
                # Also covers start == cursor (heading right at current position).
                _flush_pending_before_child(positions)
                suppress = [] if keep_line else list(positions)
                resolved.append({
                    "positions": positions, "text": label, "level": level,
                    "number": number, "matched": True, "auto": False,
                    "implicit": False, "keep_line": keep_line,
                    "suppress": suppress, "at_page_top": False,
                })
            cursor = end
        else:
            pending.append((label, level, number))

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
            if (not h.get("at_page_top") and not h.get("before_child")
                    and h["positions"][0] == (first_pi, first_li)):
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
                h["suppress"] = []
            resolved = promoted + remaining

    return resolved


def resolve_book_headings(meta: dict, pages_iter):
    """
    Enrich each page with toc_breadcrumb, toc_number, resolved_headings,
    and unanchored_toc_entries (TOC labels with no text match on the page).
    """
    toc_flat = meta.get("toc_flat", [])

    _stack: list[tuple[int, int]] = []
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
                if parent.get("page_id") is None:
                    parent["page_id"] = pid
                else:
                    break
        _stack.append((lvl, i))

    sorted_toc = sorted(
        [e for e in toc_flat if e.get("page_id") is not None],
        key=lambda e: (e["page_id"], e.get("level", 0))
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
        page_toc_new: list[tuple[str, int, str]] = []

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
                    breadcrumb_stack.append((number, label, level))
                    page_toc_new.append((label, level, number))
                toc_idx += 1

        if page_toc_new:
            labels = [l for l, _, _ in page_toc_new]
            numbers = [n for _, _, n in page_toc_new]
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

        # Also suppress any plain-text line whose normalized form matches an
        # already-resolved heading label — these are unbracketed echoes of
        # bracket headings (e.g. "الأصْلُ الثَّانِي" after "[الأصل الثاني...]").
        resolved_norms: set[str] = {_normalize_ar(h["text"]) for h in located}
        echo_suppressed: list[list[int]] = []
        paras_here = page.get("paragraphs") or []
        for pi, para in enumerate(paras_here):
            if para.get("type") == "hamesh":
                continue
            for li, line in enumerate(para.get("lines", [])):
                if (pi, li) in consumed_positions:
                    continue
                if _normalize_ar(line) in resolved_norms:
                    consumed_positions.add((pi, li))
                    echo_suppressed.append([pi, li])

        deepest_level = breadcrumb_stack[-1][2] if breadcrumb_stack else -1
        deepest_number = breadcrumb_stack[-1][0] if breadcrumb_stack else "0"
        for h in located:
            deepest_level, deepest_number = h["level"], h["number"]

        auto_n = 0
        resolved: list[dict] = list(located)
        for bh in _extract_bracket_headings(page.get("paragraphs")):
            pos = (bh["para_idx"], bh["line_idx"])
            if pos in consumed_positions:
                continue
            auto_n += 1
            auto_level = min(deepest_level + 1, 6)
            auto_number = f"{deepest_number}.u{auto_n}"
            resolved.append({
                **bh, "positions": [pos], "level": auto_level,
                "number": auto_number, "matched": False, "auto": True,
                "implicit": False,
            })

        resolved.sort(key=lambda h: h["positions"][0])

        page["resolved_headings"] = resolved
        page["echo_suppressed_positions"] = echo_suppressed  # render path uses this
        page["unanchored_toc_entries"] = [
            {"label": h["text"], "level": h["level"], "number": h["number"]}
            for h in resolved if h.get("implicit")
        ]

        yield page


def materialize_resolved_pages(meta: dict, pages_path: Path, resolved_path: Path) -> int:
    count = 0
    with open(resolved_path, "w", encoding="utf-8") as out_fh:
        for page in resolve_book_headings(meta, iter_pages_jsonl(pages_path)):
            out_fh.write(json.dumps(page, ensure_ascii=False) + "\n")
            count += 1
    return count


def _render_page_html(page: dict, toc_by_page: dict, vol_boundaries: set,
                       vol_num_ref: list) -> tuple[str, int]:
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
            f'{_e(page_breadcrumb)}</div>\n'
        )
    if page_toc_number:
        parts.append(
            f'<div class="bm-setter" '
            f'style="position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;overflow:hidden;font-size:0;string-set:toc-number content()">'
            f'{_e(page_toc_number)}</div>\n'
        )

    paras = page.get("paragraphs")
    # heading_by_start: position → ordered list of headings at that position.
    # A position can hold multiple headings when an unmatched parent is placed
    # "before_child" at the same (pi, li) as its matched child.
    heading_by_start: dict[tuple[int, int], list[dict]] = {}
    suppressed_positions: set[tuple[int, int]] = set()

    page_start_headings: list[dict] = []
    for h in resolved_headings:
        for pos in h.get("suppress", []):
            suppressed_positions.add(tuple(pos))
        pi, li = h["positions"][0]
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
        hlist.sort(key=lambda h: (
            0 if h.get("before_child") else 1,
            h.get("_before_child_slot", 0),
        ))

    def _heading_html(h: dict) -> str:
        if h.get("auto"):
            nh_class = "auto"
        elif h.get("implicit"):
            nh_class = "implicit"
        else:
            nh_class = h["level"]
        label_text = f'{h["number"]}. {_e(h["text"])}'
        return (f'<p class="toc-numbered-heading toc-nh-{nh_class} toc-bm-{h["level"]}">'
                f'{label_text}</p>\n')

    entry_open = [False]

    def _open_entry():
        if not entry_open[0]:
            parts.append('<div class="page-entry"><div class="page-text">')
            entry_open[0] = True

    def _close_if_open():
        if entry_open[0]:
            parts.append('</div></div>')
            entry_open[0] = False

    has_body = paras or page.get("text")
    if has_body:
        _open_entry()
        for h in sorted(page_start_headings, key=lambda x: x["positions"][0]):
            parts.append(_heading_html(h))

    if paras:
        for pi, para in enumerate(paras):
            ptype = para["type"]
            if ptype == "hamesh":
                parts.append('<div class="hamesh">')
                for line in para["lines"]:
                    if line.strip():
                        parts.append(f'<p>{line.strip()}</p>')
                parts.append('</div>')
                continue

            for li, line in enumerate(para["lines"]):
                if not line.strip():
                    continue
                headings_here = heading_by_start.get((pi, li))
                if headings_here:
                    for heading in headings_here:
                        parts.append(_heading_html(heading))
                    # Skip the source line unless the last real (non-before_child) heading wants it
                    last_real = next(
                        (h for h in reversed(headings_here) if not h.get("before_child")),
                        None
                    )
                    if last_real is None or not last_real.get("keep_line"):
                        continue
                if (pi, li) in suppressed_positions:
                    continue
                if ptype == "heading":
                    parts.append(f'<p class="chapter-heading">{line.strip()}</p>')
                else:
                    parts.append(f'<p>{line.strip()}</p>')
    elif page.get("text"):
        for chunk in page["text"].split("\n\n"):
            if chunk.strip():
                parts.append(f'<p>{_e(chunk.strip())}</p>')

    _close_if_open()
    return "".join(parts), vol_num


def _ensure_resolved(meta: dict, pages_iter):
    pages_iter = iter(pages_iter)
    try:
        first = next(pages_iter)
    except StopIteration:
        return
    import itertools
    chained = itertools.chain([first], pages_iter)
    if "resolved_headings" in first:
        yield from chained
    else:
        yield from resolve_book_headings(meta, chained)


def build_html_to_file(meta: dict, author_info: dict, pages_iter, out_path: Path,
                       flush_every: int = 50) -> None:
    """
    Stream-write the full HTML document to *out_path* one page at a time.
    Memory usage is O(flush_every pages) instead of O(all pages).

    pages_iter may be a list or any iterable of page dicts.
    flush_every: write buffer to disk every N pages (default 50).
    """
    import datetime

    css = _build_html_css(meta)

    # Build page_id -> [(label, toc_level), ...] lookup
    toc_flat = meta.get("toc_flat", [])
    toc_by_page: dict = {}
    for entry in toc_flat:
        pid = entry.get("page_id")
        if pid is not None:
            toc_by_page.setdefault(pid, []).append(
                (entry.get("label", ""), entry.get("level", 0))
            )

    vol_starts_list = sorted(meta.get("volume_start_pages", []))
    vol_boundaries = set(vol_starts_list)
    vol_num = [1]   # mutable reference

    scraped_date = datetime.date.today().strftime("%Y-%m-%d")

    with open(out_path, "w", encoding="utf-8", buffering=1 << 20) as fh:  # 1 MB write buffer
        # ── DOCTYPE + CSS ──────────────────────────────────────────────
        fh.write(
            f'<!DOCTYPE html><html lang="ar" dir="rtl"><head>'
            f'<meta charset="UTF-8">'
            f'<style>{css}</style></head><body>\n'
        )

        # Hidden span: sets book-title and chapter-title to book title (fallback)
        book_title = meta.get("title", "كتاب")
        fh.write(f'<span style="display:none; string-set: book-title content(), chapter-title content()">'
                 f'{_e(_truncate(book_title))}</span>\n')
        fh.write(f'<span style="display:none; string-set: toc-number content()"></span>\n')

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
        fh.write('<div class="cover-ornament-top">&#10022; &#10023; &#10022; &#10023; &#10022;</div>\n')

        # Bismillah
        fh.write('<div class="cover-bismillah">بِسْمِ اللَّهِ الرَّحْمَنِ الرَّحِيمِ</div>\n')

        # Decorative divider
        fh.write('<div class="cover-divider">&#9473;&#9473;&#9473; &#10022; &#9473;&#9473;&#9473;</div>\n')

        # Book title
        fh.write(f'<h1>{_e(meta.get("title", "كتاب"))}</h1>\n')

        # Meta info table
        rows = []
        for label, key in [("المؤلف", "author"), ("الناشر", "publisher"),
                            ("الطبعة", "edition"), ("عدد الأجزاء", "volumes")]:
            if meta.get(key):
                rows.append(f'<tr><td class="meta-label">{_e(label)}</td>'
                             f'<td>{_e(meta[key])}</td></tr>')
        if rows:
            fh.write(f'<table class="meta-table">{"".join(rows)}</table>\n')

        # Bottom ornament
        fh.write('<div class="cover-ornament-bottom">&#10022; &#10023; &#10022; &#10023; &#10022;</div>\n')

        # Source
        fh.write(f'<p class="source">المصدر: {_e(meta.get("url",""))} | '
                 f'تاريخ التحميل: {scraped_date}</p>\n')

        fh.write('</div><!-- cover-frame-inner -->\n')
        fh.write('</div><!-- cover-frame-outer -->\n')
        fh.write('</div><!-- cover -->\n')

        # ── AUTHOR INFO ────────────────────────────────────────────────
        if author_info and not author_info.get("error"):
            fh.write('<div class="section-front">\n')
            fh.write('<h2>ترجمة المؤلف</h2>\n')
            if author_info.get("bio"):
                fh.write('<div class="author-bio">\n')
                for line in author_info["bio"].splitlines():
                    if line.strip():
                        fh.write(f'<p>{_e(line.strip())}</p>\n')
                fh.write('</div>\n')
            if author_info.get("url"):
                fh.write(f'<p style="font-size:8pt;color:#aaa">'
                         f'المصدر: {_e(author_info["url"])}</p>\n')
            fh.write('</div>\n')

        # ── BOOK PAGES ─────────────────────────────────────────────────
        fh.write('<div class="section-text">\n')
        fh.write('<h2>نص الكتاب</h2>\n')

        buf: list[str] = []
        first_page = True

        for page in _ensure_resolved(meta, pages_iter):
            pid = page.get("page_id")

            if pid and pid in vol_boundaries and not first_page:
                buf.append('</div>\n')
                buf.append(f'<div class="volume-divider">'
                            f'<h2>الجزء {_e(str(vol_num[0] + 1))}</h2></div>\n')
                buf.append('<div class="section">\n')
                vol_num[0] += 1

            frag, _ = _render_page_html(page, toc_by_page, vol_boundaries, vol_num)
            buf.append(frag)
            first_page = False

            if len(buf) >= flush_every:
                fh.write("".join(buf))
                buf.clear()

        if buf:
            fh.write("".join(buf))

        fh.write('</div>\n')
        fh.write('</body></html>\n')


def build_pdf(meta: dict, author_info: dict, pages_iter, out_path: str,
              flush_every: int = 50):
    """
    Build a PDF from *pages_iter* (list or generator of page dicts).

    Strategy (memory-safe):
      1. Stream-write HTML to <out_path>.html — O(flush_every) memory.
      2. Hand WeasyPrint a file:// URL instead of a string — it can
         parse/stream from disk rather than holding the whole HTML in RAM.
      3. The intermediate HTML file is kept for debugging; delete it
         manually if disk space is a concern.
    """
    html_path = Path(out_path).with_suffix(".html")

    print(f"  [html] streaming HTML → {html_path} ...")
    build_html_to_file(meta, author_info, pages_iter, html_path, flush_every=flush_every)
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
# FOLDER STRUCTURE: category / author / book
# ═══════════════════════════════════════════════════════════════════════════

def resolve_book_dir(out_dir: Path, manifest: Manifest, book_id: int,
                      meta: dict, author_info: dict, category_label: str = None) -> Path:
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

def process_book(session: requests.Session, book_id: int, out_dir: Path,
                  manifest: Manifest, category_label: str, args) -> None:
    book_id = int(book_id)
    entry = manifest.book(book_id)

    if entry.get("status") == "done" and not args.force and not args.pdf_only:
        print(f"[skip] book {book_id} ({entry.get('title','')}) — already done")
        return

    print(f"[*] Fetching metadata for book {book_id} ...")
    meta = fetch_book_meta(session, book_id)
    print(f"    title  : {meta.get('title','?')}")
    print(f"    author : {meta.get('author','?')}")
    print(f"    toc    : {meta['toc_summary']['total_entries']} entries, "
          f"max depth {meta['toc_summary']['max_depth']}")

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

    book_dir = resolve_book_dir(out_dir, manifest, book_id, meta, author_info, category_label)

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
    if first_page == 1 and meta.get("toc_flat"):
        first_with_pid = next((n["page_id"] for n in meta["toc_flat"] if n.get("page_id")), None)
        if first_with_pid:
            first_page = first_with_pid
    elif first_page == 1 and meta.get("volume_start_pages"):
        first_page = meta["volume_start_pages"][0]

    # ── scrape (resumable) ──────────────────────────────────────────────
    if not args.pdf_only:
        print(f"[*] Scraping pages for book {book_id} (starting at page_id={first_page}) ...")
        prog = scrape_book_pages_resumable(
            session, book_id, book_dir,
            start_page=first_page, meta=meta, limit=args.limit, delay=args.delay,
            force=args.force, workers=getattr(args, "workers", 8),
        )
        entry["status"] = prog["status"]
        entry["last_page_id"] = prog.get("last_page_id")
        entry["next_page_id"] = prog.get("next_page_id")
        entry["pages_scraped"] = prog.get("pages_scraped")
        entry["updated_at"] = datetime.datetime.now().isoformat()
        manifest.save()
        if prog["status"] == "error":
            print(f"[!] book {book_id} stopped early: {prog.get('error')}")
            print(f"    re-run the same command later to resume from page_id={prog.get('next_page_id')}")
            return

    # ── resolve TOC ↔ content heading alignment ONCE, persist it ─────────
    pages_path = book_dir / "pages.jsonl"
    resolved_path = book_dir / "pages_resolved.jsonl"
    print(f"[*] Resolving headings for book {book_id} ...")
    n_resolved = materialize_resolved_pages(meta, pages_path, resolved_path)
    print(f"    → {n_resolved} pages resolved → {resolved_path}")

    # ── assemble combined JSON ───────────────────────────────────────────
    pages = load_pages_jsonl(resolved_path)
    data = {"meta": meta, "author_info": author_info, "pages": pages}
    json_path = book_dir / f"book_{book_id}.json"
    atomic_write_json(json_path, data)
    print(f"  [json] saved → {json_path}  ({len(pages)} pages)")

    del pages
    import gc; gc.collect()

    # ── PDF ───────────────────────────────────────────────────────────────
    if not args.json_only:
        pdf_path = book_dir / f"book_{book_id}.pdf"
        print(f"[*] Building PDF for book {book_id} ...")
        build_pdf(meta, author_info, iter_pages_jsonl(resolved_path), str(pdf_path),
                  flush_every=getattr(args, "flush_every", 50))

    # Only mark "done" when scraping was actually performed and completed.
    # --pdf_only must never change the scraping status, so interrupted
    # scrapes remain resumable on the next regular run.
    if not args.pdf_only:
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
                print(f"[*] Category {cid_int} ({cat_info.get('name')}) — using cached list "
                      f"({len(cat_info['books'])} books). Use --refresh_categories to re-fetch.")
            else:
                print(f"[*] Fetching category {cid_int} ...")
                cat_info = fetch_category_books(session, cid_int, delay=args.delay)
                manifest.data["categories"][str(cid_int)] = cat_info
                manifest.save()
                print(f"    → {len(cat_info['books'])} books found in '{cat_info.get('name')}'")

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
        print(f"  [{b.get('status','?'):10s}] {bid:>8s}  {b.get('title','?')[:60]}  "
              f"({b.get('pages_scraped',0)} pages)  → {b.get('dir','?')}")


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Scrape shamela.ws book(s)/categories to JSON + PDF")
    parser.add_argument("--book_id", type=int, default=None, help="Single Shamela book ID (legacy)")
    parser.add_argument("--book_ids", default=None, help="Comma-separated list of book IDs, e.g. 12762,667")
    parser.add_argument("--category_ids", default=None, help="Comma-separated list of category IDs, e.g. 13,33,40")
    parser.add_argument("--out_dir", default="./shamela_output", help="Root output directory")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Base courtesy delay between requests in seconds (default 0.5; "
                             "actual per-thread delay = delay × workers)")
    parser.add_argument("--workers", type=int, default=8,
                        help="Concurrent page-fetch threads (default 8; increase for speed, "
                             "decrease if the server rate-limits you)")
    parser.add_argument("--start_page", type=int, default=1, help="First page ID to scrape (per book, if not resuming)")
    parser.add_argument("--limit", type=int, default=None, help="Max pages to scrape PER BOOK")
    parser.add_argument("--cf_clearance", default=None, help="Cloudflare cf_clearance cookie value")
    parser.add_argument("--json_only", action="store_true", help="Only scrape + save JSON, skip PDF")
    parser.add_argument("--pdf_only", action="store_true", help="Rebuild PDF from already-scraped data, skip network page-scraping")
    parser.add_argument("--force", action="store_true", help="Re-scrape books even if already marked done")
    parser.add_argument("--refresh_categories", action="store_true", help="Re-fetch category book listings instead of using cached manifest copy")
    parser.add_argument("--max_books", type=int, default=None, help="Cap total number of books processed this run (testing)")
    parser.add_argument("--status", action="store_true", help="Print manifest status summary and exit (no scraping)")
    parser.add_argument("--flush_every", type=int, default=50,
                        help="Flush HTML buffer to disk every N pages during PDF build (lower = less RAM, default 50)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(out_dir)

    if args.status:
        print_status(manifest)
        return

    if not args.book_id and not args.book_ids and not args.category_ids:
        parser.error("Provide at least one of --book_id, --book_ids, or --category_ids (or --status)")

    session = get_session(args.cf_clearance)

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

    print(f"\n[✓] Run finished. done={done}  error/incomplete={errors}  total={len(queue)}")
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
    print(f"    Run again with the same command at any time to resume incomplete books.")


if __name__ == "__main__":
    main()
