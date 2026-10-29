"""Read the Taiwan storefront independently, using its public search page."""
import re
import os
import asyncio
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse


class SourceUnavailable(RuntimeError):
    """A search could not be verified; this must not become an empty result."""


@dataclass
class TaiwanItem:
    id_: str
    name: str
    real_price: int
    thumbnails: list[str]
    url: str
    currency: str = "TWD"
    item_type: str = "ITEM_TYPE_MERCARI"
    auction: object = None


def original_item_id(image_url: str, fallback: str) -> str:
    parsed = urlparse(image_url)
    if parsed.hostname and (parsed.hostname == "mercdn.net" or parsed.hostname.endswith(".mercdn.net")):
        match = re.search(r"/(m\d+)_\d+\.", parsed.path)
        if match:
            return match.group(1)
    return "tw:" + fallback


def taiwan_search_url(keyword: str) -> str:
    # These values are the storefront's own "available" and "new items" controls.
    return "https://tw.mercari.com/zh-hant/search?" + urlencode({
        "keyword": keyword, "status": "in-stock", "availability": "1", "sort": "1",
    })


EXTRACT_SEARCH = r"""() => {
    const root = document.querySelector('[data-testid="search-result-items"]');
    const status = document.querySelector('[data-testid="item-availability-filter-panel"]')?.value;
    const sort = document.querySelector('[data-testid="item-sort-panel"]')?.innerText;
    const cards = root ? [...root.querySelectorAll('a[data-testid^="serp-item-tile"]')] : [];
    const items = cards.map(a => {
        const text = a.innerText.trim();
        const price = text.match(/NT\$\s*([\d,]+)/);
        return {
            url: a.href,
            title: price ? text.slice(0, price.index).trim() : '',
            price: price ? Number(price[1].replaceAll(',', '')) : null,
            image: a.querySelector('img')?.src || '',
        };
    });
    const empty = /找不到符合|沒有找到|沒有符合|找不到商品|沒有搜尋結果|沒有搜索結果/.test(document.querySelector('main')?.innerText || '');
    return {status, sort, items, empty, hasRoot: !!root};
}"""


def parse_taiwan_results(data: dict) -> list[TaiwanItem]:
    if data.get("status") != "in-stock" or data.get("sort") != "新商品":
        raise SourceUnavailable("Taiwan search filters were not confirmed")
    if not data.get("items") and not data.get("empty"):
        raise SourceUnavailable("Taiwan search results were not confirmed")
    unique = {}
    for raw in data.get("items", []):
        parsed = urlparse(raw.get("url", ""))
        if parsed.hostname != "tw.mercari.com" or not re.fullmatch(r"/zh-hant/items/[a-f0-9-]+", parsed.path):
            raise SourceUnavailable("Unexpected Taiwan item URL")
        if not raw.get("title") or not isinstance(raw.get("price"), (int, float)):
            raise SourceUnavailable("Incomplete Taiwan item")
        image = raw.get("image") or ""
        identity = original_item_id(image, parsed.path.rsplit("/", 1)[-1])
        unique[identity] = TaiwanItem(
            id_=identity, name=raw["title"], real_price=raw["price"],
            thumbnails=[image] if image.startswith("https://") else [], url=raw["url"],
        )
    return list(unique.values())


class TaiwanSearch:
    """Reuse one headless browser; do not log queries, pages, or challenge content."""

    def __init__(self):
        self.playwright = None
        self.browser = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def search(self, keyword: str) -> list[TaiwanItem]:
        from playwright.async_api import TimeoutError as BrowserTimeout
        for attempt in range(2):
            try:
                return await self._search_once(keyword)
            except BrowserTimeout as error:
                if attempt:
                    raise SourceUnavailable("Taiwan search timed out after one retry") from error
                await asyncio.sleep(1)

    async def _search_once(self, keyword: str) -> list[TaiwanItem]:
        if self.browser is None:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True, channel="chromium")
        platform = "Windows NT 10.0; Win64; x64" if os.name == "nt" else "X11; Linux x86_64"
        agent = (f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
                 f"Chrome/{self.browser.version} Safari/537.36")
        page = await self.browser.new_page(locale="zh-TW", user_agent=agent)
        try:
            response = await page.goto(taiwan_search_url(keyword), wait_until="domcontentloaded", timeout=30000)
            if response is None or response.status >= 400:
                status = response.status if response is not None else "no response"
                raise SourceUnavailable(f"Taiwan search request was rejected (HTTP {status})")
            await page.wait_for_selector('[data-testid="item-sort-panel"]', timeout=20000)
            await page.wait_for_function("""() =>
                !!document.querySelector('[data-testid="search-result-items"] a[data-testid^="serp-item-tile"]') ||
                /找不到符合|沒有找到|沒有符合|找不到商品|沒有搜尋結果|沒有搜索結果/.test(document.querySelector('main')?.innerText || '')
            """, timeout=20000)
            return parse_taiwan_results(await page.evaluate(EXTRACT_SEARCH))
        finally:
            await page.close()
