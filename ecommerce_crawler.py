import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from fake_useragent import UserAgent
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

try:
    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except Exception:  # optional dependency
    Update = Any
    ContextTypes = Any
    Application = None


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ecommerce_crawler")


@dataclass
class CrawlResult:
    source: str
    query: str
    items: List[Dict[str, Any]]
    started_at: str
    finished_at: str


class EcommerceCrawler:
    """Robust async crawler for AliExpress, Alibaba, and generic product pages."""

    def __init__(self, headless: bool = True, proxy: Optional[str] = None, timeout_ms: int = 60000):
        self.headless = headless
        self.proxy = proxy
        self.timeout_ms = timeout_ms
        self.ua = UserAgent()

    async def start_browser(self) -> tuple[Playwright, Browser, BrowserContext, Page]:
        playwright = await async_playwright().start()
        launch_options: Dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-features=IsolateOrigins,site-per-process",
                "--start-maximized",
            ],
        }
        if self.proxy:
            launch_options["proxy"] = {"server": self.proxy}

        browser = await playwright.chromium.launch(**launch_options)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=self.ua.random,
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
            window.chrome = { runtime: {} };
            """
        )
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)
        return playwright, browser, context, page

    async def close_browser(self, playwright: Playwright, browser: Browser, context: BrowserContext) -> None:
        await context.close()
        await browser.close()
        await playwright.stop()

    async def human_like_delay(self, min_sec: float = 0.8, max_sec: float = 2.0) -> None:
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    async def random_scroll(self, page: Page) -> None:
        scroll_height = await page.evaluate("Math.max(document.body.scrollHeight, 1000)")
        top = random.randint(200, max(300, scroll_height - 300))
        await page.evaluate(f"window.scrollTo({{top: {top}, behavior: 'smooth'}})")
        await self.human_like_delay(0.4, 1.2)

    async def run_with_retry(self, coro_func, *args, max_retries: int = 3, **kwargs):
        for attempt in range(1, max_retries + 1):
            try:
                return await coro_func(*args, **kwargs)
            except Exception as exc:
                logger.warning("Attempt %s/%s failed: %s", attempt, max_retries, exc)
                if attempt == max_retries:
                    raise
                await asyncio.sleep(2 * attempt)

    @staticmethod
    def _dedupe_by_url(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen, out = set(), []
        for item in items:
            url = item.get("url", "")
            if url and url not in seen:
                out.append(item)
                seen.add(url)
        return out

    async def scrape_aliexpress_search(self, keyword: str, max_pages: int = 1) -> List[Dict[str, Any]]:
        playwright, browser, context, page = await self.start_browser()
        products: List[Dict[str, Any]] = []
        try:
            for page_num in range(1, max_pages + 1):
                url_keyword = re.sub(r"\s+", "-", keyword.strip())
                search_url = f"https://www.aliexpress.com/w/wholesale-{url_keyword}.html?page={page_num}"
                logger.info("AliExpress page %s: %s", page_num, search_url)
                await page.goto(search_url, wait_until="domcontentloaded")
                await self.human_like_delay(1.5, 3)
                await self.random_scroll(page)
                await page.wait_for_selector("a[href*='/item/']")
                page_products = await page.evaluate(
                    """
                    () => {
                        const products = [];
                        const links = document.querySelectorAll('a[href*="/item/"]');
                        links.forEach((item) => {
                            const container = item.closest('div[class*="gallery"]') || item.parentElement;
                            if (!container) return;
                            const title = container.querySelector('h1, h3, [class*="title"], [class*="name"]')?.innerText?.trim() || '';
                            const price = container.querySelector('[class*="price"], [class*="Price"]')?.innerText?.trim() || '';
                            const image = container.querySelector('img')?.src || '';
                            const match = item.href.match(new RegExp('item/(\\d+)\\.html'));
                            const product_id = match ? match[1] : '';
                            if (title && product_id) {
                                products.push({title, price, image, product_id, url: item.href});
                            }
                        });
                        return products;
                    }
                    """
                )
                products.extend(page_products)
                await self.human_like_delay(1, 2)
        finally:
            await self.close_browser(playwright, browser, context)
        return self._dedupe_by_url(products)

    async def scrape_alibaba_search(self, keyword: str, max_pages: int = 1) -> List[Dict[str, Any]]:
        playwright, browser, context, page = await self.start_browser()
        products: List[Dict[str, Any]] = []
        try:
            for page_num in range(1, max_pages + 1):
                url_keyword = re.sub(r"\s+", "_", keyword.strip())
                search_url = f"https://www.alibaba.com/products/{url_keyword}/page{page_num}.html"
                logger.info("Alibaba page %s: %s", page_num, search_url)
                await page.goto(search_url, wait_until="domcontentloaded")
                await self.human_like_delay(1.5, 3)
                page_products = await page.evaluate(
                    """
                    () => {
                        const products = [];
                        const items = document.querySelectorAll('[data-id], .product-item, .offer-item');
                        items.forEach((item) => {
                            const title = item.querySelector('h2, h3, .title')?.innerText?.trim() || '';
                            if (!title) return;
                            const price = item.querySelector('[class*="price"]')?.innerText?.trim() || '';
                            const supplier = item.querySelector('[class*="supplier"], [class*="company"]')?.innerText?.trim() || '';
                            const moq = item.querySelector('[class*="MOQ"], [class*="moq"]')?.innerText?.trim() || '';
                            const url = item.querySelector('a')?.href || '';
                            products.push({title, price, supplier, moq, url});
                        });
                        return products;
                    }
                    """
                )
                products.extend(page_products)
        finally:
            await self.close_browser(playwright, browser, context)
        return self._dedupe_by_url(products)

    async def scrape_generic_product(self, url: str) -> Dict[str, Any]:
        playwright, browser, context, page = await self.start_browser()
        product_data: Dict[str, Any] = {"url": url}
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await self.human_like_delay(1.0, 2.5)
            product_data.update(
                await page.evaluate(
                    """
                    () => {
                        const pick = (selectors) => {
                            for (const s of selectors) {
                                const e = document.querySelector(s);
                                if (e && e.innerText?.trim()) return e.innerText.trim();
                            }
                            return '';
                        };
                        return {
                            title: pick(['h1','[class*="title"]','[itemprop="name"]']),
                            price: pick(['[class*="price"]','[itemprop="price"]','.price']),
                            rating: pick(['[class*="rating"]','[itemprop="ratingValue"]']),
                            brand: pick(['[class*="brand"]','[itemprop="brand"]']),
                            scraped_at: new Date().toISOString(),
                        };
                    }
                    """
                )
            )
        finally:
            await self.close_browser(playwright, browser, context)
        return product_data

    @staticmethod
    def save_to_json(data: List[Dict[str, Any]], filename: str = "scraped_data.json") -> Path:
        path = Path(filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    @staticmethod
    def save_to_csv(data: List[Dict[str, Any]], filename: str = "scraped_data.csv") -> Optional[Path]:
        if not data:
            return None
        path = Path(filename)
        pd.DataFrame(data).to_csv(path, index=False, encoding="utf-8")
        return path


class TelegramCrawlerBot:
    """Telegram wrapper for user-friendly crawler usage."""

    def __init__(self, token: str, crawler: Optional[EcommerceCrawler] = None):
        if Application is None:
            raise RuntimeError("python-telegram-bot is required. Install with: pip install python-telegram-bot")
        self.token = token
        self.crawler = crawler or EcommerceCrawler(headless=True)
        self.app = Application.builder().token(token).build()
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.app.add_handler(CommandHandler("start", self.start_cmd))
        self.app.add_handler(CommandHandler("help", self.help_cmd))
        self.app.add_handler(CommandHandler("ae", self.ae_cmd))
        self.app.add_handler(CommandHandler("ab", self.ab_cmd))
        self.app.add_handler(CommandHandler("url", self.url_cmd))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.fallback_text))

    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("👋 Welcome! Use /help to see crawler commands.")

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "*Crawler Commands*\n"
            "• `/ae <keyword>` - AliExpress search\n"
            "• `/ab <keyword>` - Alibaba search\n"
            "• `/url <product_url>` - Generic product scrape\n"
            "\nExamples:\n`/ae excavator hydraulic pump`\n`/ab excavator parts`\n`/url https://example.com/product`")
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def ae_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyword = " ".join(context.args).strip()
        if not keyword:
            await update.message.reply_text("Please provide a search keyword. Example: /ae excavator pump")
            return
        await self._run_search(update, source="aliexpress", keyword=keyword)

    async def ab_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyword = " ".join(context.args).strip()
        if not keyword:
            await update.message.reply_text("Please provide a search keyword. Example: /ab excavator parts")
            return
        await self._run_search(update, source="alibaba", keyword=keyword)

    async def url_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        target = " ".join(context.args).strip()
        if not target:
            await update.message.reply_text("Please provide a URL. Example: /url https://site.com/product")
            return
        await update.message.reply_text("⏳ Scraping product page...")
        try:
            data = await self.crawler.run_with_retry(self.crawler.scrape_generic_product, target)
            await update.message.reply_text(f"✅ Done\n```{json.dumps(data, indent=2)[:3500]}```", parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            await update.message.reply_text(f"❌ Failed to scrape URL: {exc}")

    async def _run_search(self, update: Update, source: str, keyword: str):
        await update.message.reply_text(f"⏳ Searching {source} for: {keyword}")
        started = datetime.now(timezone.utc).isoformat()
        try:
            if source == "aliexpress":
                items = await self.crawler.run_with_retry(self.crawler.scrape_aliexpress_search, keyword, max_pages=1)
            else:
                items = await self.crawler.run_with_retry(self.crawler.scrape_alibaba_search, keyword, max_pages=1)

            result = CrawlResult(
                source=source,
                query=keyword,
                items=items,
                started_at=started,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            await self._send_result(update, result)
        except Exception as exc:
            await update.message.reply_text(f"❌ Search failed: {exc}")

    async def _send_result(self, update: Update, result: CrawlResult):
        top = result.items[:5]
        if not top:
            await update.message.reply_text("No products found. Try another keyword.")
            return

        lines = [f"✅ {result.source.title()} results for *{result.query}* ({len(result.items)} items)"]
        for i, item in enumerate(top, start=1):
            title = item.get("title", "(no title)")[:100]
            price = item.get("price", "n/a")
            url = item.get("url", "")
            lines.append(f"{i}. {title}\n   💰 {price}\n   🔗 {url}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def fallback_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Please use a command. Try /help.")

    def run(self) -> None:
        logger.info("Starting Telegram crawler bot")
        self.app.run_polling()


async def example_cli_run() -> None:
    crawler = EcommerceCrawler(headless=True)
    results = await crawler.scrape_aliexpress_search("excavator hydraulic pump", max_pages=1)
    path = crawler.save_to_json(results, "aliexpress_search_results.json")
    logger.info("Saved %s items to %s", len(results), path)


if __name__ == "__main__":
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if bot_token:
        TelegramCrawlerBot(bot_token).run()
    else:
        asyncio.run(example_cli_run())
