import re
import os
import json
import html as html_lib
from datetime import datetime
from urllib.parse import urljoin

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

# Tokens that match the model-code shape but are never real models.
FITS_NOISE = {
    "HTTP", "HTTPS", "JSON", "HTML", "UTF8", "ITEM", "PART", "PAGE", "MENU",
    "CART", "SHOP", "VOLVO", "PENTA", "USD", "SEK", "EUR", "ISO", "SKU",
}
# ============================


# ---------- generic HTML helpers ----------

def _html_to_text(html: str) -> str:
    """Flatten raw HTML into visible-ish text so the same regexes work on it."""
    html = re.sub(r'(?is)<(script|style|noscript)[^>]*>.*?</\1>', ' ', html)
    html = re.sub(r'(?s)<[^>]+>', '\n', html)
    html = html_lib.unescape(html).replace('\xa0', ' ')  # &nbsp; -> real space
    html = re.sub(r'[ \t\r\f\v]+', ' ', html)
    html = re.sub(r'\n[ ]*(\n[ ]*)+', '\n', html)
    return html.strip()


def _attr(tag: str, name: str) -> str:
    """Read one attribute out of a single tag string, quoted or not."""
    m = re.search(
        rf'(?is)\b{name}\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag
    )
    if not m:
        return ""
    return (m.group(2) or m.group(3) or m.group(4) or "").strip()


# ---------- JSON-LD (schema.org) ----------

def _jsonld_nodes(html: str):
    """Yield every dict inside every <script type="application/ld+json"> block."""
    for m in re.finditer(
        r'(?is)<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html
    ):
        raw = m.group(1).strip()
        data = None
        for candidate in (raw, html_lib.unescape(raw)):
            try:
                data = json.loads(candidate)
                break
            except ValueError:
                continue
        if data is None:
            continue

        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                if "@graph" in node:
                    stack.append(node["@graph"])


def _jsonld_product(html: str):
    for node in _jsonld_nodes(html):
        raw_type = node.get("@type", "")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        if any(str(t).lower().startswith("product") for t in types):
            return node
    return None


def _first_url(value) -> str:
    """schema.org image/url fields may be str, dict, or list of either."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _first_url(value.get("url") or value.get("contentUrl") or "")
    if isinstance(value, list):
        for v in value:
            found = _first_url(v)
            if found:
                return found
    return ""


def _offer_price(node: dict) -> str:
    offers = node.get("offers")
    candidates = offers if isinstance(offers, list) else [offers]
    for offer in candidates:
        if not isinstance(offer, dict):
            continue
        spec = offer.get("priceSpecification")
        if isinstance(spec, dict):
            offer = {**offer, **spec}
        price = offer.get("price") or offer.get("lowPrice")
        if price in (None, ""):
            continue
        currency = str(offer.get("priceCurrency") or "USD").upper()
        return f"${price}" if currency == "USD" else f"{price} {currency}"
    return ""


# ---------- field extractors ----------

def _extract_name_from_html(html: str) -> str:
    match = re.search(r'(?is)<h1[^>]*>(.*?)</h1>', html)
    if not match:
        return ""
    name = _html_to_text(match.group(1)).replace("\n", " ")
    return name.replace("Parts:", "").strip()


def _extract_image(html: str, base_url: str) -> str:
    """Product photo URL. og:image is the most reliable signal on shop pages;
    fall back to link[rel=image_src] and then the first non-chrome <img>."""
    for tag in re.findall(r'(?is)<meta[^>]*>', html):
        prop = (_attr(tag, "property") or _attr(tag, "name")).lower()
        if prop in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"):
            url = _attr(tag, "content")
            if url:
                return urljoin(base_url, url)

    for tag in re.findall(r'(?is)<link[^>]*>', html):
        if "image_src" in _attr(tag, "rel").lower():
            url = _attr(tag, "href")
            if url:
                return urljoin(base_url, url)

    for tag in re.findall(r'(?is)<img[^>]*>', html):
        url = _attr(tag, "src") or _attr(tag, "data-src") or _attr(tag, "data-original")
        if not url:
            srcset = _attr(tag, "srcset")
            if srcset:
                url = srcset.split(",")[0].strip().split(" ")[0]
        if not url or url.lower().startswith("data:"):
            continue
        low = url.lower()
        if any(junk in low for junk in
               ("logo", "icon", "sprite", "placeholder", "avatar", "flag", ".svg")):
            continue
        return urljoin(base_url, url)

    return ""


def _extract_fits(full_text: str) -> list:
    """Model codes listed under the page's 'Fits products' section."""
    section = re.search(
        r'Fits products(.*?)('
        r'Dealer information|Log in|Choose dealer|Specifications|Related|'
        r'Documents|Add to cart|Contact|Cookie|$)',
        full_text, re.IGNORECASE | re.DOTALL
    )
    if not section:
        return []

    # \d{1,5} (not {2,5}) so single-digit series like "D4-260" are caught too.
    models = re.findall(r'\b([A-Z]{1,5}\d{1,5}[A-Z0-9\-]*)\b', section.group(1))
    out = []
    seen = set()
    for m in models:
        if len(m) >= 4 and m not in seen and m.upper() not in FITS_NOISE:
            seen.add(m)
            out.append(m)
    return out


def _extract_fields(result: dict, full_text: str):
    """Text-based extraction. Only fills fields still unset, so richer
    structured data (JSON-LD) always wins over loose regex matches."""
    if result["price"] == "N/A":
        price_match = re.search(r'\$[\d,]+\.?\d*', full_text)
        if price_match:
            result["price"] = price_match.group(0)

    if result["weight"] == "N/A":
        weight_match = re.search(
            r'Weight incl\. packaging[:\s]*([\d.,]+\s*(lbs|kg|lb))', full_text, re.IGNORECASE
        )
        if weight_match:
            result["weight"] = weight_match.group(1).strip()

    if result["category"] == "N/A":
        # Prefer the most specific match: generic words like "Filter" also occur in
        # part names ("Oil Filter"), which sit earlier in the page than the category.
        cat_matches = re.findall(
            r'(Fuel System|Lubricating and Oil System|Cooling System|Electrical System|Engine|Transmission|Propulsion|Filter|Hose|Bearing)',
            full_text, re.IGNORECASE
        )
        if cat_matches:
            result["category"] = max(cat_matches, key=len)

    if not result["fits_products"]:
        result["fits_products"] = _extract_fits(full_text)


def _parse_page(url: str, html: str, full_text: str, result: dict):
    """Single parsing entry point shared by both scrape tiers."""
    product = _jsonld_product(html) if html else None
    if product:
        name = product.get("name")
        if isinstance(name, str) and name.strip():
            result["name"] = name.replace("Parts:", "").strip()

        image = _first_url(product.get("image"))
        if image:
            result["image"] = urljoin(url, image)

        price = _offer_price(product)
        if price:
            result["price"] = price

        category = product.get("category")
        if isinstance(category, str) and category.strip():
            result["category"] = category.strip()

    if result["name"] == "N/A" and html:
        name = _extract_name_from_html(html)
        if name:
            result["name"] = name

    if result["image"] == "N/A" and html:
        image = _extract_image(html, url)
        if image:
            result["image"] = image

    _extract_fields(result, full_text)


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


# ---------- scrape tiers ----------

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
    _parse_page(url, html, full_text, result)
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
            # Rendered HTML too, so JSON-LD / og:image extraction works here as well.
            html = await page.content()
            _save_debug(result["part_number"], full_text, "browser")
            _parse_page(url, html, full_text, result)
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
        "image": "N/A",
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
        result["image"] = "N/A"
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
        f"Photo       : {data['image']}\n"
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


def photo_caption(data: dict) -> str:
    """Short caption sent alongside each part photo."""
    lines = [f"{data['part_number']} — {data['name']}"]
    if data["price"] != "N/A":
        lines.append(f"Price: {data['price']}")
    if data["fits_products"]:
        shown = ", ".join(data["fits_products"][:12])
        extra = len(data["fits_products"]) - 12
        if extra > 0:
            shown += f" (+{extra} more)"
        lines.append(f"Fits ({len(data['fits_products'])}): {shown}")
    return "\n".join(lines)[:1024]  # Telegram caption limit


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

    # Send each part photo with its fits-model list. Telegram fetches the image
    # URL itself, so nothing is downloaded locally.
    for data in all_results:
        if data["status"] != "success" or data["image"] == "N/A":
            continue
        try:
            await update.message.reply_photo(photo=data["image"], caption=photo_caption(data))
        except Exception:
            # Unreachable/blocked image URL must not abort the rest of the run.
            await update.message.reply_text(f"{photo_caption(data)}\nPhoto: {data['image']}")

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
