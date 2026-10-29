import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import mercari_watch as app
from market_sources import SourceUnavailable
from surugaya_source import NEWEST, SurugayaSearch, parse_surugaya_results, surugaya_search_url


def card(**changes):
    return {"url": "https://www.suruga-ya.jp/product/detail/EXAMPLE100",
            "title": "Example product", "image": "https://example.org/image.jpg",
            "prices": ["中古：￥1,200 税込", "定価：￥9,999", "1,500円以上は送料無料"],
            "soldOut": False, "reservation": False, "timeSale": False, "marketplace": True,
            **changes}


def data(rows):
    return {"url": surugaya_search_url("example"), "sort": NEWEST, "stock": "Off",
            "stockOff": True, "query": "example", "hasRoot": True,
            "hit": f"該当件数:{len(rows)}件中 1-{len(rows)}件", "items": rows}


class SurugayaTests(unittest.TestCase):
    def test_latest_and_sold_out_exclusion_must_be_verified(self):
        self.assertIn("inStock=Off", surugaya_search_url("example"))
        for key, value in [("sort", "relavancy(int)"), ("stock", "On"),
                           ("stockOff", False), ("query", "other"), ("hasRoot", False)]:
            with self.subTest(key=key), self.assertRaises(SourceUnavailable):
                parse_surugaya_results({**data([card()]), key: value}, "example")
        with self.assertRaises(SourceUnavailable):
            parse_surugaya_results({**data([card()]), "url": surugaya_search_url("other")}, "example")

    def test_own_price_does_not_use_retail_marketplace_or_shipping(self):
        item = parse_surugaya_results(data([card()]), "example")[0]
        self.assertEqual(item.real_price, 1200)
        self.assertEqual(item.thumbnails, ["https://example.org/image.jpg"])
        self.assertEqual(item.id_, "surugaya:EXAMPLE100:0000:used")
        self.assertEqual(item.url, card()["url"])
        rec = app.to_record(item, {"id": "sample", "keyword": "example"})
        embed = app.build_discord_messages([rec])[0]["embeds"][0]
        self.assertEqual(embed["image"]["url"], item.thumbnails[0])
        self.assertEqual(embed["url"], item.url)
        self.assertIn("駿河屋本店", embed["description"])

    def test_range_and_condition_branch_are_preserved(self):
        item = parse_surugaya_results(data([card(
            url=card()["url"] + "?branch_number=0010",
            prices=["中古：￥1,200 ～ ￥1,800 税込"],
        )]), "example")[0]
        self.assertIn(":0010:used", item.id_)
        self.assertTrue(item.url.endswith("branch_number=0010"))
        self.assertIn("起價", item.price_note)
        self.assertEqual(app.price_label(app.to_record(item, {"id": "sample", "keyword": "example"})), "¥1,200 起")

    def test_unavailable_and_marketplace_only_goods_are_excluded(self):
        rows = [card(soldOut=True, url="https://www.suruga-ya.jp/product/other/EXAMPLE100"),
                card(reservation=True), card(prices=["定価：￥9,999"])]
        self.assertEqual(parse_surugaya_results(data(rows), "example"), [])

    def test_challenge_or_missing_rows_are_not_empty_results(self):
        empty = {"url": surugaya_search_url("example"), "hit": "該当件数：0件中 0 -0件", "items": []}
        self.assertEqual(parse_surugaya_results(empty, "example"), [])
        for broken in [{}, {"url": empty["url"], "items": []}, {**data([card()]), "items": []}]:
            with self.assertRaises(SourceUnavailable):
                parse_surugaya_results(broken, "example")

    def test_unknown_sale_layout_and_external_domains_fail_closed(self):
        for row in [card(timeSale=True), card(prices=["中古：￥1,200 ￥800 税込"]),
                    card(url="https://www.suruga-ya.com/product/detail/EXAMPLE100"),
                    card(prices=["中古：￥0 税込"]), card(prices=["中古：￥1,800 ～ ￥1,200 税込"])]:
            with self.assertRaises(SourceUnavailable):
                parse_surugaya_results(data([row]), "example")

    def test_direct_condition_and_mirror_links_do_not_duplicate(self):
        rows = [card(), card(), card(prices=["新品：￥1,500 税込"])]
        items = parse_surugaya_results(data(rows), "example")
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0].id_, items[1].id_)

    def test_monitor_routes_through_source_client(self):
        client = AsyncMock()
        client.search.return_value = parse_surugaya_results(data([card()]), "example")
        items = asyncio.run(app.search_new(None, {"id": "sample", "keyword": "example", "source": "surugaya"},
                                          external={"surugaya": client}))
        self.assertEqual(len(items), 1)
        client.search.assert_awaited_once_with("example")

    def test_images_use_verified_public_cdn_instead_of_protected_entry(self):
        for status, url, expected in [
            (200, "https://cdn.suruga-ya.jp/pics_webp/example.webp", True),
            (403, "https://www.suruga-ya.jp/challenge", False),
            (200, "https://other.example/image.webp", False),
        ]:
            with self.subTest(status=status, url=url):
                item = parse_surugaya_results(data([card(
                    image="https://www.suruga-ya.jp/database/photo.php?shinaban=EXAMPLE100&size=m",
                )]), "example")[0]
                response = SimpleNamespace(status=status, url=url, headers={"content-type": "image/webp"}, dispose=AsyncMock())
                request = SimpleNamespace(get=AsyncMock(return_value=response))
                source = SurugayaSearch()
                source.context = SimpleNamespace(request=request)
                asyncio.run(source.prepare_item(item))
                self.assertEqual(item.thumbnails, [url] if expected else [])
                response.dispose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
