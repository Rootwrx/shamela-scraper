# Project History

## v0.11.0 — Arabic normalization, bracket-line exclusion, heading colors 0–9

- **Arabic letter normalization in `_normalize_ar()`** — آ/أ/إ → ا, ى → ي, ة → ه, ؤ → و, ئ → ي. Fixes TOC vs body mismatches from common spelling variants.
- **Arabic digit-prefix stripping in `_normalize_ar()`** — leading `١-`, `١.` etc. stripped so `١- رأى أرسطوطاليس` matches label `رأي أرسطوطاليس`.
- **Bracket lines excluded from `flat_lines` matching** — grey `[heading]` meta-lines (auto-detected bracket headings) are no longer in `flat_lines` inside `_locate_toc_headings_in_page`. Prevents false-positive matches that placed child headings at bracket positions before the parent's body text.
- **All heading levels rendered visibly** — removed `_should_hide_heading` and `_bookmark_only_html` logic. Every TOC heading gets a visible colored `<p>` in the output.
- **CSS colors for levels 0–9** — 0 gold, 1 rust, 2 teal, 3 sepia, 4 green, 5 purple, 6 brown, 7 dark grey, 8 steel blue (`#2e6b8a`), 9 grey (`#555`). Implicit headings use their level color (not a special "implicit" class).
- **`_strip_body_color()`** — removes Shamela teal `<span>` from body lines at heading positions.
- **Body prefix stripping** — `_strip_heading_prefix()` walks normalized chars to remove the heading label from body text when `keep_line=True`.
- **Duplicate page dedup** — `_ensure_resolved()` tracks seen `url_page_id` to prevent duplicate content.
- **Bidirectional `keep_line` post-check** — runs unconditionally (both upgrades and downgrades).
- **`_prefix_headings_by_juz` capping** — single-pdf numbering capped at 3 parts (x.y.z); subtitle-level headings (level ≥ 2 with juz prefix) get no number.
- **Empty number guard** — `_heading_html()` checks for empty `h["number"]` to avoid bare period prefix.
- **Removed fuzzy fallback** — dropped `_label_words()` and broad substring match from `_find_label_in_lines()`.

### Problems fixed

1. **Child heading appearing before parent body text (book 11812)** — TOC heading `رأي أرسطوطاليس بن نيقوماخوس` matched bracket line `[رأي أرسطوطاليس...]` (pi=1, before parent body) instead of the actual body heading `١- رأى أرسطوطاليس...` (pi=5). Fix: bracket lines excluded from `flat_lines` so TOC headings must match real body content.
2. **Heading not matching due to Arabic letter variants** — TOC label `رأي` vs body text `رأى` (different ي/ى). Fix: normalize ى→ي, آ/أ/إ→ا, ة→ه, ؤ→و, ئ→ي in `_normalize_ar()`.
3. **Heading not matching due to body digit prefix** — TOC label `رأي أرسطوطاليس` vs body `١- رأى أرسطوطاليس:`. Fix: strip leading `[\u0660-\u06690-9]+[\s\-\.\)\]\}]*` in `_normalize_ar()`.
4. **Hidden headings at levels 4+** — `_should_hide_heading` suppressed headings beyond level 3. Fix: removed the function entirely.
5. **Bookmarks-only headings** — `_bookmark_only_html` rendered headings as invisible PDF bookmarks. Fix: all headings now render as visible `<p>` with per-level color.
6. **Implicit headings in rust color** — unmatched headings got CSS class `"implicit"` (rust/level 1 color). Fix: implicit headings use their actual level color.
7. **Duplicate pages in output** — same page content appeared multiple times when `pages.jsonl` had duplicates. Fix: `_ensure_resolved()` dedups by `url_page_id`.
8. **Fuzzy label matching causing false positives** — `_label_words()` and broad substring match in `_find_label_in_lines()` matched partial overlaps. Fix: removed fuzzy fallback entirely.

## v0.10.1 — Fix flat numbering when a parent heading appears after its child

- **Minimum non-auto level used as volume base** — the base level is now computed as the *minimum* level across all non-auto headings in the volume, not the first one's. Fixes flat (`1, 2, 3, 4, 5...`) instead of hierarchical (`2, 2.1, 2.2, 3...`) numbering when a chapter-level heading (level 0) appears after a sub-section heading (level 1) in the same volume.

## v0.10.0 — Auto-heading exclusion, bracket dedup, intro-volume merging

- **Auto headings excluded from numbering** — bracket-extracted headings without TOC match (`auto=True`) no longer consume counter slots or skew the volume's heading base level. Fixes flat numbering when a deep auto heading appeared before real content headings.
- **Bracket heading suppression** — when a bracket-only heading duplicates a located TOC heading's text, the bracket line is suppressed so it doesn't appear as a bare unnumbered paragraph alongside the numbered heading.
- **Intro-volume merging** — volumes with non-digit labels (e.g. `م 1`) are automatically merged with the following content volume (e.g. `1`) so each logical juz becomes a single file. Per-juz PDFs drop from 4 files to 2 for intro+content books.
- **Combined PDF clean prefixes** — volume prefixes now skip intro volumes (`م 1`, `م 2`) and start from `1` for the first content volume, `2` for the second, etc.
- **`_merge_intro_volumes`** — new function that collapses consecutive non-digit + digit volume label pairs.
- **Null-volume guard** — books without volumes no longer crash on `len(None)`.

### Problems fixed

1. **Auto headings consumed counter slot `1`** — when a volume started with an auto heading at a deep level (e.g. level 3), it took position 1 and pushed real headings to start at 2.
2. **Auto headings skewed volume base** — the minimum heading level could be set by an auto heading at depth 3, forcing all real headings to shift by +3 levels.
3. **Bracket headings duplicated numbered headings** — bracket-only headings matching a TOC heading appeared as bare unnumbered paragraphs.
4. **Intro volumes created redundant PDFs and wrong prefixes** — `م 1` + `1` produced 2 files and gave ج 1 prefix `2` in the combined PDF. Now merged into one file with prefix `1`.
5. **Books without volumes crashed** — `_merge_intro_volumes` called on `None` vol_pages.

## v0.9.0 — Full per-volume heading renumbering & per-juz prefix removal

- **Full renumbering per volume** — headings are now renumbered sequentially at every level within each volume, replacing the old offset-based logic that only adjusted the top-level component. Fixes sub-level numbering carrying over across volume boundaries (e.g. `2.1.26` → `2.1.1`).
- **Zero-counter backfill** — when the first heading in a volume is deeper than level 0 (e.g. level 2 right after `نص الكتاب`), implicit parent-level counters are backfilled to 1 instead of staying at 0.
- **Per-juz PDFs omit juz prefix** — standalone per-juz PDFs now number headings starting from `1` since the volume is already identified by filename/cover. Combined PDFs (`--single_pdf`) retain the `2.` prefix.
- **`_prefix_headings_by_juz` signature** — new `include_juz_prefix` parameter (default `True`).

### Problems fixed

1. **Sub-level heading numbering continued across volume boundaries** — juz 2 headings showed `2.1.26` instead of `2.1.1` because offset only applied to `parts[0]`. Deeper levels unchanged.
2. **Zero counters** — `2.0.1` appeared when the first heading in a volume was at a deeper level with no lower-level headings preceding it.
3. **Redundant juz prefix in per-juz PDFs** — standalone juz PDFs included `{juz}.` prefix even though the file is for a single volume.

## v0.8.0 — Bigger page numbers, shamela attribution

- Page numbers increased to **14pt bold** in both top-left and bottom-center
- Added `مُستَخرَج من المكتبة الشاملة` in red at bottom of cover page

## v0.7.0 — Per-juz heading reset & inline volume dividers

- **Inline volume dividers** — juz headings (الجزء X) now appear as red headings at the top of the content page, not on a separate splash page. No blank pages between heading and content.
- **Juz 1 flows naturally** — first juz's content flows directly after the `نص الكتاب` section heading with no page break.
- **Per-juz heading reset** — each standalone per-juz PDF resets heading numbering per juz (e.g. juz 2 PDF starts at `1.1` instead of continuing from juz 1's numbering).
- **Combined PDF (`--single_pdf`)** — heading numbers are prefixed and reset per juz (e.g. `2.1`, `2.2` for juz 2). Use `--no_juz_outline` to suppress the prefix.
- **Fixed top margin** — removed excess gap above `نص الكتاب` heading (was ~2.7cm from `@page` margin + h2 `margin-top`, now flush).
- **Volume dropdown dedup** — duplicate volume entries from top+bottom dropdowns handled at both extraction and build time.
- **Volume divider CSS** — changed from full-page splash (`page-break-before: always` + `page-break-after: always` + centered 30pt gold text) to inline red heading (18pt, `#c0392b`, no page break).
- **First section override** — juz 1's `<div class="section">` uses `style="page-break-before:auto"` to avoid blank page after `نص الكتاب`.

### Problems fixed

1. **Blank page between juz heading and content** — both `.volume-divider` and `.section` had `page-break-before: always`, creating an empty page. Fix: moved divider inside `.section` and removed page break from divider CSS.
2. **Large gap above `نص الكتاب`** — `@page book-text` margin 1.6cm + h2 `margin-top: 1.6em` ≈ 2.7cm total. Fix: `.section-text > h2:first-child { margin-top: 0; }`.
3. **Duplicate volume entries** — top and bottom dropdowns both contained the same volume links, duplicating pages and labels. Fix: dedup with `seen` set at both extraction and build time.
4. **Stale `meta.json` duplicates** — even after extraction fix, previously saved `meta.json` could still have duplicates. Fix: dedup at build time too.
5. **Heading numbering not reset per juz** — in per-juz PDFs, headings continued global numbering (e.g. juz 2 started at heading 51). Fix: applied `_prefix_headings_by_juz` to per-juz iterator.

## v0.6.0 — Per-juz PDF splitting (previous release)

- Books with ajza'/volumes automatically split into per-juz PDFs (`book_{id}_{label}.pdf`)
- Each per-juz PDF has its own cover showing `الجزء {label}`
- Volume labels extracted from dropdown on page 1 full HTML
- `--single_pdf` flag for combined PDF when volumes exist
- `--no_juz_outline` flag for old-style headings without juz prefix
- `--force` flag to clear `pages.jsonl` and re-scrape from scratch
- Juz 1 label now inserted before first content (not skipped)
- Heading reset per juz via `_prefix_headings_by_juz` (offset subtraction)

### Problems fixed

1. **Volume dropdown not on home page** — some books have the ajza' dropdown only in page 1's full HTML, not the home page. Fix: fall back to fetching page 1 HTML.
2. **Duplicate volume entries from top+bottom dropdowns** — both nav bars contain the same links. Fix: dedup with `seen` set.
3. **Juz 1 label missing in combined PDF** — the first volume divider was skipped because the loop started at boundary detection (which only fires on page transition, not before first page). Fix: insert juz 1 label explicitly before the loop.

## v0.5.0 — Sliding window chain-walk

- Replaced batch-based chain-walk with `_chain_walk` sliding-window pattern
- Eliminated batch synchronization idle time
- `_find_last_page_id` discovers last page via `>>` button (single request vs hundreds)

## v0.4.0 — Cover & border fixes

- Cover content vertically centered via flexbox
- Corner ornaments added
- Outer double border removed (inner gold border sufficient)
- `@page :first` sizing issue worked around

## v0.3.0 — Renumber headings by document order

- `_renumber_headings_by_document_order` fixes heading numbers when TOC hierarchy is inconsistent with content order
- Handles `bracket_matched` parent headings on different pages from their children

## v0.2.0 — Initial features

- Resumable downloads with `pages.jsonl` as source of truth
- Parallel page fetching (16 workers default)
- Category and book-level input
- Deeply-nested TOC parsing with lazy-loaded [+] expand nodes
- Beautiful PDF output with ornate cover page, Islamic gold palette, A4 format
- Structured output tree (category → author → book)
- Global manifest tracking
- Cloudflare clearance support

## Architecture Notes

### Page fetching
- Primary: Ajax endpoint `/ajax/pageContent/{book_id}/{page_id}`
- Fallback: Full HTML `/book/{book_id}/{page_id}` (on non-403 errors)
- Last page discovery: `>>` button in full HTML pagination nav

### Volume detection
- Volume dropdown selector: `ul.dropdown-menu a[href*='/book/']`
- Extracted from home page, falls back to page 1 full HTML
- Stored in `meta.json` as `volume_start_pages` and `volume_labels`

### Heading resolution
- Independent from page fetching
- TOC parsed from book's home page (fully-expanded tree)
- `materialize_resolved_pages` + `_renumber_headings_by_document_order`
- `_prefix_headings_by_juz` resets per-volume numbering in combined PDF

### PDF generation
- WeasyPrint-based, A4 format
- Cream background (#fdfaf4), gold accents (#c9a84c), teal headings (#00695c)
- Scheherazade New / Noto Naskh Arabic / Amiri fonts for Arabic text
