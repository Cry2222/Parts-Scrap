import asyncio
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from fake_useragent import UserAgent
from playwright.async_api import Browser, Page, Playwright, async_playwright


class EcommerceCrawler:
    """
    Web crawler for AliExpress, Alibaba, and other e-commerce sites.
    Uses Playwright with stealth techniques to avoid detection.
    """

    def __init__(self, headless: bool = False, proxy: Optional[str] = None):
        """
        Initialize crawler.

        Args:
            headless: Run browser in background (False = visible, better for avoiding detection)
            proxy: Proxy URL (e.g., 'http://user:pass@host:port')
        """
        self.headless = headless
        self.proxy = proxy
        self.ua = UserAgent()
        self.results = []

    async def start_browser(self) -> tuple[Playwright, Browser, Page]:
        """Launch browser with stealth settings."""
        playwright = await async_playwright().start()

        # Launch options to avoid detection
        launch_options = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--start-maximized",
            ],
        }

        if self.proxy:
            launch_options["proxy"] = {"server": self.proxy}

        browser = await playwright.chromium.launch(**launch_options)

        # Create context with realistic viewport and user agent
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=self.ua.random,
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        # Add stealth script to hide automation
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            window.chrome = { runtime: {} };
        """
        )

        page = await context.new_page()
        return playwright, browser, page

    async def human_like_delay(self, min_sec: float = 1.0, max_sec: float = 3.0):
        """Random delay to mimic human behavior."""
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    async def random_scroll(self, page: Page):
        """Randomly scroll to mimic human reading."""
        scroll_height = await page.evaluate("document.body.scrollHeight")
        scroll_position = random.randint(300, scroll_height - 500)
        await page.evaluate(f"window.scrollTo({{top: {scroll_position}, behavior: 'smooth'}})")
        await self.human_like_delay(0.5, 1.5)

    async def scrape_aliexpress_search(self, keyword: str, max_pages: int = 2) -> List[Dict]:
        """
        Scrape AliExpress search results for a keyword.

        Args:
            keyword: Product search term (e.g., "excavator hydraulic pump")
            max_pages: Number of search result pages to scrape

        Returns:
            List of product dictionaries
        """
        print(f"🔍 Scraping AliExpress for: {keyword}")

        playwright, browser, page = await self.start_browser()
        products = []

        try:
            for page_num in range(1, max_pages + 1):
                search_url = f"https://www.aliexpress.com/w/wholesale-{keyword.replace(' ', '-')}.html?page={page_num}"
                print(f"  📄 Page {page_num}: {search_url}")

                await page.goto(search_url, wait_until="networkidle", timeout=60000)
                await self.human_like_delay(3, 6)
                await self.random_scroll(page)

                try:
                    await page.wait_for_selector("a[href*='/item/']", timeout=15000)
                except Exception:
                    print(f"  ⚠️ No products found on page {page_num}")
                    continue

                page_products = await page.evaluate(
                    '''
                    () => {
                        const products = [];
                        const items = document.querySelectorAll('a[href*="/item/"]');

                        items.forEach(item => {
                            const product = {};
                            const container = item.closest('div[class*="gallery"]') || item.parentElement;

                            const titleElem = container.querySelector('h1, h3, [class*="title"], [class*="name"]');
                            product.title = titleElem ? titleElem.innerText.trim() : '';

                            const priceElem = container.querySelector('[class*="price"], [class*="Price"]');
                            product.price = priceElem ? priceElem.innerText.trim() : '';

                            const imgElem = container.querySelector('img');
                            product.image = imgElem ? imgElem.src : '';

                            const urlMatch = item.href.match(/\\/item\\/(\\d+)\\.html/);
                            product.product_id = urlMatch ? urlMatch[1] : '';
                            product.url = item.href;

                            if (product.title && product.product_id) {
                                products.push(product);
                            }
                        });

                        return products;
                    }
                '''
                )

                products.extend(page_products)
                print(f"  ✅ Found {len(page_products)} products")

                if page_num < max_pages:
                    await self.human_like_delay(4, 8)

        except Exception as e:
            print(f"  ❌ Error: {e}")

        finally:
            await browser.close()
            await playwright.stop()

        print(f"📊 Total products scraped: {len(products)}")
        return products

    async def scrape_aliexpress_product_details(self, product_url: str) -> Dict:
        print(f"🔍 Scraping product: {product_url}")

        playwright, browser, page = await self.start_browser()
        product_data = {"url": product_url}

        try:
            await page.goto(product_url, wait_until="networkidle", timeout=60000)
            await self.human_like_delay(3, 5)
            await self.random_scroll(page)

            details = await page.evaluate(
                '''
                () => {
                    const data = {};
                    data.title = (document.querySelector('h1[class*="product"], .product-title') || {}).innerText?.trim() || '';
                    data.price = (document.querySelector('[class*="price"], .product-price-value') || {}).innerText?.trim() || '';
                    data.original_price = (document.querySelector('[class*="original"], [class*="old-price"]') || {}).innerText?.trim() || '';
                    data.rating = (document.querySelector('[class*="rating"], .star-container') || {}).innerText?.trim() || '';
                    data.reviews = (document.querySelector('[class*="review-count"], [class*="feedback"]') || {}).innerText?.trim() || '';
                    data.orders = (document.querySelector('[class*="order"], [class*="sold"]') || {}).innerText?.trim() || '';
                    data.seller = (document.querySelector('[class*="store"], [class*="seller"]') || {}).innerText?.trim() || '';
                    data.description = (document.querySelector('[class*="description"], .product-description') || {}).innerText?.trim() || '';

                    const images = [];
                    document.querySelectorAll('img[class*="product"], .image-viewer img').forEach(img => {
                        if (img.src && img.src.includes('alicdn')) images.push(img.src);
                    });
                    data.images = images;
                    return data;
                }
            '''
            )

            product_data.update(details)
            product_data["scraped_at"] = datetime.now().isoformat()

        except Exception as e:
            print(f"  ❌ Error: {e}")
            product_data["error"] = str(e)

        finally:
            await browser.close()
            await playwright.stop()

        return product_data

    async def scrape_alibaba_search(self, keyword: str, max_pages: int = 2) -> List[Dict]:
        print(f"🔍 Scraping Alibaba for: {keyword}")

        playwright, browser, page = await self.start_browser()
        products = []

        try:
            for page_num in range(1, max_pages + 1):
                search_url = f"https://www.alibaba.com/products/{keyword.replace(' ', '_')}/page{page_num}.html"
                print(f"  📄 Page {page_num}: {search_url}")
                await page.goto(search_url, wait_until="networkidle", timeout=60000)
                await self.human_like_delay(3, 6)

                page_products = await page.evaluate(
                    '''
                    () => {
                        const products = [];
                        const items = document.querySelectorAll('[data-id], .product-item, .offer-item');

                        items.forEach(item => {
                            const product = {};
                            product.title = (item.querySelector('h2, h3, .title') || {}).innerText?.trim() || '';
                            product.price = (item.querySelector('[class*="price"]') || {}).innerText?.trim() || '';
                            product.supplier = (item.querySelector('[class*="supplier"], [class*="company"]') || {}).innerText?.trim() || '';
                            product.moq = (item.querySelector('[class*="MOQ"], [class*="moq"]') || {}).innerText?.trim() || '';
                            product.url = item.querySelector('a')?.href || '';
                            if (product.title) products.push(product);
                        });

                        return products;
                    }
                '''
                )

                products.extend(page_products)
                print(f"  ✅ Found {len(page_products)} products")

                if page_num < max_pages:
                    await self.human_like_delay(5, 10)

        except Exception as e:
            print(f"  ❌ Error: {e}")

        finally:
            await browser.close()
            await playwright.stop()

        return products

    async def scrape_generic_product(self, url: str) -> Dict:
        print(f"🔍 Scraping generic URL: {url}")

        playwright, browser, page = await self.start_browser()
        product_data = {"url": url}

        try:
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await self.human_like_delay(2, 4)

            product_data.update(
                await page.evaluate(
                    '''
                    () => {
                        const extractWithSelectors = (selectors) => {
                            for (const selector of selectors) {
                                const elem = document.querySelector(selector);
                                if (elem && elem.innerText.trim()) return elem.innerText.trim();
                            }
                            return '';
                        };

                        const priceSelectors = [
                            '[class*="price"]', '[itemprop="price"]', '.price',
                            '.product-price', '[class*="Price"]', '.sale-price',
                            '.current-price', '[data-price]'
                        ];

                        const titleSelectors = [
                            'h1', '[class*="title"]', '[itemprop="name"]',
                            '.product-title', '[class*="product-name"]'
                        ];

                        const ratingSelectors = [
                            '[class*="rating"]', '[itemprop="ratingValue"]',
                            '.review-score', '[class*="stars"]'
                        ];

                        const brandSelectors = [
                            '[class*="brand"]', '[itemprop="brand"]', '.brand-name'
                        ];

                        return {
                            title: extractWithSelectors(titleSelectors),
                            price: extractWithSelectors(priceSelectors),
                            rating: extractWithSelectors(ratingSelectors),
                            brand: extractWithSelectors(brandSelectors),
                            scraped_at: new Date().toISOString()
                        };
                    }
                '''
                )
            )

        except Exception as e:
            print(f"  ❌ Error: {e}")
            product_data["error"] = str(e)

        finally:
            await browser.close()
            await playwright.stop()

        return product_data

    def save_to_json(self, data: List[Dict], filename: str = "scraped_data.json"):
        output_path = Path(filename)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"💾 Saved to {output_path.absolute()}")

    def save_to_csv(self, data: List[Dict], filename: str = "scraped_data.csv"):
        if not data:
            print("⚠️ No data to save")
            return

        df = pd.DataFrame(data)
        df.to_csv(filename, index=False, encoding="utf-8")
        print(f"💾 Saved to {filename}")

    async def run_with_retry(self, scraper_func, *args, max_retries: int = 3, **kwargs):
        for attempt in range(max_retries):
            try:
                result = await scraper_func(*args, **kwargs)
                if result:
                    return result
            except Exception as e:
                print(f"  Attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 10
                    print(f"  Waiting {wait_time} seconds before retry...")
                    await asyncio.sleep(wait_time)
        return []


async def main():
    crawler = EcommerceCrawler(headless=False)

    print("=" * 60)
    print("E-COMMERCE WEB CRAWLER")
    print("=" * 60)

    print("\n📌 EXAMPLE 1: Search AliExpress")
    print("-" * 40)
    results = await crawler.scrape_aliexpress_search("excavator hydraulic pump", max_pages=2)
    crawler.save_to_json(results, "aliexpress_search_results.json")

    print("\n📌 EXAMPLE 3: Search Alibaba")
    print("-" * 40)
    alibaba_results = await crawler.scrape_alibaba_search("excavator parts", max_pages=1)
    crawler.save_to_json(alibaba_results, "alibaba_search_results.json")

    print("\n✅ All scraping completed!")


if __name__ == "__main__":
    asyncio.run(main())
