# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository overview

This is a small, single-file Telegram bot: `ecommerce_crawler.py`. It receives Volvo Penta part numbers via Telegram messages, scrapes `https://www.volvopenta.com/shop/0/parts/<part_number>`, extracts part details via regex, and replies with a summary plus a downloadable results file.

There is no build system, package manifest, test suite, or CI configuration in this repo — it is one script with no other supporting modules.

## Running the bot

Dependencies are not vendored or pinned in a requirements file. The only required install is:

```bash
pip install python-telegram-bot
python ecommerce_crawler.py
```

`httpx` (used for the browserless scrape path) comes in automatically as a hard dependency of `python-telegram-bot`, so it never needs installing separately.

Playwright is **optional** — only needed if the site stops server-rendering part data and the browser fallback has to kick in (see below). Install it only if results come back `Fetched via : browser`:

```bash
pip install playwright && playwright install chromium
```

Configuration is via environment variables:
- `BOT_TOKEN` — required; falls back to the placeholder `"YOUR_BOT_TOKEN_HERE"` if unset.
- `OUTPUT_DIR` — where result/debug files are written. Defaults to `/storage/emulated/0` when that directory exists (Android/Termux), otherwise the current working directory.

There are no test, lint, or build commands configured for this project.

## Scrape architecture (the important part)

`scrape_part()` is a **two-tier pipeline that picks the cheapest path that works**, decided at runtime rather than hardcoded:

1. `_scrape_http()` — one plain `httpx` GET, no browser. `_html_to_text()` flattens the raw HTML into visible-ish text (drops `<script>`/`<style>`, converts tags to newlines, unescapes entities including `&nbsp;`) so the same regexes can run over it.
2. `_scrape_browser()` — only if tier 1 returns no usable data. Launches headless Chromium via Playwright and reads `body.inner_text()`. **The `playwright` import is deliberately lazy (inside the function)** so the package is not needed at all when tier 1 succeeds.

Both tiers feed the same `_extract_fields()` (price, weight, category, fits-products) so extraction logic stays in one place; only name extraction differs (`<h1>` regex vs. `h1` locator). A tier "succeeds" if it found either a name or a price — that's `_has_data()`.

The chosen tier is recorded in `result["method"]` (`"http"` / `"browser"`) and surfaced in output as `Fetched via :`. This is the signal for whether Playwright is still needed at all. If both tiers fail, `result["error"]` contains both tiers' errors joined by `|`.

`result["fits_products"]` is reset between tiers so a partial tier-1 extraction can't pollute tier-2 results.

## Extraction gotchas

- Scraping is best-effort and regex-based against page text, not structured selectors, so upstream markup/copy changes can silently degrade field extraction. "Page loaded but nothing matched" is treated as a possible bot block, not a parsing bug.
- **Category** picks the *longest* match, not the first. Generic names in the list (`Filter`, `Engine`, `Hose`) also appear inside part names ("Oil Filter"), which sit earlier in the page than the real category — leftmost-match would return the wrong one.
- **Fits products** uses `\b([A-Z]{1,5}\d{1,5}[A-Z0-9\-]*)\b` with a `len >= 4` filter. The `\d{1,5}` (rather than `{2,5}`) is what allows single-digit series like `D4-260`.
- Debug dumps (`debug_<part>_<http|browser>.txt`, first 5000 chars) are best-effort — wrapped so an unwritable `OUTPUT_DIR` can never fail a scrape.

## Telegram layer

`handle_message` parses part numbers out of free text (regex `[A-Za-z0-9\-]{4,}`, deduplicated, uppercased), scrapes them **sequentially, not concurrently**, edits a "Processing i/N" status message as it goes, writes all results to `<OUTPUT_DIR>/volvo_parts_<timestamp>.txt`, sends a summary, reports the first failure's error inline, then uploads the results file. `main()` registers `/start` plus the catch-all text handler and runs polling.
