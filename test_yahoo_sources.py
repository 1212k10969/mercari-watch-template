import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx

import mercari_watch as app
from market_sources import SourceUnavailable
from yahoo_sources import AUCTIONS, FLEAMARKET, YahooSearch, next_data, parse_yahoo_results, yahoo_search_url

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def row(source, **changes):
    result = {"title": "Example product", "price": 1200, "endTime": "2026-01-03T12:00:00+09:00"}
    if source == FLEAMARKET:
        result.update(id="z100", itemStatus="OPEN", openTime="2026-01-01T09:00:00+09:00",
                      thumbnailImageUrl="https://example.org/photo.jpg")
    else:
        result.update(auctionId="a100", endTimeStatus="TODAY", startTime="2026-01-01T09:00:00+09:00",
                      leftTime=1000, isFleamarketItem=False, isFixedPrice=False,
                      bidCount=3, buyNowPrice=9999, imageUrl="https://example.org/photo.jpg")
    return {**result, **changes}


def data(source, rows):
    listing = {"items": rows, "totalResultsAvailable": len(rows)}
    if source == FLEAMARKET:
        return {"query": {"query": "example"}, "props": {"initialState": {"searchState": {"search": {
            "isLoading": False, "searchParams": {"query": "example", "sort": "openTime", "order": "DESC", "itemStatus": "open"}, "result": listing,
        }}}}}
    listing.update(isFetching=False, metadata={"sort": "-FIRST_START_TIME", "request": {"query": "example"}})
    return {"query": {"p": "example", "s1": "new", "o1": "d"}, "props": {"initialState": {"search": {"items": {"listing": listing}}}}}


class YahooTests(unittest.TestCase):
    def parse(self, source, rows):
        return parse_yahoo_results(source, data(source, rows), "example", now=NOW)

    def test_native_filter_urls_and_server_filters(self):
        self.assertIn("open=1", yahoo_search_url(FLEAMARKET, "a/b"))
        self.assertIn("a%2Fb", yahoo_search_url(FLEAMARKET, "a/b"))
        self.assertIn("s1=new", yahoo_search_url(AUCTIONS, "example"))
        for source in (FLEAMARKET, AUCTIONS):
            payload = data(source, [row(source)])
            if source == FLEAMARKET:
                payload["props"]["initialState"]["searchState"]["search"]["searchParams"].pop("itemStatus")
            else:
                payload["props"]["initialState"]["search"]["items"]["listing"]["metadata"]["sort"] = "SCORE"
            with self.assertRaises(SourceUnavailable):
                parse_yahoo_results(source, payload, "example", now=NOW)

    def test_items_have_name_image_link_and_current_bid(self):
        auction = self.parse(AUCTIONS, [row(AUCTIONS)])[0]
        self.assertEqual(auction.real_price, 1200)
        self.assertEqual(auction.auction.highest_bid, 1200)
        self.assertEqual(auction.auction.total_bid, 3)
        self.assertIsNotNone(auction.auction.bid_deadline.tzinfo)
        self.assertEqual(auction.id_, "yahoo_auctions:a100")
        self.assertEqual(auction.thumbnails, ["https://example.org/photo.jpg"])
        rec = app.to_record(auction, {"id": "sample", "keyword": "example"})
        self.assertEqual(list(rec["links"]), [AUCTIONS])
        self.assertEqual(app.build_discord_messages([rec])[0]["embeds"][0]["fields"][0]["name"], "拍賣目前價格")
        self.assertEqual(self.parse(AUCTIONS, [row(AUCTIONS, auctionId="1234567890")])[0].id_, "yahoo_auctions:1234567890")

    def test_mirrored_listing_has_one_identity_and_no_auction(self):
        first = self.parse(FLEAMARKET, [row(FLEAMARKET)])[0]
        mirrored = self.parse(AUCTIONS, [row(AUCTIONS, auctionId="z100", isFleamarketItem=True, isFixedPrice=True)])[0]
        self.assertEqual(app.item_identity(first), app.item_identity(mirrored))
        self.assertEqual(first.url, mirrored.url)
        self.assertFalse(app.is_auction(mirrored))
        self.assertFalse(app.is_auction(self.parse(AUCTIONS, [row(AUCTIONS, isFixedPrice=True)])[0]))

    def test_sold_ended_and_expired_items_are_excluded(self):
        self.assertEqual(self.parse(FLEAMARKET, [row(FLEAMARKET, itemStatus="SOLD")]), [])
        self.assertEqual(self.parse(AUCTIONS, [row(AUCTIONS, endTimeStatus="ENDED")]), [])
        self.assertEqual(self.parse(AUCTIONS, [row(AUCTIONS, leftTime=0)]), [])
        self.assertEqual(self.parse(FLEAMARKET, [row(FLEAMARKET, endTime="2025-01-01T09:00:00Z")]), [])

    def test_explicit_empty_differs_from_missing_or_challenged_results(self):
        for source in (FLEAMARKET, AUCTIONS):
            self.assertEqual(self.parse(source, []), [])
            for broken in ({}, {"query": {}}, {"props": {"initialState": {}}}):
                with self.assertRaises(SourceUnavailable):
                    parse_yahoo_results(source, broken, "example", now=NOW)
        with self.assertRaises(SourceUnavailable):
            next_data("<html><title>Just a moment...</title></html>")
        self.assertEqual(next_data('<script id="__NEXT_DATA__" type="application/json">{"ok":true}</script>'), {"ok": True})

    def test_bad_prices_dates_and_partial_results_fail_closed(self):
        for source in (FLEAMARKET, AUCTIONS):
            for changes in ({"price": True}, {"price": float("nan")}, {"price": None}, {"endTime": "2026-01-03T12:00:00"}, {"title": ""}):
                with self.assertRaises(SourceUnavailable):
                    self.parse(source, [row(source), row(source, **changes)])
        payload = data(FLEAMARKET, [])
        payload["props"]["initialState"]["searchState"]["search"]["result"]["totalResultsAvailable"] = 2
        with self.assertRaises(SourceUnavailable):
            parse_yahoo_results(FLEAMARKET, payload, "example", now=NOW)

    def test_blocked_http_does_not_become_empty_or_get_retried(self):
        async def check():
            async with YahooSearch(FLEAMARKET) as client:
                client.client = AsyncMock()
                client.client.get.return_value = httpx.Response(403, request=httpx.Request("GET", yahoo_search_url(FLEAMARKET, "example")))
                with self.assertRaises(SourceUnavailable):
                    await client.search("example")
                self.assertEqual(client.client.get.await_count, 1)
        asyncio.run(check())

    def test_soft_404_requires_verified_search_state(self):
        async def check():
            import json
            payload = data(FLEAMARKET, [row(FLEAMARKET, endTime="2099-01-01T00:00:00Z")])
            html = '<script id="__NEXT_DATA__">' + json.dumps(payload) + '</script>'
            async with YahooSearch(FLEAMARKET) as client:
                client.client = AsyncMock()
                client.client.get.return_value = httpx.Response(404, text=html, request=httpx.Request("GET", yahoo_search_url(FLEAMARKET, "example")))
                self.assertEqual(len(await client.search("example")), 1)
                self.assertEqual(len(await client.search("example")), 1)
                self.assertEqual(client.client.get.await_count, 1)
            async with YahooSearch(FLEAMARKET) as client:
                client.client = AsyncMock()
                client.client.get.return_value = httpx.Response(404, text="Not found", request=httpx.Request("GET", yahoo_search_url(FLEAMARKET, "example")))
                with self.assertRaises(SourceUnavailable):
                    await client.search("example")
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
