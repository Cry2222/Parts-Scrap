# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository overview

This is a small, single-file Telegram bot: `ecommerce_crawler.py`. It receives Volvo Penta part numbers via Telegram messages, scrapes `https://www.volvopenta.com/shop/0/parts/<part_number>` with Playwright (headless Chromium), extracts part details via regex/DOM parsing, and replies with a summary plus a downloadable results file.

There is no build system, package manifest, test suite, or CI configuration in this repo — it is one script with no other supporting modules.

## Running the bot

The script depends on `python-telegram-bot` and `playwright`, neither of which is vendored or pinned in a requirements file. To run it locally:

```bash
pip install python-telegram-bot playwright
playwright install chromium
python ecommerce_crawler.py
```

Before running, set a real bot token — `BOT_TOKEN` is hardcoded as a placeholder at the top of `ecommerce_crawler.py` (`"YOUR_BOT_TOKEN_HERE"`). Do not commit a real token; prefer swapping this to read from an environment variable if you touch this code.

There are no test, lint, or build commands configured for this project.

## Code structure (single file: `ecommerce_crawler.py`)

The script is organized around one async scrape pipeline plus a thin Telegram handler layer:

- `scrape_part(part_number)` — launches a headless Chromium instance via Playwright (sandbox flags disabled, mobile Chrome user agent), navigates to the part's URL, waits for network idle + a fixed extra delay for JS rendering, then pulls the full page body text and extracts fields with targeted regexes:
  - name (from the first `<h1>`)
  - price (`$` pattern match)
  - weight (`Weight incl. packaging` pattern)
  - category (matched against a fixed list of known system names)
  - `fits_products` (parsed out of a "Fits products" section, matched against an alphanumeric model-code regex)

  It also writes a debug dump of the first 5000 characters of page text to a hardcoded Android path (`/storage/emulated/0/debug_<part_number>.txt`) — this assumes the bot runs inside Termux/Android, not a generic server. Returns a result dict with a `status` of `"success"` or `"failed"` and an `error` message when applicable.

- `format_result(data)` — renders one result dict as a plain-text block for the summary file.

- `start` / `handle_message` — Telegram handlers. `handle_message` parses one or more part numbers out of a free-text message (regex `[A-Za-z0-9\-]{4,}`, deduplicated), scrapes them sequentially (not concurrently), edits a "Processing i/N" status message as it goes, writes all results to a timestamped file at `/storage/emulated/0/volvo_parts_<timestamp>.txt` (same Android-path assumption as the debug dump), sends a summary, reports the first failure's error inline, and finally uploads the results file back to the chat.

- `main()` — builds the `Application`, registers `/start` and the catch-all text handler, and runs polling.

## Key conventions / gotchas

- All file output paths (`/storage/emulated/0/...`) are hardcoded for an Android/Termux environment. If adapting this to run elsewhere, these paths need to change or be made configurable.
- Scraping is best-effort and regex-based against live page text; there's no structured selector-based extraction, so upstream site markup/copy changes can silently degrade field extraction (the code treats "page loaded but nothing matched" as a possible bot block, not a parsing bug).
- Part numbers are uppercased and stripped before use; multiple part numbers in a single Telegram message are processed one at a time, in order.
