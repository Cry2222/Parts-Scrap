# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository overview

Two front-ends over one scraper, for looking up Volvo Penta parts by number:

- **`ecommerce_crawler.py`** — a Telegram bot, and the home of all scraping logic. Receives part numbers via chat, scrapes `https://www.volvopenta.com/shop/0/parts/<part_number>`, extracts part details (name, price, weight, category, photo, fits-models), and replies with a per-part photo plus a downloadable results file.
- **`mobile_app.py`** — a local mobile web app. Runs an HTTP server on the device itself and serves a touch-friendly page; intended for Termux on Android, opened at `http://localhost:8000`.

**`mobile_app.py` imports `scrape_part` from `ecommerce_crawler` rather than reimplementing it** — scraping changes must go in `ecommerce_crawler.py` so both front-ends stay in sync. The app also reuses `format_result` so its saved file is byte-identical to the bot's.

There is no build system, package manifest, test suite, or CI configuration in this repo.

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

## Running the mobile app

```bash
python mobile_app.py     # then open http://localhost:8000 on the device
```

Stdlib-only (`http.server`) — no web framework, and `BOT_TOKEN` is not needed. Extra environment variables:
- `PORT` — listen port, default `8000`.
- `HOST` — bind address, default `127.0.0.1`. Set `0.0.0.0` to reach it from another device on the same Wi-Fi.

Routes: `GET /` (the page), `GET /api/part?number=<pn>` (scrapes one part, returns the result dict as JSON), `POST /api/save` (takes a JSON array of results, writes the `.txt`, returns its filename), `GET /download/<filename>` (serves it from `OUTPUT_DIR`, `basename()`-guarded against path traversal).

The page scrapes **one part per request** so results stream in and the progress bar advances, rather than blocking on a single batch call. `ThreadingHTTPServer` plus `asyncio.run()` per request bridges the sync server to the async scraper.

## Scrape architecture (the important part)

`scrape_part()` is a **two-tier pipeline that picks the cheapest path that works**, decided at runtime rather than hardcoded:

1. `_scrape_http()` — one plain `httpx` GET, no browser.
2. `_scrape_browser()` — only if tier 1 returns no usable data. Headless Chromium via Playwright. **The `playwright` import is deliberately lazy (inside the function)** so the package is not needed at all when tier 1 succeeds.

Both tiers produce the same two inputs — raw `html` and flattened `full_text` — and hand them to **`_parse_page()`, the single parsing entry point**. The browser tier calls `page.content()` alongside `body.inner_text()` specifically so the HTML-based extractors (JSON-LD, `og:image`) work identically on both paths.

A tier "succeeds" if it found either a name or a price (`_has_data()`). The chosen tier is recorded in `result["method"]` and surfaced as `Fetched via :` — that's the signal for whether Playwright is still needed at all. If both tiers fail, `result["error"]` holds both tiers' errors joined by `|`.

`fits_products` and `image` are reset between tiers so a partial tier-1 extraction can't leak into tier-2 results.

## Extraction precedence

`_parse_page()` runs sources **most-structured first**, and `_extract_fields()` only fills fields that are still `"N/A"` — so loose regex never overwrites good structured data:

1. **JSON-LD** (`_jsonld_product`) — schema.org `Product` node from any `<script type="application/ld+json">`. Handles `@graph` wrappers, `@type` as list, `offers` as dict or list, and nested `priceSpecification`. Supplies name, image, price (with currency), category. Malformed JSON is skipped, never fatal.
2. **HTML-specific extractors** — `<h1>` for name, `_extract_image()` for photo.
3. **Text regex** (`_extract_fields`) — price, weight, category, fits-models.

### Photo (`result["image"]`)

`_extract_image()` is a fallback cascade: `og:image` / `twitter:image` meta → `<link rel="image_src">` → first `<img>` that isn't obvious page chrome (skips `logo`, `icon`, `sprite`, `placeholder`, `avatar`, `flag`, `.svg`, and `data:` URIs; understands `src`, `data-src`, `data-original`, `srcset`). All URLs are absolutized against the page URL with `urljoin`.

### Other gotchas

- Scraping is best-effort against page text, not structured selectors, so upstream markup changes can silently degrade extraction. "Page loaded but nothing matched" is treated as a possible bot block, not a parsing bug.
- **Category** picks the *longest* match, not the first. Generic names in the list (`Filter`, `Engine`, `Hose`) also appear inside part names ("Oil Filter"), which sit earlier in the page than the real category — leftmost-match returns the wrong one.
- **Fits products** uses `\b([A-Z]{1,5}\d{1,5}[A-Z0-9\-]*)\b` with a `len >= 4` filter plus a `FITS_NOISE` denylist. The `\d{1,5}` (rather than `{2,5}`) is what allows single-digit series like `D4-260`. Add new false positives to `FITS_NOISE` rather than tightening the regex.
- Debug dumps (`debug_<part>_<http|browser>.txt`, first 5000 chars) are best-effort — wrapped so an unwritable `OUTPUT_DIR` can never fail a scrape.

## Telegram layer

`handle_message` parses part numbers out of free text (regex `[A-Za-z0-9\-]{4,}`, deduplicated, uppercased), scrapes them **sequentially, not concurrently**, and edits a "Processing i/N" status message as it goes. It then, in order: writes `<OUTPUT_DIR>/volvo_parts_<timestamp>.txt`, sends the summary, sends **one photo per successful part** captioned with part number, name, price and fits-models (`photo_caption`, capped at Telegram's 1024-char limit), reports the first failure's error, and uploads the results file.

Photos are sent by passing the **image URL straight to `reply_photo`** — Telegram fetches it server-side, so nothing is downloaded locally. If Telegram can't fetch the URL, it falls back to a text message with the link rather than aborting the run.

`main()` registers `/start` plus the catch-all text handler and runs polling.
