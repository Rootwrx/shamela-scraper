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
import re
import sys
import time
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

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\n\r\t]')


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
    soup = BeautifulSoup(resp.text, "html.parser")

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

    # ── TOC: prefer s-nav (deep nested tree from first content page) ───
    # The betaka-index on the book home page often only lists bare page numbers
    # (e.g. ٩٩٩, ١٠٠٠) for multi-volume works.  The real chapter/section tree
    # lives in the .s-nav sidebar that appears on every content page.
    # Strategy:
    #   1. Try betaka-index first.
    #   2. If it looks like it has real chapter labels (not just bare numbers),
    #      use it as-is.
    #   3. Otherwise, fetch the first content page and extract .s-nav > ul.
    betaka = soup.find("div", class_="betaka-index")
    top_ul = betaka.find("ul", recursive=False) if betaka else None
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
        # Try to get the rich s-nav TOC from the first content page
        first_content_url = None
        # Use volume_start_pages hint if we already have it, else use /1
        if meta.get("volume_start_pages"):
            first_content_url = f"{BASE}/book/{book_id}/{meta['volume_start_pages'][0]}"
        else:
            first_content_url = f"{BASE}/book/{book_id}/1"
        try:
            r2 = session.get(first_content_url, timeout=15)
            r2.raise_for_status()
            soup2 = BeautifulSoup(r2.text, "html.parser")
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
        soup = BeautifulSoup(resp.text, "html.parser")
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

        soup = BeautifulSoup(resp.text, "html.parser")

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
    return BeautifulSoup(html_str, "html.parser").get_text(" ", strip=True)


def parse_page_html(html: str) -> list[dict]:
    """
    Extract page entries from a shamela page HTML blob.
    Each paragraph is {"type": "text"|"hamesh", "lines": [html_fragment, ...]}
    Lines are HTML fragments preserving span.cX color and <br> splits.
    """
    soup = BeautifulSoup(html, "html.parser")
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
    soup = BeautifulSoup(html, "html.parser")
    btn = soup.find("button", {"id": "bu_load_next"})
    if btn and btn.get("data-next-id"):
        return int(btn["data-next-id"])
    return None


# ═══════════════════════════════════════════════════════════════════════════
# RESUMABLE PAGE SCRAPING
# ═══════════════════════════════════════════════════════════════════════════

def load_pages_jsonl(pages_path: Path) -> list[dict]:
    pages = []
    if pages_path.exists():
        for line in pages_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                pages.append(json.loads(line))
    return pages


def scrape_book_pages_resumable(
    session: requests.Session,
    book_id: int,
    book_dir: Path,
    start_page: int,
    limit: int = None,
    delay: float = 1.0,
    force: bool = False,
    progress: bool = True,
) -> dict:
    """
    Crawl pages of a book, writing each page to <book_dir>/pages.jsonl as soon
    as it is parsed, and checkpointing exact resume position to
    <book_dir>/progress.json after every page. Safe to interrupt (Ctrl-C,
    crash, network loss) at any point — re-running resumes from the exact
    next page id instead of restarting the whole book.

    Returns the final progress dict, with status one of:
      "done"        — reached the end of the book (no more next-page id)
      "paused"      — stopped early because --limit was reached
      "error"       — a request failed; progress saved so it can retry
    """
    progress_path = book_dir / "progress.json"
    pages_path = book_dir / "pages.jsonl"

    prog = load_json(progress_path, None)
    if prog and prog.get("status") == "done" and not force:
        if progress:
            print(f"  [skip] book {book_id}: already fully scraped "
                  f"({prog.get('pages_scraped', 0)} pages)")
        return prog

    if not prog or force:
        prog = {
            "book_id": book_id,
            "status": "in_progress",
            "next_page_id": start_page,
            "last_page_id": None,
            "pages_scraped": 0,
        }
        pages_path.write_text("", encoding="utf-8")  # fresh start / forced redo
        atomic_write_json(progress_path, prog)
    elif prog.get("status") == "in_progress" or prog.get("status") == "paused" or prog.get("status") == "error":
        if progress:
            print(f"  [resume] book {book_id}: continuing from page_id="
                  f"{prog.get('next_page_id')} ({prog.get('pages_scraped', 0)} pages so far)")

    current_id = prog.get("next_page_id")
    count = prog.get("pages_scraped", 0)

    with open(pages_path, "a", encoding="utf-8") as pf:
        while current_id is not None:
            if limit is not None and count >= limit:
                prog["status"] = "paused"
                break

            url = f"{BASE}/book/{book_id}/{current_id}"
            try:
                resp = session.get(url, timeout=20)
                if resp.status_code == 403:
                    prog["status"] = "error"
                    prog["error"] = "403 Forbidden — try supplying --cf_clearance cookie"
                    atomic_write_json(progress_path, prog)
                    if progress:
                        print(f"\n  [!] 403 on page {current_id} — try --cf_clearance cookie")
                    break
                resp.raise_for_status()
            except requests.RequestException as e:
                prog["status"] = "error"
                prog["error"] = str(e)
                atomic_write_json(progress_path, prog)
                if progress:
                    print(f"\n  [!] Error fetching page {current_id}: {e}")
                break

            html = resp.text
            page_data = parse_page_html(html)
            for pg in page_data:
                pf.write(json.dumps(pg, ensure_ascii=False) + "\n")
            pf.flush()
            count += len(page_data)

            next_id = get_next_page_id(html)

            prog["last_page_id"] = current_id
            prog["next_page_id"] = next_id
            prog["pages_scraped"] = count
            prog["status"] = "in_progress"
            atomic_write_json(progress_path, prog)

            if progress:
                print(f"\r  pages scraped: {count}  (current id: {current_id})",
                      end="", flush=True)

            current_id = next_id
            if next_id is not None:
                time.sleep(delay)

    if progress:
        print()

    if prog.get("status") not in ("error", "paused"):
        prog["status"] = "done" if prog.get("next_page_id") is None else "paused"
        atomic_write_json(progress_path, prog)

    return prog


# ═══════════════════════════════════════════════════════════════════════════
# PDF GENERATION  (weasyprint — Arabic renders via system fonts, no reshaping)
# ═══════════════════════════════════════════════════════════════════════════

import html as _html


def _e(text: str) -> str:
    """HTML-escape a string."""
    return _html.escape(str(text)) if text else ""



# NOTE: Visible TOC rendering has been removed per prompt_toc.txt requirements.
# The PDF's native outline/bookmark feature (via WeasyPrint bookmark-level CSS)
# is used instead.  The TOC tree is still parsed and stored in meta["toc"] /
# meta["toc_flat"] for downstream use, but no visible TOC page is produced.


def build_html(meta: dict, author_info: dict, pages: list[dict]) -> str:
    """Render book data as a full UTF-8 HTML document with RTL Arabic styling."""
    import datetime

    font_candidates = ["ScheherazadeNew", "Amiri", "AmiriQuran", "Traditional Arabic", "Arial"]
    font_stack = ", ".join(f'"{f}"' for f in font_candidates) + ", serif"

    # ── volume boundary set ────────────────────────────────────────────
    vol_starts = set(meta.get("volume_start_pages", []))

    # ── TOC BOOKMARK INJECTION ─────────────────────────────────────────
    # Build page_id -> [(label, toc_level), ...] lookup from parsed TOC.
    # Each TOC entry becomes a PDF bookmark at the page where it starts.
    toc_flat = meta.get("toc_flat", [])
    toc_by_page = {}
    max_toc_level = -1
    for entry in toc_flat:
        pid = entry.get("page_id")
        if pid is not None:
            toc_by_page.setdefault(pid, []).append(
                (entry.get("label", ""), entry.get("level", 0))
            )
            if entry.get("level", 0) > max_toc_level:
                max_toc_level = entry["level"]

    # Generate CSS classes: .toc-bm-0, .toc-bm-1, ...
    # TOC level 0 -> PDF bookmark-level 2 (nested under h2 "نص الكتاب" at level 1)
    toc_bm_css_parts = []
    for lvl in range(max_toc_level + 1):
        bm = lvl + 2  # offset: h2 is bookmark-level 1
        toc_bm_css_parts.append(
            f".toc-bm-{lvl} {{ bookmark-level: {bm}; bookmark-label: content(); "
            f"prince-bookmark-level: {bm}; prince-bookmark-label: content(); }}"
        )
    toc_bm_css = "\n    ".join(toc_bm_css_parts)

    # Re-render CSS with TOC bookmark classes injected
    css = f"""
    /* Front-matter pages (cover, author bio, TOC): roman numerals, no header */
    @page front-matter {{
        size: A4;
        margin: 2.5cm 2.5cm 3cm 2.5cm;
        @bottom-center {{
            content: counter(page, lower-roman);
            font-family: {font_stack};
            font-size: 9pt;
            color: #ccc;
        }}
        @top-left {{ content: none; }}
        @top-right {{ content: none; }}
    }}

    /* Book-text pages: arabic numerals starting from 1, with header */
    @page book-text {{
        size: A4;
        margin: 2.5cm 2.5cm 3cm 2.5cm;
        @bottom-center {{
            content: counter(page);
            font-family: {font_stack};
            font-size: 9pt;
            color: #aaa;
        }}
        @top-left {{
            content: string(chapter-title);
            font-family: {font_stack};
            font-size: 8pt;
            color: #aaa;
        }}
        @top-right {{
            content: "{_e(meta.get('title',''))}";
            font-family: {font_stack};
            font-size: 8pt;
            color: #aaa;
        }}
    }}

    /* Cover page: no numbering at all */
    @page {{
        size: A4;
        margin: 2.5cm 2.5cm 3cm 2.5cm;
    }}
    @page :first {{ @top-left {{ content: none }} @top-right {{ content: none }} @bottom-center {{ content: none }} }}

    * {{ box-sizing: border-box; }}
    body {{
        font-family: {font_stack};
        font-size: 13pt;
        line-height: 2.2;
        direction: rtl;
        text-align: right;
        color: #1a1a1a;
        background: white;
        text-justify: kashida;
    }}

    /* ── COVER ── */
    .cover {{
        page-break-after: always;
        text-align: center;
        padding: 4cm 1.5cm 2cm;
        border: 6px double #8b6914;
        margin: 0.5cm;
    }}
    .cover-ornament {{
        font-size: 20pt;
        color: #8b6914;
        margin-bottom: 0.5em;
        letter-spacing: 0.3em;
    }}
    .cover h1 {{
        font-size: 20pt;
        color: #1a1a2e;
        margin: 0.6em 0;
        line-height: 1.8;
        font-weight: bold;
    }}

    .cover .meta-table {{
        margin: 1.5em auto;
        border-collapse: collapse;
        width: 80%;
        font-size: 11pt;
        color: #333;
    }}
    .cover .meta-table td {{
        padding: 0.4em 0.8em;
        text-align: right;
        border-bottom: 1px dotted #ddd;
        vertical-align: top;
    }}
    .cover .meta-label {{
        color: #8b6914;
        font-weight: bold;
        white-space: nowrap;
        text-align: right;
        width: 25%;
    }}
    .cover .source {{
        font-size: 8pt;
        color: #bbb;
        margin-top: 2em;
    }}

    /* ── SECTION / CHAPTER PAGES ── */
    .section {{ page-break-before: always; }}
    .section-front {{
        page: front-matter;
        page-break-before: always;
    }}
    .section-text {{
        page: book-text;
        page-break-before: always;
        counter-reset: page 1;
    }}
    .volume-divider {{
        page-break-before: always;
        page-break-after: always;
        text-align: center;
        padding-top: 8cm;
        background: #fafaf8;
    }}
    .volume-divider h2 {{
        font-size: 24pt;
        color: #8b6914;
        border: none;
    }}

    /* ── HEADINGS + PDF NATIVE BOOKMARKS ──
       WeasyPrint uses bookmark-level / bookmark-label to build the PDF outline.
       No visible TOC page is produced; these CSS properties generate the
       navigation sidebar bookmarks that PDF readers display in their outline
       panel.  prince-bookmark-* aliases are included for compatibility.
    */
    h2 {{
        font-size: 15pt;
        color: #1a1a2e;
        border-bottom: 2px solid #8b6914;
        padding-bottom: 0.3em;
        margin-top: 1.8em;
        margin-bottom: 0.6em;
        /* PDF outline bookmark — level 1 */
        bookmark-level: 1;
        bookmark-label: content();
        prince-bookmark-level: 1;
        prince-bookmark-label: content();
        string-set: chapter-title content();
    }}
    h3 {{
        font-size: 13pt;
        color: #2a2a4e;
        border-right: 4px solid #8b6914;
        padding-right: 0.5em;
        margin-top: 1.2em;
        margin-bottom: 0.4em;
    }}
    .chapter-heading {{
        font-size: 13pt;
        font-weight: bold;
        color: #1a1a2e;
        text-align: center;
        margin: 1.2em 0 0.5em;
        border-top: 1px solid #ddd;
        border-bottom: 1px solid #ddd;
        padding: 0.3em 0;
        string-set: chapter-title content();
    }}

    /* ── TOC-BASED BOOKMARKS (dynamically generated) ── */
    .toc-bookmark-anchor {{
        height: 0;
        overflow: hidden;
        line-height: 0;
        font-size: 1pt;
        margin: 0;
        padding: 0;
    }}
    {toc_bm_css}

    /* ── AUTHOR BIO ── */
    .author-bio {{
        font-size: 11pt;
        color: #333;
        line-height: 2.2;
        background: #fafaf8;
        border-right: 4px solid #8b6914;
        padding: 0.8em 1em;
        margin: 0.5em 0;
    }}
    .author-bio p {{ margin: 0.3em 0; }}

    /* No visible TOC is rendered — structure is exposed only via the PDF's
       native outline/bookmark panel (WeasyPrint bookmark-level CSS below).
       TOC-based bookmark classes are injected dynamically above. */

    /* ── BODY TEXT ── */
    .page-entry {{ margin-bottom: 0.8em; }}
    .page-text p {{
        margin: 0.5em 0;
        text-align: justify;
        text-justify: kashida;
        orphans: 3;
        widows: 3;
    }}

    /* ── FOOTNOTES (hamesh) ── */
    .hamesh {{
        border-top: 1px solid #bbb;
        margin-top: 1.5em;
        padding-top: 0.5em;
        font-size: 9.5pt;
        color: #444;
        line-height: 1.9;
    }}
    .hamesh p {{
        margin: 0.1em 0;
        text-align: right;
    }}
    """

    parts = [
        f'<!DOCTYPE html><html lang="ar" dir="rtl"><head>'
        f'<meta charset="UTF-8">'
        f'<style>{css}</style></head><body>'
    ]

    # ── COVER ──────────────────────────────────────────────────────────
    scraped_date = datetime.date.today().strftime("%Y-%m-%d")
    parts.append('<div class="cover">')
    parts.append('<div class="cover-ornament">❦ ❦ ❦</div>')
    parts.append(f'<h1>{_e(meta.get("title", "كتاب"))}</h1>')
    rows = []
    for label, key in [("المؤلف", "author"), ("الناشر", "publisher"),
                        ("الطبعة", "edition"), ("عدد الأجزاء", "volumes")]:
        if meta.get(key):
            rows.append(f'<tr><td class="meta-label">{_e(label)}</td><td>{_e(meta[key])}</td></tr>')
    if rows:
        parts.append(f'<table class="meta-table">{"".join(rows)}</table>')
    parts.append(f'<p class="source">المصدر: {_e(meta.get("url",""))} | تاريخ التحميل: {scraped_date}</p>')
    parts.append('</div>')

    # ── AUTHOR INFO ────────────────────────────────────────────────────
    if author_info and not author_info.get("error"):
        parts.append('<div class="section-front">')
        parts.append('<h2>ترجمة المؤلف</h2>')
        if author_info.get("bio"):
            bio_html = "".join(
                f"<p>{_e(l.strip())}</p>"
                for l in author_info["bio"].splitlines() if l.strip()
            )
            parts.append(f'<div class="author-bio">{bio_html}</div>')
        if author_info.get("url"):
            parts.append(f'<p style="font-size:8pt;color:#aaa">المصدر: {_e(author_info["url"])}</p>')
        parts.append('</div>')

    # ── BOOK PAGES — counter resets to 1 here ─────────────────────────
    parts.append('<div class="section-text">')
    parts.append('<h2>نص الكتاب</h2>')

    vol_starts_list = sorted(meta.get("volume_start_pages", []))
    vol_num = 1
    vol_boundaries = set(vol_starts_list)

    for i, page in enumerate(pages):
        pid  = page.get("page_id")

        # volume divider
        if pid and pid in vol_boundaries and i > 0:
            parts.append('</div>')  # close prev section
            parts.append(f'<div class="volume-divider"><h2>الجزء {_e(str(vol_num + 1))}</h2></div>')
            parts.append('<div class="section">')
            vol_num += 1

        # ── Inject TOC bookmark anchors for this page ──────────────────
        if pid and pid in toc_by_page:
            for label, toc_lvl in toc_by_page[pid]:
                if label:
                    parts.append(
                        f'<div class="toc-bookmark-anchor toc-bm-{toc_lvl}">'
                        f'{_e(label)}</div>'
                    )

        parts.append('<div class="page-entry">')
        parts.append('<div class="page-text">')

        paras = page.get("paragraphs")
        if paras:
            for para in paras:
                ptype = para["type"]
                if ptype == "hamesh":
                    parts.append('<div class="hamesh">')
                    for line in para["lines"]:
                        if line.strip():
                            parts.append(f'<p>{line.strip()}</p>')
                    parts.append('</div>')
                elif ptype == "heading":
                    for line in para["lines"]:
                        if line.strip():
                            parts.append(f'<p class="chapter-heading">{line.strip()}</p>')
                else:
                    for line in para["lines"]:
                        if line.strip():
                            parts.append(f'<p>{line.strip()}</p>')
        else:
            for chunk in page["text"].split("\n\n"):
                if chunk.strip():
                    parts.append(f'<p>{_e(chunk.strip())}</p>')

        parts.append('</div></div>')

    parts.append('</div>')

    # NOTE: No visible TOC section is rendered here (per prompt_toc.txt).
    # The PDF's native outline/bookmarks come from:
    #   - h2 elements (bookmark-level 1): cover, author bio, book text
    #   - .toc-bookmark-anchor elements (bookmark-level 2+): injected from
    #     parsed TOC data at the correct page positions, preserving hierarchy.
    # PDF readers expose this in their bookmarks / outline sidebar panel.

    parts.append('</body></html>')
    return "\n".join(parts)


def build_pdf(meta: dict, author_info: dict, pages: list[dict], out_path: str):
    if WeasyprintHTML is None:
        print("  [!] weasyprint not installed — skipping PDF, JSON/HTML still saved.")
        html_str = build_html(meta, author_info, pages)
        Path(out_path).with_suffix(".html").write_text(html_str, encoding="utf-8")
        return
    html_str = build_html(meta, author_info, pages)
    # optionally save HTML alongside for debugging
    html_path = Path(out_path).with_suffix(".html")
    html_path.write_text(html_str, encoding="utf-8")
    WeasyprintHTML(string=html_str).write_pdf(out_path)
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
            start_page=first_page, limit=args.limit, delay=args.delay, force=args.force,
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

    # ── assemble combined JSON ───────────────────────────────────────────
    pages = load_pages_jsonl(book_dir / "pages.jsonl")
    data = {"meta": meta, "author_info": author_info, "pages": pages}
    json_path = book_dir / f"book_{book_id}.json"
    atomic_write_json(json_path, data)
    print(f"  [json] saved → {json_path}  ({len(pages)} pages)")

    # ── PDF ───────────────────────────────────────────────────────────────
    if not args.json_only:
        pdf_path = book_dir / f"book_{book_id}.pdf"
        print(f"[*] Building PDF for book {book_id} ...")
        build_pdf(meta, author_info, pages, str(pdf_path))

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
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between requests (seconds)")
    parser.add_argument("--start_page", type=int, default=1, help="First page ID to scrape (per book, if not resuming)")
    parser.add_argument("--limit", type=int, default=None, help="Max pages to scrape PER BOOK")
    parser.add_argument("--cf_clearance", default=None, help="Cloudflare cf_clearance cookie value")
    parser.add_argument("--json_only", action="store_true", help="Only scrape + save JSON, skip PDF")
    parser.add_argument("--pdf_only", action="store_true", help="Rebuild PDF from already-scraped data, skip network page-scraping")
    parser.add_argument("--force", action="store_true", help="Re-scrape books even if already marked done")
    parser.add_argument("--refresh_categories", action="store_true", help="Re-fetch category book listings instead of using cached manifest copy")
    parser.add_argument("--max_books", type=int, default=None, help="Cap total number of books processed this run (testing)")
    parser.add_argument("--status", action="store_true", help="Print manifest status summary and exit (no scraping)")
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
            print(f"[!] Unhandled error on book {book_id}: {e}")
            entry = manifest.book(book_id)
            entry["status"] = "error"
            entry["error"] = str(e)
            manifest.save()
        status = manifest.book(book_id).get("status")
        if status == "done":
            done += 1
        elif status == "error":
            errors += 1
        if i < len(queue):
            time.sleep(args.delay)

    print(f"\n[✓] Run finished. done={done}  error/incomplete={errors}  total={len(queue)}")
    print(f"    Manifest: {manifest.path}")
    print(f"    Run again with the same command at any time to resume incomplete books.")


if __name__ == "__main__":
    main()
