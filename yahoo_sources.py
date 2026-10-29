"""Public Yahoo search pages; read server state without a login or AI service."""
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import quote, urlencode, urlparse

import httpx

from market_sources import SourceUnavailable

FLEAMARKET = "yahoo_fleamarket"
AUCTIONS = "yahoo_auctions"


@dataclass
class YahooItem:
    id_: str
    name: str
    real_price: int
    thumbnails: list[str]
    url: str
    source: str
    currency: str = "JPY"
    item_type: str = "ITEM_TYPE_YAHOO"
    auction: object = None


class _NextData(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.collecting = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.collecting = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.collecting = False

    def handle_data(self, value):
        if self.collecting:
            self.parts.append(value)


def next_data(html: str) -> dict:
    parser = _NextData()
    parser.feed(html)
    try:
        value = json.loads("".join(parser.parts))
        if isinstance(value, dict):
            return value
    except (ValueError, TypeError):
        pass
    raise SourceUnavailable("Yahoo search state was not confirmed")


def yahoo_search_url(source: str, keyword: str) -> str:
    if source == FLEAMARKET:
        # The native selling-only checkbox produces open=1 (status=open is ignored).
        return "https://paypayfleamarket.yahoo.co.jp/search/" + quote(keyword, safe="") + "?" + urlencode({
            "sort": "openTime", "order": "desc", "open": "1",
        })
    if source == AUCTIONS:
        return "https://auctions.yahoo.co.jp/search/search?" + urlencode({
            "p": keyword, "s1": "new", "o1": "d", "b": "1", "n": "50",
        })
    raise ValueError("Unsupported Yahoo source")


def _time(value) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is not None:
            return result
    except (ValueError, TypeError, AttributeError):
        pass
    raise SourceUnavailable("Yahoo listing time was not confirmed")


def _price(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or int(value) != value:
        raise SourceUnavailable("Yahoo listing price was not confirmed")
    return int(value)


def _item(raw, source: str, *, now: datetime) -> YahooItem | None:
    if not isinstance(raw, dict):
        raise SourceUnavailable("Incomplete Yahoo listing")
    if source == FLEAMARKET:
        status = raw.get("itemStatus")
        if status in ("SOLD", "SOLD_OUT", "CLOSED", "DELETED"):
            return None
        if status != "OPEN":
            raise SourceUnavailable("Yahoo sale status was not confirmed")
        identity, image = raw.get("id"), raw.get("thumbnailImageUrl")
        auction = None
    else:
        if raw.get("endTimeStatus") in ("END", "ENDED", "CLOSED", "CANCELLED", "DELETED"):
            return None
        # endTimeStatus is also a display hint (e.g. TODAY), not a sale-status enum.
        remaining = raw.get("leftTime")
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining):
            raise SourceUnavailable("Yahoo auction status was not confirmed")
        if remaining <= 0:
            return None
        identity, image = raw.get("auctionId"), raw.get("imageUrl")
        mirrored = raw.get("isFleamarketItem")
        fixed = raw.get("isFixedPrice")
        if not isinstance(mirrored, bool) or not isinstance(fixed, bool):
            raise SourceUnavailable("Yahoo listing type was not confirmed")
        if mirrored:
            source = FLEAMARKET
            auction = None
        elif fixed:
            auction = None
        else:
            bids = raw.get("bidCount")
            if isinstance(bids, bool) or not isinstance(bids, int) or bids < 0:
                raise SourceUnavailable("Yahoo bid count was not confirmed")
            auction = SimpleNamespace(bid_deadline=_time(raw.get("endTime")),
                                      highest_bid=_price(raw.get("price")), total_bid=bids)
    # Explicit timestamps prevent an expired cached listing from being notified.
    if _time(raw.get("endTime")) <= now:
        return None
    _time(raw.get("openTime") if "openTime" in raw else raw.get("startTime"))
    title = raw.get("title")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,79}", identity) or not isinstance(title, str) or not title.strip():
        raise SourceUnavailable("Incomplete Yahoo listing")
    price = _price(raw.get("price"))  # Displayed current price, including seller tax; never buyNowPrice.
    base = "https://paypayfleamarket.yahoo.co.jp/item/" if source == FLEAMARKET else "https://auctions.yahoo.co.jp/jp/auction/"
    images = [image] if isinstance(image, str) and image.startswith("https://") else []
    return YahooItem(id_=source + ":" + identity, name=title.strip(), real_price=price,
                     thumbnails=images, url=base + identity, source=source, auction=auction)


def parse_yahoo_results(source: str, data: dict, keyword: str, *, now=None) -> list[YahooItem]:
    now = now or datetime.now(timezone.utc)
    try:
        query = data["query"]
        state = data["props"]["initialState"]
        if source == FLEAMARKET:
            search = state["searchState"]["search"]
            params = search["searchParams"]
            if query.get("query") != keyword or params.get("query") != keyword or params.get("sort") != "openTime" or params.get("order") != "DESC" or params.get("itemStatus") != "open" or search.get("isLoading") is not False:
                raise SourceUnavailable("Yahoo Fleamarket filters were not confirmed")
            listing = search["result"]
        elif source == AUCTIONS:
            listing = state["search"]["items"]["listing"]
            meta = listing["metadata"]
            if query.get("p") != keyword or query.get("s1") != "new" or query.get("o1") != "d" or meta.get("sort") != "-FIRST_START_TIME" or meta.get("request", {}).get("query") != keyword or listing.get("isFetching") is not False:
                raise SourceUnavailable("Yahoo Auctions filters were not confirmed")
        else:
            raise ValueError("Unsupported Yahoo source")
        rows, count = listing["items"], listing["totalResultsAvailable"]
        if not isinstance(rows, list) or isinstance(count, bool) or not isinstance(count, int) or count < 0 or (not rows and count != 0) or (rows and count == 0):
            raise SourceUnavailable("Yahoo search results were not confirmed")
        unique = {}
        for raw in rows:
            item = _item(raw, source, now=now)
            if item is not None:
                unique[item.id_] = item
        return list(unique.values())
    except (KeyError, TypeError, AttributeError) as error:
        raise SourceUnavailable("Yahoo search schema was not confirmed") from error


class YahooSearch:
    def __init__(self, source: str):
        if source not in (FLEAMARKET, AUCTIONS):
            raise ValueError("Unsupported Yahoo source")
        self.source = source
        self.client = None
        self.cache = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        if self.client:
            await self.client.aclose()

    async def search(self, keyword: str) -> list[YahooItem]:
        if keyword in self.cache:
            return list(self.cache[keyword])
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=25, follow_redirects=True)
        try:
            response = await self.client.get(yahoo_search_url(self.source, keyword))
            host = "paypayfleamarket.yahoo.co.jp" if self.source == FLEAMARKET else "auctions.yahoo.co.jp"
            if response.status_code not in (200, 404) or urlparse(str(response.url)).hostname != host:
                raise SourceUnavailable("Yahoo search request was rejected")
            results = parse_yahoo_results(self.source, next_data(response.text), keyword)
            # Yahoo uses soft 404s for some completed searches, including broadened
            # results. Accept only the same verified state/filters as HTTP 200;
            # the monitor's private title rules still decide which items match.
            self.cache[keyword] = results
            return list(results)
        except httpx.HTTPError as error:
            raise SourceUnavailable("Yahoo search request failed") from error
