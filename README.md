# shamela_scraper

Scrape books from [shamela.ws](https://shamela.ws) — the largest free Islamic library — and export them to structured JSON + beautiful PDFs and/or DOCX documents.

## Features

- **Resumable downloads** — exact page-level checkpointing; re-run the same command to pick up where you left off
- **Parallel fetching** — multi-threaded page download; configurable workers (default 4)
- **Category or book-level input** — download entire sections via `--category_ids` or specific books via `--book_ids`
- **Deeply-nested TOC parsing** — handles multi-level table-of-contents, including lazy-loaded [+] expand nodes that are resolved via AJAX
- **Beautiful PDF output** — ornate cover page, Islamic gold palette, A4 format, WeasyPrint-powered
- **DOCX output** — native Word `.docx` with outline navigation, per-level heading colors, hamesh footnotes, RTL page layout
- **Per-juz PDFs/DOCX** — books with ajza'/volumes are automatically split into separate files (`book_{id}_{label}.pdf` / `.docx`), each with its own cover showing `الجزء {label}`
- **Single combined output** — use `--single` for one combined file when volumes exist; headings are prefixed with the juz number (e.g. `2.1`, `2.2` for juz 2)
- **Multi-column layout** — `--columns 2` for two-column newspaper-style PDF text layout
- **Fast mode** — `--fast` drops kashida justification for significantly faster PDF rendering
- **Structured output tree** — organized by category → author → book
- **Global manifest** — tracks every book/author/category ever processed
- **Cloudflare clearance** — pass `--cf_clearance` or use the automatic cookie-refresh helper

## Requirements

- Python 3.10+
- [WeasyPrint](https://doc.courtbouillon.org/weasyprint/stable/) (for PDF generation) — requires system-level dependencies (libpango, libcairo, etc.)
- [python-docx](https://python-docx.readthedocs.io/) (for DOCX generation; optional)

## Installation

```bash
# Clone the repo
git clone <repo-url>
cd shamela

# Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt
```

### System dependencies for WeasyPrint

On **Debian/Ubuntu**:

```bash
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
  libgdk-pixbuf2.0-dev libffi-dev shared-mime-info libcairo2
```

On **Arch Linux**:

```bash
sudo pacman -S pango cairo gdk-pixbuf2 libffi shared-mime-info
```

On **Fedora**:

```bash
sudo dnf install pango cairo gdk-pixbuf2 libffi shared-mime-info
```

On **macOS** (Homebrew):

```bash
brew install pango cairo gdk-pixbuf libffi
```

> **Note:** PDF generation is optional. Without WeasyPrint, the script scrapes data and saves JSON but skips PDF output. Install it only if you need PDFs.
>
> DOCX generation requires `python-docx` (included in `requirements.txt`). If not installed, the `--docx` flag is silently ignored.

## Quick Start

```bash
# Scrape one book (downloads pages + generates PDF)
python scraper.py --book_id 12762

# Or generate a DOCX instead
python scraper.py --book_id 12762 --docx

# Output will be at ./shamela_output/<category>/<author>/<book_id>_<title>/
```

## Usage

> Always work on the `dev` branch (see `AGENTS.md`).

### Scrape a single book

```bash
python scraper.py --book_id 12762
```

Books with ajza' (volumes) automatically produce per-juz PDFs:

```
book_30186.pdf              ← not created (volumes exist)
book_30186_1.pdf            ← juz 1
book_30186_2.pdf            ← juz 2
book_30186_3.pdf            ← juz 3
...
```

Each per-juz file has its own cover annotated with `الجزء {label}` and its own heading numbering that resets per juz.

Books **without** volumes produce a single file.

### Scrape several specific books

```bash
python scraper.py --book_ids 12762,667,151109
```

### Scrape everything in one or more categories

```bash
python scraper.py --category_ids 13,33,40
```

### Mix categories and specific books

```bash
python scraper.py --category_ids 13 --book_ids 667 --out_dir ./library
```

### Generate single combined output (even when ajza' exist)

```bash
python scraper.py --book_id 30186 --single
```

Produces one file (`book_30186.pdf` and/or `book_30186.docx`) with:
- Red inline headings (`الجزء 2`) as volume dividers within the text
- Heading numbering prefixed and reset per juz (e.g. `2.1`, `2.2` for juz 2)
- Juz 1 label shown before first content, no splash page

To suppress the juz prefix in headings (old plain style):

```bash
python scraper.py --book_id 30186 --single --no_juz_outline
```

### Build DOCX output

Generate a `.docx` alongside the PDF:

```bash
python scraper.py --book_id 12762 --docx
```

Build DOCX only (skip PDF):

```bash
python scraper.py --book_id 12762 --docx --no-pdf
```

Build both formats:

```bash
python scraper.py --book_id 12762 --docx --pdf
```

Single combined DOCX for multi-volume books (uses `--single` which applies to whichever format is being built):

```bash
python scraper.py --book_id 12762 --docx --single
```

### Two-column PDF layout

```bash
python scraper.py --book_id 12762 --columns 2
```

### Fast PDF mode (no kashida justification)

```bash
python scraper.py --book_id 12762 --fast
```

### Specific volume only

```bash
python scraper.py --book_id 30186 --juz 3
```

Outputs only juz 3 for whichever format(s) are being built.

### Rebuild output from cached data (no network)

```bash
python scraper.py --book_id 12762 --pdf_only
python scraper.py --book_id 12762 --docx --pdf_only    # rebuild DOCX only
```

### Force re-download of already-finished books

```bash
python scraper.py --book_id 12762 --force
```

Clears `pages.jsonl` and re-scrapes from scratch.

### Check download status

```bash
python scraper.py --out_dir ./shamela_output --status
```

### Scrape JSON only (skip PDF/DOCX)

```bash
python scraper.py --book_id 12762 --json_only
```

### Save combined book JSON (meta + TOC + all pages with headings)

```bash
python scraper.py --book_id 12762 --json
```

The combined file `book_<id>.json` contains:

```
{
  "meta":           { ... },           # book metadata, volume info, toc_summary
  "toc_flat":       [ ... ],           # flat TOC entries with level, label, page_id
  "toc":            [ ... ],           # nested TOC tree (for hierarchy)
  "pages":          [ ... ],           # every page with resolved_headings
  "author_info":    { ... }            # author biography (if available)
}
```

Each page's `resolved_headings` array includes the heading number, label, level, page position, and whether it was bracket-matched or implicit — everything needed for post-processing.

### Control concurrency and politeness

```bash
python scraper.py --book_ids 12762,667 --workers 16 --delay 0.3
```

### All options

| Flag | Default | Description |
|------|---------|-------------|
| `--book_id` | — | Single Shamela book ID |
| `--book_ids` | — | Comma-separated list of book IDs |
| `--category_ids` | — | Comma-separated list of category IDs |
| `--out_dir` | `./shamela_output` | Root output directory |
| `--delay` | `1.0` | Base courtesy delay in seconds between requests |
| `--workers` | `4` | Concurrent page-fetch threads |
| `--start_page` | `1` | First page ID to scrape (per book) |
| `--limit` | — | Max pages to scrape per book |
| `--cf_clearance` | — | Cloudflare `cf_clearance` cookie value |
| `--user-agent` / `--ua` | — | Browser User-Agent matching the `cf_clearance` source |
| `--json` | `False` | Save a combined `book_<id>.json` (meta + TOC + all pages with resolved headings) alongside other outputs |
| `--pdf_only` | `False` | Rebuild output from cached data only (no network) |
| `--pdf` | `True` | Generate PDF output (use `--no-pdf` to disable) |
| `--docx` | `False` | Build DOCX alongside PDF. Combine with `--no-pdf` for DOCX-only |
| `--single` | `False` | Single combined file (PDF and/or DOCX) even when ajza'/volumes exist |
| `--no_juz_outline` | `False` | With `--single`: skip juz numbering in headings |
| `--juz` | — | Output only a specific juz/volume (1-based index) |
| `--force` | `False` | Re-scrape even if marked done; clears `pages.jsonl` |
| `--override` | `False` | Process books even if output already exists on disk. By default, finished books are skipped on re-runs |
| `--refresh_categories` | `False` | Re-fetch category book listings (not cached) |
| `--max_books` | — | Cap total books processed this run (for testing) |
| `--status` | `False` | Print manifest status and exit without scraping |
| `--columns` | `1` | Number of text columns per PDF page (`1` or `2`) |
| `--fast` | `False` | Drop kashida justification for faster PDF rendering |
| `--style_config` | — | Path to `style_config.json` (falls back to project root, then built-in defaults) |
| `--flush_every` | `50` | HTML buffer flush interval for PDF generation |

## Output structure

```
<out_dir>/
├── manifest.json                          # Global cross-run tracking (books, authors, categories)
├── <category_id>_<category_name>/
│   └── <author_id>_<author_name>/
│       └── <book_id>_<title>/
│           ├── book_<id>.json             # Full combined JSON (meta, TOC, all pages with headings); saved only with `--json`
│           ├── book_<id>.pdf              # Single PDF (or combined when --single)
│           ├── book_<id>_<label>.pdf      # Per-juz PDFs, e.g. book_30186_1.pdf
│           ├── book_<id>.docx             # Single DOCX (only with --docx; or combined when --single)
│           ├── book_<id>_<label>.docx     # Per-juz DOCX files (only with --docx)
│           ├── meta.json                  # Book metadata (title, author, volume info, etc.)
│           ├── author_info.json           # Author biography from shamela
│           ├── pages.jsonl                # Raw scraped pages (append-only, line-delimited JSON)
│           ├── pages_resolved.jsonl       # Pages with resolved TOC headings (for PDF generation)
│           └── progress.json              # Resume checkpoint (scanned pages.jsonl is source of truth)
```

**Per-juz vs combined:** When a book has ajza' (volumes) listed in its dropdown, per-juz files are created by default. Use `--single` to get one combined file instead.

**PDF vs DOCX:** PDF is built by default. Pass `--docx` to build DOCX alongside, or `--docx --no-pdf` for DOCX-only. The `--single` flag applies to whichever format is being built.

**JSON assembly:** The full `book_<id>.json` is skipped by default to save RAM on large books (17K-page Tafsir al-Tabari would consume several hundred MB). Pass `--json` to enable it. Individual source files (`pages.jsonl`, `pages_resolved.jsonl`, `meta.json`) are always on disk.

## Resumability

The scraper writes each page to `pages.jsonl` immediately after fetching it. If interrupted (Ctrl+C, crash, timeout), re-running the **exact same command** resumes from where it stopped — no pages are re-downloaded.

Progress is self-healing: on resume, the script scans `pages.jsonl` on disk and uses it as the source of truth, even if the checkpoint file (`progress.json`) was corrupted or lagging behind.

## Implicit headings (missing titles)

Some TOC entries never appear as visible headings in the book text — the author refers to a section by number (e.g. "4.3. دلالة الألفاظ على أنفسها") but skips a printed heading line. These are called **implicit headings**.

### Detection

During heading resolution, implicit entries are logged and saved to a dedicated file:

```
<book_dir>/implicits.json
```

Example contents:

```json
[
  {"page": 15, "number": "4.3", "label": "دلالة الألفاظ على أنفسها"},
  {"page": 21, "number": "4.4", "label": "إطلاق الاسم على الصفة"},
  {"page": 22, "number": "4.5", "label": "إقحام لفظة «اسم» في الكلام"}
]
```

Each entry contains:
- `page` — the Shamela page ID where the heading should appear
- `number` — the TOC heading number (e.g. `4.3`)
- `label` — the heading text

### How to fix

Implicit headings have **no number in the content text** — the numbering (`4.3`, `4.4`, …) is auto-generated from the TOC hierarchy, not from the text itself. When fixing, you supply both the number and the label.

Two approaches:

**1. Edit the DOCX file directly** — Open the `.docx` in Word/WPS/LibreOffice, navigate to the page, type the heading where it belongs (paragraph break or mid-paragraph — your judgment). Re-upload or redistribute the edited file. No re-run needed.

**2. Edit the source HTML + rebuild** — Find the corresponding Shamela page file in the book directory, insert a heading tag like `<h3>4.3. دلالة الألفاظ على أنفسها</h3>` at the correct position, then re-run with `--docx --pdf_only` to regenerate from your edits.

Both methods require human judgment — `implicits.json` only tells you *what* is missing and *on which page*, not *where on that page*. The insertion point could be inside a paragraph, after a specific word, etc., so the script never auto-injects.

## Style configuration

All visual styles — fonts, colors, heading sizes, shapes, and more — come from `style_config.json` in the project root. You can edit this file directly to customize the output without touching the Python code.

Use a custom config path:

```bash
python scraper.py --book_id 12762 --style_config /path/to/my_config.json
```

### config file structure

```json
{
  "fonts": {
    "heading": "Scheherazade New",
    "body": "Scheherazade New"
  },
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
    "brown": "#3e2712"
  },
  "shamela_span_colors": {
    "c1": "#cc3333",
    "c2": "#1a7a1a",
    "c3": "#8a8a20",
    "c4": "#909090",
    "c5": "#1a7aa0"
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
    {"level": 9, "size_pdf": 17, "size_docx": 16, "color": "#c06080", "shape": "▪"}
  ],
  "delimiter_colors": {
    "brace": "#FF0000",
    "verse": "#FF0000"
  }
}
```

| Section | What it controls |
|---------|-----------------|
| `fonts` | Typeface for heading text (`heading`) and body text (`body`). Affects both PDF and DOCX. |
| `body_size_pdf` | Body text size in PDF (pt). |
| `body_size_docx` | Body text size in DOCX (pt). |
| `palette` | Named colors used across cover, headings, borders, etc. Change these to shift the entire color scheme. |
| `shamela_span_colors` | Colors for Shamela's original `<span class="c1">` through `<span class="c5">` used in body text. |
| `heading_levels` | Per-level settings: `size_pdf` / `size_docx` (pt), `color` (hex), and `shape` (Unicode character). |
| `delimiter_colors` | Colors for `{...}` brace text and `﴾...﴿` / `﴿...﴾` verse delimiters in body content. |

Any field you omit falls back to the built-in default. You only need to include the values you want to change.

## Cloudflare bypass

If shamela.ws returns 403 errors, you may need to pass a Cloudflare clearance cookie:

1. Open your browser's developer tools
2. Visit `https://shamela.ws`
3. Copy the `cf_clearance` cookie value
4. Pass it to the scraper:

```bash
python scraper.py --book_id 12762 --cf_clearance "your-cf-clearance-value"
```

The clearance cookie expires after a few hours — you will need to refresh it periodically.

## Notes

- The scraper respects the site with a configurable delay between requests (`--delay`)
- Books that error out are logged in the manifest and can be resumed later
- The TOC is parsed from the book's home page (not the content page) to get the fully-expanded tree with all nested sections
- Heading resolution runs independently from page fetching — `pages_resolved.jsonl` contains TOC headings matched to their content pages, and heading numbering is global across the entire book
- For per-juz files, heading numbers are reset per juz (e.g. juz 1 → `1.1`, `1.2`; juz 2 → `2.1`, `2.2`); same for `--single` unless `--no_juz_outline` is given
- Per-juz files have their own cover page annotated with the juz number (`الجزء {label}`)
- The last page of the book is discovered by parsing the `>>` button in the pagination nav — a single request instead of walking hundreds of tail pages
- Arabic fonts (Scheherazade New, Noto Naskh Arabic, Amiri) are recommended for best PDF rendering. Install them system-wide or WeasyPrint will fall back to a default font.
- Network errors (timeout, 522) during page fetch are skipped and logged; re-running resumes and re-fetches only the missing pages
- `--force` clears `pages.jsonl` and re-scrapes entirely from scratch
- `--fast` disables kashida (Arabic letter-stretch) justification for a major WeasyPrint speed win on large books — plain justify is used instead
- The DOCX output reuses the exact same heading resolution and numbering logic as the PDF (`_render_page_html`), so the two formats never drift apart
- The `books_ids.txt` file in the repo is for testing/debugging problem books — it is NOT a batch-processing file
