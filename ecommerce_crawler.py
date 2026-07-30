import re
import os
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from playwright.async_api import async_playwright

# ========== CONFIG ==========
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
# ============================


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
        "status": "failed",
        "error": ""
    }

    try:
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

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
                viewport={"width": 1280, "height": 720},
                java_script_enabled=True,
            )

            page = await context.new_page()

            # Go to page
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(5000)  # extra wait for JS

            full_text = await page.locator("body").inner_text()

            # ===== DEBUG: Save page text for checking =====
            debug_file = f"/storage/emulated/0/debug_{part_number}.txt"
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(full_text[:5000])  # save first 5000 characters

            # Name
            try:
                if await page.locator("h1").count() > 0:
                    name = await page.locator("h1").first.inner_text()
                    result["name"] = name.replace("Parts:", "").strip()
            except:
                pass

            # Price
            price_match = re.search(r'\$[\d,]+\.?\d*', full_text)
            if price_match:
                result["price"] = price_match.group(0)

            # Weight
            weight_match = re.search(r'Weight incl\. packaging[:\s]*([\d.,]+\s*(lbs|kg|lb))', full_text, re.IGNORECASE)
            if weight_match:
                result["weight"] = weight_match.group(1).strip()

            # Category
            cat_match = re.search(
                r'(Fuel System|Lubricating and Oil System|Cooling System|Electrical System|Engine|Transmission|Propulsion|Filter|Hose|Bearing)',
                full_text, re.IGNORECASE
            )
            if cat_match:
                result["category"] = cat_match.group(1)

            # Fits products
            fits_section = re.search(
                r'Fits products(.*?)(Dealer information|Log in|Choose dealer|Specifications|$)',
                full_text, re.IGNORECASE | re.DOTALL
            )
            if fits_section:
                section_text = fits_section.group(1)
                models = re.findall(r'\b([A-Z]{1,5}\d{2,5}[A-Z0-9\-]*)\b', section_text)
                seen = set()
                for m in models:
                    if m not in seen and len(m) >= 4:
                        seen.add(m)
                        result["fits_products"].append(m)

            if result["name"] != "N/A" or result["price"] != "N/A":
                result["status"] = "success"
            else:
                result["status"] = "failed"
                result["error"] = "Page loaded but no useful data found (possible bot block)"

            await browser.close()

    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)[:200]

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
    filepath = f"/storage/emulated/0/{filename}"

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