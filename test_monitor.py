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
                      [watch(keywords=[""])]]
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


if __name__ == "__main__":
    unittest.main()

