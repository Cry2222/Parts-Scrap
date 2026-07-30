import re
import os
import html as html_lib
from datetime import datetime

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Android/Termux writes to shared storage; anywhere else falls back to the
# working directory so the bot can run on a plain server too.
ANDROID_DIR = "/storage/emulated/0"
OUTPUT_DIR = os.environ.get("OUTPUT_DIR") or (
    ANDROID_DIR if os.path.isdir(ANDROID_DIR) else os.getcwd()
)

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
# ============================


def _html_to_text(html: str) -> str:
    """Flatten raw HTML into visible-ish text so the same regexes work on it."""
    html = re.sub(r'(?is)<(script|style|noscript)[^>]*>.*?</\1>', ' ', html)
    html = re.sub(r'(?s)<[^>]+>', '\n', html)
    html = html_lib.unescape(html).replace('\xa0', ' ')  # &nbsp; -> real space
    html = re.sub(r'[ \t\r\f\v]+', ' ', html)
    html = re.sub(r'\n[ ]*(\n[ ]*)+', '\n', html)
    return html.strip()


def _extract_name_from_html(html: str) -> str:
    match = re.search(r'(?is)<h1[^>]*>(.*?)</h1>', html)
    if not match:
        return ""
    name = _html_to_text(match.group(1)).replace("\n", " ")
    return name.replace("Parts:", "").strip()


def _extract_fields(result: dict, full_text: str):
    """Pull price/weight/category/fits out of page text (shared by both paths)."""
    price_match = re.search(r'\$[\d,]+\.?\d*', full_text)
    if price_match:
        result["price"] = price_match.group(0)

    weight_match = re.search(
        r'Weight incl\. packaging[:\s]*([\d.,]+\s*(lbs|kg|lb))', full_text, re.IGNORECASE
    )
    if weight_match:
        result["weight"] = weight_match.group(1).strip()

    # Prefer the most specific match: generic words like "Filter" also occur in
    # part names ("Oil Filter"), which sit earlier in the page than the category.
    cat_matches = re.findall(
        r'(Fuel System|Lubricating and Oil System|Cooling System|Electrical System|Engine|Transmission|Propulsion|Filter|Hose|Bearing)',
        full_text, re.IGNORECASE
    )
    if cat_matches:
        result["category"] = max(cat_matches, key=len)

    fits_section = re.search(
        r'Fits products(.*?)(Dealer information|Log in|Choose dealer|Specifications|$)',
        full_text, re.IGNORECASE | re.DOTALL
    )
    if fits_section:
        # \d{1,5} (not {2,5}) so single-digit series like "D4-260" are caught too.
        models = re.findall(r'\b([A-Z]{1,5}\d{1,5}[A-Z0-9\-]*)\b', fits_section.group(1))
        seen = set()
        for m in models:
            if m not in seen and len(m) >= 4:
                seen.add(m)
                result["fits_products"].append(m)


def _save_debug(part_number: str, text: str, suffix: str):
    """Best-effort page dump for debugging; never fails the scrape."""
    try:
        path = os.path.join(OUTPUT_DIR, f"debug_{part_number}_{suffix}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text[:5000])
    except OSError:
        pass


def _has_data(result: dict) -> bool:
    return result["name"] != "N/A" or result["price"] != "N/A"


async def _scrape_http(url: str, result: dict) -> bool:
    """Fast path: one plain HTTP request, no browser. Works if the site
    server-renders the part data into the HTML it returns."""
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    full_text = _html_to_text(html)
    _save_debug(result["part_number"], full_text, "http")

    name = _extract_name_from_html(html)
    if name:
        result["name"] = name
    _extract_fields(result, full_text)

    return _has_data(result)


async def _scrape_browser(url: str, result: dict) -> bool:
    """Fallback: render with headless Chromium for client-side-only pages.
    Imported lazily so Playwright is only needed if the fast path fails."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
                "--no-zygote",
            ]
        )
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 720},
                java_script_enabled=True,
            )
            page = await context.new_page()

            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(5000)  # extra wait for JS

            full_text = await page.locator("body").inner_text()
            _save_debug(result["part_number"], full_text, "browser")

            if await page.locator("h1").count() > 0:
                name = await page.locator("h1").first.inner_text()
                result["name"] = name.replace("Parts:", "").strip()

            _extract_fields(result, full_text)
        finally:
            await browser.close()

    return _has_data(result)


async def scrape_part(part_number: str) -> dict:
    part_number = part_number.strip().upper()
    url = f"https://www.volvopenta.com/shop/0/parts/{part_number}"

    result = {
        "part_number": part_number,
        "name": "N/A",
        "category": "N/A",
        "price": "N/A",
        "weight": "N/A",
        "fits_products": [],
        "url": url,
        "method": "N/A",
        "status": "failed",
        "error": ""
    }

    errors = []

    # Try the cheap request first, only spin up a browser if it comes back empty.
    for method, scraper in (("http", _scrape_http), ("browser", _scrape_browser)):
        # Reset partial extractions so a failed attempt can't pollute the next one.
        result["fits_products"] = []
        try:
            if await scraper(url, result):
                result["method"] = method
                result["status"] = "success"
                return result
            errors.append(f"{method}: no useful data found")
        except ImportError:
            errors.append("browser: playwright not installed")
        except Exception as e:
            errors.append(f"{method}: {str(e)[:120]}")

    result["status"] = "failed"
    result["error"] = " | ".join(errors)[:200]
    return result


def format_result(data: dict) -> str:
    fits = "\n".join([f"  - {m}" for m in data["fits_products"]]) if data["fits_products"] else "  Not found"

    text = (
        f"Part Number : {data['part_number']}\n"
        f"Name        : {data['name']}\n"
        f"Category    : {data['category']}\n"
        f"Price       : {data['price']}\n"
        f"Weight      : {data['weight']}\n"
        f"Fits Models : {len(data['fits_products'])} found\n"
        f"{fits}\n"
        f"URL         : {data['url']}\n"
        f"Fetched via : {data['method']}\n"
        f"Status      : {data['status']}\n"
    )
    if data.get("error"):
        text += f"Error       : {data['error']}\n"
    text += f"{'-'*50}\n"
    return text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Volvo Penta Parts Bot (Improved)\n\n"
        "Send part number(s):\n"
        "8159975\n\n"
        "or multiple:\n"
        "8159975 3580458"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.startswith("/"):
        return

    part_numbers = re.findall(r'[A-Za-z0-9\-]{4,}', text)
    part_numbers = list(dict.fromkeys(part_numbers))

    if not part_numbers:
        await update.message.reply_text("Send valid part number(s)")
        return

    waiting = await update.message.reply_text(f"Processing {len(part_numbers)} part(s)...")

    all_results = []
    success_count = 0

    for i, pn in enumerate(part_numbers, 1):
        await waiting.edit_text(f"Processing {i}/{len(part_numbers)} → {pn}")
        data = await scrape_part(pn)
        all_results.append(data)
        if data["status"] == "success":
            success_count += 1

    # Save file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"volvo_parts_{timestamp}.txt"
    filepath = os.path.join(OUTPUT_DIR, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("Volvo Penta Parts Lookup\n")
        f.write(f"Date: {datetime.now()}\n")
        f.write(f"Total: {len(part_numbers)} | Success: {success_count}\n\n")
        for data in all_results:
            f.write(format_result(data))

    summary = f"Finished!\nTotal: {len(part_numbers)}\nSuccess: {success_count}\nFailed: {len(part_numbers)-success_count}\n\nFile: {filename}"
    await waiting.edit_text(summary)

    # Show error of first failed part (for debugging)
    for data in all_results:
        if data["status"] != "success":
            await update.message.reply_text(f"Error on {data['part_number']}:\n{data.get('error', 'Unknown')}")
            break

    # Send file
    try:
        with open(filepath, "rb") as f:
            await update.message.reply_document(document=f, filename=filename)
    except Exception as e:
        await update.message.reply_text(f"Could not send file: {e}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
