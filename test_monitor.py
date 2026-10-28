import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, patch

import httpx
import mercari_watch as app
from market_sources import SourceUnavailable, TaiwanItem, original_item_id, parse_taiwan_results, taiwan_search_url
from urllib.parse import parse_qs, urlparse


def watch(**changes):
    return {"id": "sample", "name": "測試條件", "keyword": "測試",
            "keywords": [], "price_min": None, "price_max": 1000,
            "must_match": [], "must_not_match": [], "enabled": True,
            **changes}


def item(id_="one", **changes):
    return Obj(**{"id_": id_, "name": "測試商品", "real_price": 500,
                  "auction": None, "item_type": "ITEM_TYPE_MERCARI",
                  "thumbnails": [], **changes})


class MonitorTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "LINE_CHANNEL_ACCESS_TOKEN": "not-a-real-token",
            "LINE_USER_ID": "not-a-real-recipient",
            "PYTHON_DOTENV_DISABLED": "1",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        network = patch.object(app.httpx, "post", side_effect=AssertionError("Unexpected HTTP request"))
        network.start()
        self.addCleanup(network.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.watch_path = Path(self.tmp.name) / "watchlist.json"
        self.seen_path = Path(self.tmp.name) / "seen.json"
        self.watch_path.write_text(json.dumps([watch()]))
        for name, value in (("WATCHLIST", self.watch_path),
                            ("SEEN", self.seen_path), ("DELAY_BETWEEN", 0)):
            p = patch.object(app, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_watches(self, values):
        self.watch_path.write_text(json.dumps(values))

    def run_check(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return asyncio.run(app.check_once(**kw))

    def test_shipped_config_is_blank_and_disabled(self):
        values = json.loads((app.BASE / "watchlist.json").read_text())
        self.assertEqual(len(values), 1)
        self.assertIs(values[0]["enabled"], False)
        for field in ("id", "name", "keyword", "review_note"):
            self.assertEqual(values[0][field], "")
        self.assertEqual(values[0]["keywords"], [])

    def test_blank_and_empty_config_never_search_send_or_write(self):
        for values in ([], [{"id": "", "keyword": "", "enabled": False}]):
            self.write_watches(values)
            with patch.object(app, "Mercapi") as client, patch.object(app, "line_push") as send:
                self.assertEqual(self.run_check(), 0)
                client.assert_not_called()
                send.assert_not_called()
                self.assertFalse(self.seen_path.exists())

    def test_invalid_config_rejected_before_network(self):
        bad_values = [{}, [watch(keyword="")], [watch(id="")],
                      [watch(id="_health")], [watch(), watch()],
                      [watch(price_min=1001)], [watch(must_match=["["])],
                      [watch(keywords=[""])], [watch(sources=[])],
                      [watch(sources=["other"])], [watch(sources=["jp", "jp"])],
                      [watch(source_keywords={"tw": ["example"]})],
                      [watch(sources=["tw"], source_keywords={"tw": []})]]
        for values in bad_values:
            self.write_watches(values)
            with patch.object(app, "Mercapi") as client:
                with self.assertRaises((ValueError, TypeError, app.re.error)):
                    self.run_check()
                client.assert_not_called()

    def test_keywords_merge_and_deduplicate(self):
        client = Obj(search=AsyncMock(side_effect=[
            Obj(items=[item()]), Obj(items=[item(), item("two")])]))
        with contextlib.redirect_stdout(io.StringIO()):
            result = asyncio.run(app.search_new(
                client, watch(keywords=["first", "second", "first"])))
        self.assertEqual([x.id_ for x in result], ["one", "two"])
        self.assertEqual(client.search.await_count, 2)
        options = client.search.call_args.kwargs
        self.assertEqual(options["sort_by"], app.SearchRequestData.SortBy.SORT_CREATED_TIME)
        self.assertEqual(options["sort_order"], app.SearchRequestData.SortOrder.ORDER_DESC)
        self.assertEqual(options["status"], [app.SearchRequestData.Status.STATUS_ON_SALE])

    def test_partial_keyword_failure_does_not_return_partial_result(self):
        client = Obj(search=AsyncMock(side_effect=[
            Obj(items=[item()]), RuntimeError("unavailable")]))
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                asyncio.run(app.search_new(client, watch(keywords=["first", "second"])))

    def test_filter_title_and_current_auction_price(self):
        auction = Obj(bid_deadline=datetime.now(timezone.utc), highest_bid=1001)
        self.assertFalse(app.passes_filters(item(auction=auction), watch()))
        auction.highest_bid = 1000
        self.assertTrue(app.passes_filters(item(auction=auction), watch()))
        self.assertFalse(app.passes_filters(item(auction=auction), watch(include_auctions=False)))
        self.assertFalse(app.passes_filters(item(), watch(must_match=["other"])))
        self.assertFalse(app.passes_filters(item(), watch(must_not_match=["商品"])))
        self.assertFalse(app.is_auction(item(auction=Obj(bid_deadline=None))))

    def test_first_run_only_saves_baseline(self):
        with patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(return_value=[item()])), patch.object(app, "notify") as send:
            self.assertEqual(self.run_check(), 0)
            send.assert_not_called()
        self.assertEqual(json.loads(self.seen_path.read_text())["sample"], ["one"])

    def test_notify_failure_keeps_old_state_then_success_advances_it(self):
        self.seen_path.write_text(json.dumps({"sample": ["one"]}))
        with patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(return_value=[item(), item("two")])), patch.object(app, "notify", return_value=False):
            self.assertEqual(self.run_check(), 1)
        self.assertEqual(json.loads(self.seen_path.read_text())["sample"], ["one"])
        with patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(return_value=[item(), item("two")])), patch.object(app, "notify", return_value=True) as send:
            self.assertEqual(self.run_check(), 0)
            self.assertEqual(len(send.call_args.args[0]), 1)
        self.assertEqual(json.loads(self.seen_path.read_text())["sample"], ["two", "one"])

    def test_dry_run_never_notifies_or_writes(self):
        self.seen_path.write_text(json.dumps({"sample": ["one"]}))
        before = self.seen_path.read_bytes()
        with patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(return_value=[item("two")])), patch.object(app, "notify") as send:
            self.assertEqual(self.run_check(dry_run=True), 0)
            send.assert_not_called()
        self.assertEqual(self.seen_path.read_bytes(), before)

    def test_failed_search_keeps_state_and_hides_error_details(self):
        self.seen_path.write_text(json.dumps({"sample": ["one"]}))
        output, error = io.StringIO(), io.StringIO()
        with patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(side_effect=RuntimeError("sensitive-query"))), patch.object(app, "line_push") as send, contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(asyncio.run(app.check_once()), 1)
            send.assert_not_called()
        state = json.loads(self.seen_path.read_text())
        self.assertEqual(state["sample"], ["one"])
        self.assertEqual(state["_health"]["fails"], 1)
        self.assertNotIn("sensitive-query", output.getvalue() + error.getvalue())

    def test_notification_chunks_keep_all_items_without_duplicates(self):
        records = [app.to_record(item(str(i)), watch()) for i in range(26)]
        with patch.object(app, "line_push", return_value=True) as send, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(app.notify(records + [records[0]]))
        messages = send.call_args.args[0]
        self.assertEqual([len(m["contents"]["contents"]) for m in messages], [12, 12, 2])

    def test_logs_hide_product_title_keyword_and_url(self):
        self.seen_path.write_text(json.dumps({"sample": []}))
        product = item(name="private-title")
        output = io.StringIO()
        with patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(return_value=[product])), patch.object(app, "line_push", return_value=True), contextlib.redirect_stdout(output):
            self.assertEqual(asyncio.run(app.check_once()), 0)
        self.assertNotIn("private-title", output.getvalue())
        self.assertNotIn("jp.mercari.com", output.getvalue())

    def test_line_test_does_not_query_or_write_state(self):
        with patch.object(app, "Mercapi") as client, patch.object(app, "line_push", return_value=True) as send:
            self.assertEqual(asyncio.run(app.test_line()), 0)
            client.assert_not_called()
            self.assertEqual(send.call_args.args[0][0]["type"], "text")
            self.assertTrue(send.call_args.args[0][0]["text"].startswith("【測試】"))
        self.assertFalse(self.seen_path.exists())

    def test_line_missing_secrets_does_not_send(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(app.httpx, "post") as send, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(app.line_push([{"type": "text", "text": "test"}]))
            send.assert_not_called()

    def test_line_error_does_not_log_response_body(self):
        env = {"LINE_CHANNEL_ACCESS_TOKEN": "not-a-real-token", "LINE_USER_ID": "not-a-real-recipient"}
        response = httpx.Response(401, text="sensitive-response")
        error = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch.object(app.httpx, "post", return_value=response) as send, contextlib.redirect_stderr(error):
            self.assertFalse(app.line_push([{"type": "text", "text": "test"}]))
        self.assertEqual(send.call_args.args[0], app.LINE_PUSH_URL)
        self.assertNotIn("sensitive-response", error.getvalue())

    def test_discord_only_sends_product_and_advances_state(self):
        self.seen_path.write_text(json.dumps({"sample": ["one"]}))
        env = {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/not-a-real-token"}
        with patch.dict(os.environ, env, clear=True), patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(return_value=[item("two", thumbnails=["https://example.com/product.jpg"])])), patch.object(app, "line_push") as line, patch.object(app.httpx, "post", return_value=httpx.Response(200)) as send:
            self.assertEqual(self.run_check(), 0)
        line.assert_not_called()
        self.assertEqual(send.call_args.args[0].params["wait"], "true")
        payload = send.call_args.kwargs["json"]
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(payload["embeds"][0]["title"], "測試商品")
        self.assertEqual(payload["embeds"][0]["fields"][0]["value"], "¥500")
        self.assertEqual(payload["embeds"][0]["image"]["url"], "https://example.com/product.jpg")
        self.assertIn("https://jp.mercari.com/item/two", payload["embeds"][0]["description"])
        self.assertEqual(json.loads(self.seen_path.read_text())["sample"], ["two", "one"])

    def test_both_channels_attempted_and_failure_preserves_state(self):
        env = {"LINE_CHANNEL_ACCESS_TOKEN": "fake", "LINE_USER_ID": "fake",
               "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/fake"}
        for line_ok, discord_ok in ((False, True), (True, False), (True, True)):
            with self.subTest(line=line_ok, discord=discord_ok):
                self.seen_path.write_text(json.dumps({"sample": ["one"]}))
                with patch.dict(os.environ, env, clear=True), patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(return_value=[item("two")])), patch.object(app, "line_push", return_value=line_ok) as line, patch.object(app, "discord_push", return_value=discord_ok) as discord:
                    self.assertEqual(self.run_check(), 0 if line_ok and discord_ok else 1)
                line.assert_called_once()
                discord.assert_called_once()
                expected = ["two", "one"] if line_ok and discord_ok else ["one"]
                self.assertEqual(json.loads(self.seen_path.read_text())["sample"], expected)

    def test_no_channels_does_not_mark_new_items_notified(self):
        self.seen_path.write_text(json.dumps({"sample": ["one"]}))
        with patch.dict(os.environ, {}, clear=True), patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(return_value=[item("two")])), patch.object(app, "line_push") as line, patch.object(app, "discord_push") as discord:
            self.assertEqual(self.run_check(), 1)
        line.assert_not_called()
        discord.assert_not_called()
        self.assertEqual(json.loads(self.seen_path.read_text())["sample"], ["one"])

    def test_partial_line_config_still_attempts_discord(self):
        env = {"LINE_CHANNEL_ACCESS_TOKEN": "fake",
               "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/fake"}
        record = app.to_record(item(), watch())
        with patch.dict(os.environ, env, clear=True), patch.object(app, "discord_push", return_value=True) as discord, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(app.notify([record]))
        discord.assert_called_once()

    def test_discord_chunks_keep_items_and_respect_embed_limits(self):
        for note in ("", "人工核對" * 2000):
            with self.subTest(long_note=bool(note)):
                records = [app.to_record(item(str(i), name="商品" * 150), watch(review_note=note)) for i in range(26)]
                with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "fake"}, clear=True), patch.object(app, "discord_push", return_value=True) as send, contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(app.notify(records + [records[0]]))
                messages = [call.args[0] for call in send.call_args_list]
                embeds = [embed for message in messages for embed in message["embeds"]]
                self.assertEqual([embed["url"] for embed in embeds], [record["url"] for record in records])
                for message in messages:
                    self.assertLessEqual(len(message["embeds"]), 10)
                    size = 0
                    for embed in message["embeds"]:
                        self.assertLessEqual(len(embed["title"]), 256)
                        self.assertLessEqual(len(embed.get("description", "")), 4096)
                        size += len(embed["title"]) + len(embed.get("description", ""))
                        size += sum(len(field["name"]) + len(field["value"]) for field in embed["fields"])
                    self.assertLessEqual(size, 6000)

    def test_discord_auction_card_includes_current_price_and_details(self):
        auction = Obj(bid_deadline=datetime(2026, 1, 1, 12, tzinfo=timezone.utc), highest_bid=700, total_bid=2)
        record = app.to_record(item(auction=auction, thumbnails=["https://example.com/item.jpg"]), watch(review_note="請核對配件"))
        embed = app.build_discord_messages([record])[0]["embeds"][0]
        self.assertEqual(embed["image"]["url"], "https://example.com/item.jpg")
        self.assertIn("請核對配件", embed["description"])
        self.assertEqual([field["value"] for field in embed["fields"]], ["¥700", "01/01 20:00", "2"])

    def test_discord_test_does_not_query_or_write_state(self):
        with patch.object(app, "Mercapi") as client, patch.object(app, "discord_push", return_value=True) as send:
            self.assertEqual(asyncio.run(app.test_discord()), 0)
        client.assert_not_called()
        self.assertIn("content", send.call_args.args[0])
        self.assertTrue(send.call_args.args[0]["content"].startswith("【測試】"))
        self.assertFalse(self.seen_path.exists())

    def test_both_test_flags_skip_monitor_even_if_line_fails(self):
        with patch.object(app.sys, "argv", ["mercari_watch.py", "--once", "--test-line", "--test-discord"]), patch.object(app, "test_line", AsyncMock(return_value=1)) as line, patch.object(app, "test_discord", AsyncMock(return_value=0)) as discord, patch.object(app, "check_once", AsyncMock()) as check:
            with self.assertRaises(SystemExit) as result:
                app.main()
        self.assertEqual(result.exception.code, 1)
        line.assert_awaited_once()
        discord.assert_awaited_once()
        check.assert_not_awaited()
        self.assertFalse(self.seen_path.exists())

    def test_discord_missing_url_does_not_send(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(app.httpx, "post") as send, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(app.discord_push({"content": "test"}))
        send.assert_not_called()

    def test_discord_errors_do_not_log_url_token_or_response(self):
        secret = "not-a-real-webhook-secret"
        env = {"DISCORD_WEBHOOK_URL": f"https://discord.com/api/webhooks/123/{secret}"}
        for failure in (httpx.Response(404, text="sensitive-response"), httpx.ConnectError(f"sensitive-response {secret}")):
            with self.subTest(failure=type(failure).__name__):
                output, error = io.StringIO(), io.StringIO()
                options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.dict(os.environ, env, clear=True), patch.object(app.httpx, "post", **options) as send, contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                    self.assertFalse(app.discord_push({"content": "test"}))
                self.assertEqual(send.call_count, 1)
                for private in (secret, env["DISCORD_WEBHOOK_URL"], "sensitive-response"):
                    self.assertNotIn(private, output.getvalue() + error.getvalue())

    def test_discord_rate_limits_use_server_delay_with_bounded_retries(self):
        env = {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/fake"}
        cases = [
            ([httpx.Response(429, json={"retry_after": 0.25}), httpx.Response(200)], True, [0.25]),
            ([httpx.Response(429, headers={"Retry-After": "0.5"}), httpx.Response(204)], True, [0.5]),
            ([httpx.Response(429, json={"retry_after": 1000})], False, []),
            ([httpx.Response(429, json={"retry_after": "NaN"})], False, []),
            ([httpx.Response(429, json={"retry_after": 0})] * 3, False, [0, 0]),
        ]
        for responses, success, delays in cases:
            with self.subTest(success=success, delays=delays), patch.dict(os.environ, env, clear=True), patch.object(app.httpx, "post", side_effect=responses) as send, patch.object(app.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app.discord_push({"content": "test"}), success)
                self.assertEqual(send.call_count, len(responses))
                self.assertEqual([call.args[0] for call in sleep.call_args_list], delays)

    def test_health_alerts_go_to_both_configured_channels(self):
        with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "fake"}), patch.object(app, "line_push", return_value=True) as line, patch.object(app, "discord_push", return_value=True) as discord, contextlib.redirect_stdout(io.StringIO()):
            app.alert("監控已恢復")
        self.assertEqual(line.call_args.args[0][0]["text"], "監控已恢復")
        self.assertEqual(discord.call_args.args[0]["content"], "監控已恢復")

    def test_private_environment_rules_override_public_template(self):
        self.write_watches([])
        private = [watch(keyword="private-query", price_max=None, sources=["jp", "tw"], source_keywords={"tw": ["translated-query"]})]
        with patch.dict(os.environ, {"WATCHLIST_JSON": json.dumps(private)}):
            self.assertEqual(app.load_watchlist(), private)
        self.assertEqual(json.loads(self.watch_path.read_text()), [])

    def test_encrypted_state_roundtrip_and_wrong_key_fail_closed(self):
        state = {"sample": ["private-product-id"], "_known": ["private-product-id"]}
        key = app.Fernet.generate_key().decode()
        with patch.object(app, "SEEN", self.seen_path.with_suffix(".enc")), patch.dict(os.environ, {"MONITOR_STATE_KEY": key}):
            app.save_state(state)
            self.assertNotIn(b"private-product-id", app.SEEN.read_bytes())
            self.assertEqual(app.load_state(), state)
            with patch.dict(os.environ, {"MONITOR_STATE_KEY": app.Fernet.generate_key().decode()}):
                with self.assertRaises(ValueError):
                    app.load_state()

    def test_persistent_monitor_requires_key_before_network(self):
        with patch.object(app, "SEEN", self.seen_path.with_suffix(".enc")), patch.object(app, "Mercapi") as client:
            with self.assertRaises(ValueError):
                self.run_check()
        client.assert_not_called()

    def test_taiwan_filters_and_original_identity(self):
        query = parse_qs(urlparse(taiwan_search_url("example")).query)
        self.assertEqual(query["status"], ["in-stock"])
        self.assertEqual(query["availability"], ["1"])
        self.assertEqual(query["sort"], ["1"])
        raw = {"url": "https://tw.mercari.com/zh-hant/items/00000000-0000-0000-0000-000000000001",
               "title": "商品A", "price": 100,
               "image": "https://static.mercdn.net/thumb/item/webp/m123456_1.jpg?1"}
        data = {"status": "in-stock", "sort": "新商品", "items": [raw, raw]}
        result = parse_taiwan_results(data)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id_, "m123456")
        self.assertEqual(app.price_label(app.to_record(result[0], watch())), "NT$100")
        self.assertEqual(original_item_id("https://evilmercdn.net/m123456_1.jpg", "fallback"), "tw:fallback")
        for invalid in ({**data, "status": "all"}, {**data, "sort": "推薦排序"}, {**data, "items": []}):
            with self.assertRaises(SourceUnavailable):
                parse_taiwan_results(invalid)

    def test_cross_source_duplicates_merge_links_and_stay_known(self):
        self.write_watches([watch(price_max=None, sources=["jp", "tw"])])
        self.seen_path.write_text(json.dumps({"sample": ["old"], "_known": ["old"], "_baselines": ["sample:jp", "sample:tw"]}))
        tw_one = TaiwanItem("m123", "商品A", 100, ["https://example.com/photo.jpg"], "https://tw.mercari.com/zh-hant/items/one")
        tw_two = TaiwanItem("m456", "商品B", 200, [], "https://tw.mercari.com/zh-hant/items/two")
        with patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(side_effect=[[item("m123")], [tw_one, tw_two]])), patch.object(app, "notify", return_value=True) as send:
            self.assertEqual(self.run_check(), 0)
        records = send.call_args.args[0]
        self.assertEqual(len(records), 2)
        self.assertEqual(set(records[0]["links"]), {"jp", "tw"})
        with patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(side_effect=[[item("m123"), item("m456")], [tw_one]])), patch.object(app, "notify") as send:
            self.assertEqual(self.run_check(), 0)
        send.assert_not_called()

    def test_healthy_source_continues_when_other_source_fails(self):
        self.write_watches([watch(price_max=None, sources=["jp", "tw"])])
        self.seen_path.write_text(json.dumps({"sample": ["old"], "_known": ["old"], "_baselines": ["sample:jp", "sample:tw"]}))
        tw = TaiwanItem("m123", "商品A", 100, [], "https://tw.mercari.com/zh-hant/items/one")
        error = io.StringIO()
        with patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(side_effect=[RuntimeError("private-query"), [tw]])), patch.object(app, "notify", return_value=True) as send, contextlib.redirect_stderr(error):
            self.assertEqual(self.run_check(), 1)
        send.assert_called_once()
        self.assertNotIn("private-query", error.getvalue())
        state = json.loads(self.seen_path.read_text())
        self.assertIn("m123", state["_known"])
        self.assertIn("old", state["sample"])

    def test_new_source_baseline_does_not_consume_pending_notification(self):
        self.write_watches([watch(price_max=None, sources=["jp", "tw"])])
        self.seen_path.write_text(json.dumps({"sample": ["old"], "_known": ["old"], "_baselines": ["sample:jp"]}))
        tw = TaiwanItem("m123", "商品A", 100, [], "https://tw.mercari.com/zh-hant/items/one")
        with patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(side_effect=[[item("m123")], [tw]])), patch.object(app, "notify", return_value=False):
            self.assertEqual(self.run_check(), 1)
        state = json.loads(self.seen_path.read_text())
        self.assertNotIn("m123", state["_known"])
        self.assertNotIn("m123", state["sample"])
        with patch.object(app, "Mercapi"), patch.object(app, "search_new", AsyncMock(side_effect=[[item("m123")], [tw]])), patch.object(app, "notify", return_value=True) as send:
            self.assertEqual(self.run_check(), 0)
        self.assertEqual(len(send.call_args.args[0]), 1)


if __name__ == "__main__":
    unittest.main()

