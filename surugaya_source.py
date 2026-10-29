"""Suruga-ya's Japanese storefront, using its native search controls."""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from market_sources import SourceUnavailable

HOST = "www.suruga-ya.jp"
NEWEST = "modificationTime:descending"


def surugaya_search_url(keyword: str) -> str:
    # 品切れ ON includes sold-out goods; OFF excludes them.
    return f"https://{HOST}/search?" + urlencode({
        "search_word": keyword, "ck": "true", "inStock": "Off", "rankBy": NEWEST,
    })


@dataclass
class SurugayaItem:
    id_: str
    name: str
    real_price: int
    thumbnails: list[str]
    url: str
    price_note: str
    price_from: bool = False
    source: str = "surugaya"
    currency: str = "JPY"
    item_type: str = "ITEM_TYPE_SURUGAYA"
    auction: object = None


EXTRACT_SEARCH = r"""() => {
    const sort = document.querySelector('select[name="rankBy"]');
    const form = sort?.form;
    const stock = form?.querySelector('input[name="inStock"]')?.value;
    const stockControl = [...document.querySelectorAll('.display_option li')]
        .find(x => x.textContent.includes('品切れ:'));
    const links = stockControl ? [...stockControl.querySelectorAll('a')].map(a => a.textContent.trim()) : [];
    const cards = [...document.querySelectorAll('#search_result .item_box > .item')];
    return {
        url: location.href, sort: sort?.value, stock,
        query: form?.querySelector('input[name="search_word"]')?.value,
        stockOff: !!stockControl && links.includes('ON') && !links.includes('OFF'),
        hit: document.querySelector('#search_header .hit')?.textContent.trim(),
        hasRoot: !!document.querySelector('#search_result'),
        items: cards.map(card => {
            const price = card.querySelector('.item_price');
            const paragraphs = price ? [...price.querySelectorAll(':scope > p')] : [];
            return {
                url: card.querySelector('.title a')?.href,
                title: card.querySelector('.product-name')?.textContent.trim(),
                image: card.querySelector('.photo_box img')?.src || '',
                prices: paragraphs.map(p => {
                    const clone = p.cloneNode(true);
                    clone.querySelectorAll('del,s,strike,[style*="line-through"]').forEach(x => x.remove());
                    return clone.textContent.replace(/\s+/g, ' ').trim();
                }),
                soldOut: paragraphs.some(p => /^品切れ\s*$/.test(p.textContent.trim())),
                marketplace: !!price?.querySelector('.makeplaTit'),
                reservation: /予約/.test(price?.textContent || ''),
                timeSale: /タイムセール/.test(price?.textContent || ''),
            };
        }),
    };
}"""


def parse_surugaya_results(data: dict, keyword: str) -> list[SurugayaItem]:
    parsed = urlparse(data.get("url", ""))
    params = parse_qs(parsed.query)
    if (parsed.scheme != "https" or parsed.hostname != HOST or parsed.path != "/search"
            or params.get("search_word") != [keyword] or params.get("inStock") != ["Off"]
            or params.get("rankBy") != [NEWEST]):
        raise SourceUnavailable("Suruga-ya search URL was not confirmed")
    hit = re.fullmatch(r"該当件数[:：]\s*([\d,]+)件中\s*([\d,]+)\s*-\s*([\d,]+)件", data.get("hit") or "")
    if not hit or not isinstance(data.get("items"), list):
        raise SourceUnavailable("Suruga-ya result count was not confirmed")
    total, first, last = (int(value.replace(",", "")) for value in hit.groups())
    # The genuine empty page omits the result container and sorting controls.
    if total == first == last == 0 and not data["items"]:
        return []
    if (data.get("sort") != NEWEST or data.get("stock") != "Off" or data.get("stockOff") is not True
            or data.get("query") != keyword or not data.get("hasRoot") or not 0 < first <= last <= total
            or len(data["items"]) != last - first + 1):
        raise SourceUnavailable("Suruga-ya newest and available filters were not confirmed")
    unique = {}
    for raw in data["items"]:
        item_url = urlparse(raw.get("url") or "")
        match = re.fullmatch(r"/product/(?:detail|other)/([A-Za-z0-9]+)", item_url.path)
        if item_url.scheme != "https" or item_url.hostname != HOST or not match or not raw.get("title"):
            raise SourceUnavailable("Incomplete Suruga-ya product")
        branch = parse_qs(item_url.query).get("branch_number", ["0000"])
        if len(branch) != 1 or not re.fullmatch(r"\d{4}", branch[0]):
            raise SourceUnavailable("Suruga-ya condition branch was not confirmed")
        if raw.get("soldOut") or raw.get("reservation"):
            continue
        if not item_url.path.startswith("/product/detail/"):
            raise SourceUnavailable("Suruga-ya direct product link was not confirmed")
        # A time-sale layout must be supported explicitly before quoting it.
        if raw.get("timeSale"):
            raise SourceUnavailable("Suruga-ya time-sale price needs verification")
        offers = []
        for text in raw.get("prices", []):
            if re.match(r"^(中古|新品)[:：]", text):
                offer = re.fullmatch(r"(中古|新品)[:：]\s*[￥¥]([\d,]+)(?:\s*[～〜~]\s*[￥¥]([\d,]+))?\s*税込", text)
                if not offer:
                    raise SourceUnavailable("Suruga-ya offer price was not confirmed")
                condition, low, high = offer.groups()
                low, high = int(low.replace(",", "")), int((high or low).replace(",", ""))
                if not 0 < low <= high:
                    raise SourceUnavailable("Invalid Suruga-ya price range")
                offers.append((condition, low, high))
        if not offers and not raw.get("marketplace"):
            raise SourceUnavailable("Suruga-ya direct offer was not confirmed")
        # Marketplace-only cards have no verified individual seller/condition offer.
        for condition, low, high in offers:
            identity = f"surugaya:{match[1]}:{branch[0]}:{'used' if condition == '中古' else 'new'}"
            image = raw.get("image") or ""
            canonical = f"https://{HOST}/product/detail/{match[1]}"
            if branch[0] != "0000":
                canonical += "?" + urlencode({"branch_number": branch[0]})
            note = f"駿河屋本店・{condition}・含稅，運費及手續費另計"
            if high != low:
                note += "；多種品況起價，請開啟商品頁核對"
            unique[identity] = SurugayaItem(
                id_=identity, name=raw["title"], real_price=low,
                thumbnails=[image] if image.startswith("https://") else [],
                url=canonical, price_note=note, price_from=high != low,
            )
    return list(unique.values())


class SurugayaSearch:
    """Reuse a dedicated browser profile; never use the user's personal profile."""

    def __init__(self):
        self.playwright = None
        self.context = None
        self.page = None
        self.cache = {}
        self.image_cache = {}
        self.unavailable = False
        self.challenge_clicked = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        try:
            if self.context:
                await self.context.close()
        finally:
            if self.playwright:
                await self.playwright.stop()

    async def prepare_item(self, item: SurugayaItem):
        """Resolve the actual public image redirect, only for a new notification."""
        resolved = []
        for image in item.thumbnails[:1]:
            if image in self.image_cache:
                resolved = self.image_cache[image]
                break
            parsed = urlparse(image)
            if parsed.scheme != "https" or parsed.hostname != HOST or parsed.path != "/database/photo.php":
                continue
            response = None
            try:
                response = await self.context.request.get(image, timeout=15000, max_redirects=5)
                final = urlparse(response.url)
                if (response.status == 200 and final.scheme == "https" and final.hostname == "cdn.suruga-ya.jp"
                        and response.headers.get("content-type", "").startswith("image/")):
                    resolved = [response.url]
                    self.image_cache[image] = resolved
            except Exception:
                pass  # A missing image must not suppress the product name and link.
            finally:
                if response:
                    await response.dispose()
        item.thumbnails = list(resolved)

    async def search(self, keyword: str) -> list[SurugayaItem]:
        if self.unavailable:
            raise SourceUnavailable("Suruga-ya browser unavailable for this run")
        if keyword in self.cache:
            return list(self.cache[keyword])
        try:
            if self.context is None:
                from patchright.async_api import async_playwright
                self.playwright = await async_playwright().start()
                state = Path(os.environ.get("MONITOR_STATE_PATH", Path(__file__).parent / ".state/seen.enc"))
                profile = state.parent / "surugaya-browser"
                profile.mkdir(parents=True, exist_ok=True)
                channel = os.environ.get("SURUGAYA_BROWSER_CHANNEL", "").strip() or ("msedge" if os.name == "nt" else "chrome")
                self.context = await self.playwright.chromium.launch_persistent_context(
                    str(profile.resolve()), channel=channel, headless=False, no_viewport=True,
                )
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await self.page.goto(surugaya_search_url(keyword), wait_until="domcontentloaded", timeout=30000)
            from patchright.async_api import TimeoutError as BrowserTimeout
            try:
                await self.page.locator('#search_header .hit').wait_for(timeout=10000)
            except BrowserTimeout:
                # Try the actual visible verification control once, unattended.
                # Clicking it does not imply that the site has accepted the browser.
                for frame in self.page.frames:
                    if urlparse(frame.url).hostname != "challenges.cloudflare.com":
                        continue
                    try:
                        checkbox = frame.get_by_role("checkbox")
                        if not await checkbox.count():
                            checkbox = frame.locator('input[type="checkbox"]')
                        if await checkbox.count() and await checkbox.first.is_visible():
                            await checkbox.first.click(timeout=3000)
                            self.challenge_clicked = True
                            break
                    except Exception:
                        continue
                await self.page.locator('#search_header .hit').wait_for(timeout=15000)
            results = parse_surugaya_results(await self.page.evaluate(EXTRACT_SEARCH), keyword)
            self.cache[keyword] = results
            return list(results)
        except SourceUnavailable:
            raise
        except Exception as error:
            # Do not repeat a browser/challenge failure for every private keyword.
            self.unavailable = True
            raise SourceUnavailable("Suruga-ya browser request failed") from error
