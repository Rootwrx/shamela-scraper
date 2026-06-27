# shamela_scraper

Scrape books from [shamela.ws](https://shamela.ws) — the largest free Islamic library — and export them to structured JSON + beautiful PDFs.

## Features

- **Resumable downloads** — exact page-level checkpointing; re-run the same command to pick up where you left off
- **Parallel fetching** — multi-threaded page download; default 16 workers
- **Category or book-level input** — download entire sections via `--category_ids` or specific books via `--book_ids`
- **Deeply-nested TOC parsing** — handles multi-level table-of-contents, including lazy-loaded [+] expand nodes
- **Beautiful PDF output** — ornate cover page, Islamic gold palette, A4 format, WeasyPrint-powered
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

## Usage

> Always work on the `dev` branch (see `AGENTS.md`).

### Scrape a single book

```bash
python scraper.py --book_id 12762
```

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

### Rebuild PDFs from cached data (no network)

```bash
python scraper.py --book_id 12762 --pdf_only
```

### Force re-download of already-finished books

```bash
python scraper.py --book_id 12762 --force
```

### Check download status

```bash
python scraper.py --out_dir ./shamela_output --status
```

### Scrape JSON only (skip PDF)

```bash
python scraper.py --book_id 12762 --json_only
```

### All options

| Flag | Default | Description |
|------|---------|-------------|
| `--book_id` | — | Single Shamela book ID (legacy) |
| `--book_ids` | — | Comma-separated list of book IDs |
| `--category_ids` | — | Comma-separated list of category IDs |
| `--out_dir` | `./shamela_output` | Root output directory |
| `--delay` | `0.25` | Base courtesy delay in seconds |
| `--workers` | `16` | Concurrent page-fetch threads |
| `--start_page` | `1` | First page ID to scrape (per book) |
| `--limit` | — | Max pages to scrape per book |
| `--cf_clearance` | — | Cloudflare `cf_clearance` cookie value |
| `--json_only` | `False` | Only save JSON, skip PDF |
| `--pdf_only` | `False` | Rebuild PDF from cached data only |
| `--force` | `False` | Re-scrape even if marked done |
| `--refresh_categories` | `False` | Re-fetch category listings |
| `--max_books` | — | Cap total books processed this run |
| `--status` | `False` | Print manifest status and exit |
| `--flush_every` | `50` | HTML buffer flush interval for PDF |

## Output structure

```
<out_dir>/
├── manifest.json                          # Global cross-run tracking
├── <category_id>_<category_name>/
│   └── <author_id>_<author_name>/
│       └── <book_id>_<title>/
│           ├── book_<id>.json             # Full combined JSON (meta + pages)
│           ├── book_<id>.pdf              # PDF (requires WeasyPrint)
│           ├── meta.json                  # Book metadata
│           ├── author_info.json           # Author biography
│           ├── pages.jsonl                # Raw scraped pages (append-only)
│           ├── pages_resolved.jsonl       # Pages with resolved TOC headings
│           └── progress.json              # Resume checkpoint
```

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
- Arabic fonts (Scheherazade New, Noto Naskh Arabic, Amiri) are recommended for best PDF rendering
