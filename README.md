# shamela_scraper

Scrape books from [shamela.ws](https://shamela.ws) — the largest free Islamic library — and export them to structured JSON + beautiful PDFs.

## Features

- **Resumable downloads** — exact page-level checkpointing; re-run the same command to pick up where you left off
- **Parallel fetching** — multi-threaded page download; default 16 workers
- **Category or book-level input** — download entire sections via `--category_ids` or specific books via `--book_ids`
- **Deeply-nested TOC parsing** — handles multi-level table-of-contents, including lazy-loaded [+] expand nodes
- **Beautiful PDF output** — ornate cover page, Islamic gold palette, A4 format, WeasyPrint-powered
- **Per-juz PDFs** — books with ajza'/volumes are automatically split into separate PDFs (`book_{id}_{label}.pdf`), each with its own cover showing `الجزء {label}`
- **Single combined PDF** — use `--single_pdf` to generate one combined PDF even when volumes exist; volume dividers use the actual juz labels from the dropdown, and all heading numbers are prefixed with the juz number (e.g. `2.1`, `2.2` for juz 2)
- **Structured output tree** — organized by category → author → book
- **Global manifest** — tracks every book/author/category ever processed
- **Cloudflare clearance** — pass `--cf_clearance` if behind Cloudflare protection

## Requirements

- Python 3.10+
- [WeasyPrint](https://doc.courtbouillon.org/weasyprint/stable/) (for PDF generation) — requires system-level dependencies (libpango, libcairo, etc.)

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

## Quick Start

```bash
# Scrape one book (downloads pages + generates PDF)
python scraper.py --book_id 12762

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

Each per-juz PDF has its own cover annotated with `الجزء {label}` and its own heading numbering that resets per juz (e.g. `1.1`, `1.2` for juz 1, `1.1`, `1.2` for juz 2).

Books **without** volumes produce a single PDF as before.

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

### Generate single combined PDF (even when ajza' exist)

```bash
python scraper.py --book_id 30186 --single_pdf
```

Produces one PDF (`book_30186.pdf`) with:
- Red inline headings (`الجزء 2`) as volume dividers within the text
- Heading numbering prefixed and reset per juz (e.g. `2.1`, `2.2` for juz 2)
- Juz 1 label shown before first content, no splash page

To suppress the juz prefix in headings (old plain style):

```bash
python scraper.py --book_id 30186 --single_pdf --no_juz_outline
```

### Rebuild PDFs from cached data (no network)

```bash
python scraper.py --book_id 12762 --pdf_only
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

### Scrape JSON only (skip PDF)

```bash
python scraper.py --book_id 12762 --json_only
```

### Control concurrency and politeness

```bash
python scraper.py --book_ids 12762,667 --workers 8 --delay 0.5
```

### All options

| Flag | Default | Description |
|------|---------|-------------|
| `--book_id` | — | Single Shamela book ID (legacy) |
| `--book_ids` | — | Comma-separated list of book IDs |
| `--category_ids` | — | Comma-separated list of category IDs |
| `--out_dir` | `./shamela_output` | Root output directory |
| `--delay` | `0.25` | Base courtesy delay in seconds between requests |
| `--workers` | `16` | Concurrent page-fetch threads |
| `--start_page` | `1` | First page ID to scrape (per book) |
| `--limit` | — | Max pages to scrape per book |
| `--cf_clearance` | — | Cloudflare `cf_clearance` cookie value |
| `--json_only` | `False` | Only save JSON, skip PDF generation |
| `--pdf_only` | `False` | Rebuild PDF from cached data only (no network) |
| `--single_pdf` | `False` | Single combined PDF even when ajza' exist; headings include juz prefix with per-juz reset |
| `--no_juz_outline` | `False` | With `--single_pdf`: skip juz numbering in headings (plain old-style headings) |
| `--force` | `False` | Re-scrape even if marked done; clears `pages.jsonl` |
| `--refresh_categories` | `False` | Re-fetch category book listings (not cached) |
| `--max_books` | — | Cap total books processed this run (for testing) |
| `--status` | `False` | Print manifest status and exit without scraping |
| `--flush_every` | `50` | HTML buffer flush interval for PDF generation |

## Output structure

```
<out_dir>/
├── manifest.json                          # Global cross-run tracking (books, authors, categories)
├── <category_id>_<category_name>/
│   └── <author_id>_<author_name>/
│       └── <book_id>_<title>/
│           ├── book_<id>.json             # Full combined JSON (not saved by default — too large)
│           ├── book_<id>.pdf              # Single PDF (or combined PDF when --single_pdf)
│           ├── book_<id>_<label>.pdf      # Per-juz PDFs, e.g. book_30186_1.pdf, book_30186_2.pdf …
│           ├── meta.json                  # Book metadata (title, author, volume info, etc.)
│           ├── author_info.json           # Author biography from shamela
│           ├── pages.jsonl                # Raw scraped pages (append-only, line-delimited JSON)
│           ├── pages_resolved.jsonl       # Pages with resolved TOC headings (for PDF generation)
│           └── progress.json              # Resume checkpoint (scanned pages.jsonl is source of truth)
```

**Per-juz vs combined:** When a book has ajza' (volumes) listed in its dropdown, per-juz PDFs are created by default. Use `--single_pdf` to get one combined PDF instead.

**JSON assembly:** The full `book_<id>.json` is skipped by default to save RAM on large books. Individual source files (`pages.jsonl`, `pages_resolved.jsonl`, `meta.json`) are always on disk.

## Resumability

The scraper writes each page to `pages.jsonl` immediately after fetching it. If interrupted (Ctrl+C, crash, timeout), re-running the **exact same command** resumes from where it stopped — no pages are re-downloaded.

Progress is self-healing: on resume, the script scans `pages.jsonl` on disk and uses it as the source of truth, even if the checkpoint file (`progress.json`) was corrupted or lagging behind.

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
- For per-juz PDFs, heading numbers are reset per juz (e.g. juz 1 → `1.1`, `1.2`; juz 2 → `2.1`, `2.2`); same for `--single_pdf` unless `--no_juz_outline` is given
- Per-juz PDFs have their own cover page annotated with the juz number (`الجزء {label}`)
- The last page of the book is discovered by clicking `>>` in the pagination nav — a single request instead of walking hundreds of tail pages
- Arabic fonts (Scheherazade New, Noto Naskh Arabic, Amiri) are recommended for best PDF rendering. Install them system-wide or WeasyPrint will fall back to a default font.
- Network errors (timeout, 522) during page fetch are skipped and logged; re-running resumes and re-fetches only the missing pages
- `--force` clears `pages.jsonl` and re-scrapes entirely from scratch
- The `books_ids.txt` file in the repo is for testing/debugging problem books — it is NOT a batch-processing file
