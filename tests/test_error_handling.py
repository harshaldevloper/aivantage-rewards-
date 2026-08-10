#!/usr/bin/env python3
"""Regression tests for the failure paths. Stdlib unittest, no pip install.

    python -m unittest discover -s tests

Every case here is a bug that has to stay fixed rather than a behaviour worth
restating: a swallowed failure in this pipeline shows up days later as a digest
nobody received, a clip nobody can approve, or a reel posted twice.
"""
import json
import sys
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import approve  # noqa: E402
import common  # noqa: E402
import publish  # noqa: E402
import watch  # noqa: E402


class StateFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_write_json_is_atomic(self):
        p = self.dir / "state" / "queue.json"
        common.write_json(p, {"a": 1})
        self.assertEqual(json.loads(p.read_text()), {"a": 1})
        self.assertEqual(list(p.parent.glob("*.tmp")), [])

    def test_corrupt_state_is_not_treated_as_empty(self):
        p = self.dir / "queue.json"
        p.write_text('{"half":', encoding="utf-8")
        with self.assertRaises(common.PipelineError):
            common.read_json(p, default={})

    def test_missing_state_falls_back(self):
        self.assertEqual(common.read_json(self.dir / "nope.json", default={}), {})


class Http(unittest.TestCase):
    def test_non_json_body_is_an_error_not_a_none(self):
        req = urllib.request.Request("https://example.invalid")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"<html>maintenance</html>"
        with mock.patch.object(common.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(common.PipelineError):
                common.request_json(req, retries=1, label="x")

    def test_transient_status_is_retried_then_raised(self):
        req = urllib.request.Request("https://example.invalid")
        err = common.urllib.error.HTTPError("u", 503, "busy", {}, None)
        err.read = lambda: b"busy"
        with mock.patch.object(common.urllib.request, "urlopen", side_effect=err), \
                mock.patch.object(common.time, "sleep"):
            with self.assertRaises(common.HttpError) as ctx:
                common.request_json(req, retries=3, label="x")
        self.assertEqual(ctx.exception.status, 503)

    def test_permanent_status_is_not_retried(self):
        req = urllib.request.Request("https://example.invalid")
        err = common.urllib.error.HTTPError("u", 401, "no", {}, None)
        err.read = lambda: b"unauthorized"
        with mock.patch.object(common.urllib.request, "urlopen", side_effect=err) as u:
            with self.assertRaises(common.HttpError):
                common.request_json(req, retries=3, label="x")
        self.assertEqual(u.call_count, 1)


class Watch(unittest.TestCase):
    def test_telegram_ok_false_is_a_failure(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t",
                                            "TELEGRAM_CHAT_ID": "c"}), \
                mock.patch.object(watch, "request_json",
                                  return_value={"ok": False, "description": "chat not found"}):
            with self.assertRaises(common.PipelineError):
                watch.send("hi")

    def test_feed_shape_change_is_loud(self):
        with mock.patch.object(watch, "request_json", return_value={"data": []}):
            with self.assertRaises(common.PipelineError):
                watch.fetch("Clipping")

    def test_unreadable_budget_is_none_not_zero(self):
        self.assertIsNone(watch.money(None))
        self.assertIsNone(watch.money("n/a"))
        self.assertEqual(watch.money("$1,234.50"), 1234.50)


class Approve(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.queue, self.offset = d / "queue.json", d / "offset.json"
        patches = [mock.patch.object(approve, "QUEUE", self.queue),
                   mock.patch.object(approve, "OFFSET", self.offset),
                   mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t",
                                                  "TELEGRAM_CHAT_ID": "c"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_expired_callback_does_not_lose_the_verdict(self):
        common.write_json(self.queue, {"c1": {"status": "pending", "message_id": 5}})

        def call(method, params=None, files=None):
            if method == "getUpdates":
                return [{"update_id": 7,
                         "callback_query": {"id": "q", "data": "ok:c1"}}]
            raise common.PipelineError("query is too old")

        with mock.patch.object(approve, "call", call):
            approve.cmd_poll(None)

        self.assertEqual(json.loads(self.queue.read_text())["c1"]["status"], "approved")
        self.assertEqual(json.loads(self.offset.read_text()), {"offset": 8})

    def test_crash_midway_keeps_the_verdicts_already_read(self):
        common.write_json(self.queue, {"a": {"status": "pending"},
                                       "b": {"status": "pending"}})

        def call(method, params=None, files=None):
            if method == "getUpdates":
                return [{"update_id": 1, "callback_query": {"id": "q", "data": "ok:a"}},
                        {"update_id": 2, "callback_query": {"id": "q", "data": "no:b"}}]
            raise MemoryError("runner died")

        with mock.patch.object(approve, "call", call):
            with self.assertRaises(MemoryError):
                approve.cmd_poll(None)

        self.assertEqual(json.loads(self.queue.read_text())["a"]["status"], "approved")
        # 'b' was never handled, so its update must be redelivered next run.
        self.assertEqual(json.loads(self.offset.read_text()), {"offset": 2})


class Publish(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = Path(self.tmp.name) / "queue.json"
        for p in (mock.patch.object(publish, "QUEUE", self.queue),
                  mock.patch.object(publish, "IG_USER", "1"),
                  mock.patch.object(publish, "TOKEN", "t"),
                  mock.patch.object(publish, "remaining_quota", lambda: 25),
                  mock.patch.dict("os.environ", {"IG_USER_ID": "1",
                                                 "IG_ACCESS_TOKEN": "t"})):
            p.start()
            self.addCleanup(p.stop)

    def test_one_bad_clip_neither_stops_the_batch_nor_passes_silently(self):
        common.write_json(self.queue, {
            "good": {"status": "approved", "url": "https://x/a.mp4"},
            "bad": {"status": "approved", "url": "https://x/b.mp4"},
        })

        def publish_one(clip_id, entry):
            if clip_id == "good":
                return "media-1"
            raise publish.ClipError("Instagram could not process the video")

        with mock.patch.object(publish, "publish_one", publish_one), \
                mock.patch.object(sys, "argv", ["publish.py"]):
            with self.assertRaises(SystemExit) as ctx:
                publish.main()

        self.assertEqual(ctx.exception.code, 1)
        queue = json.loads(self.queue.read_text())
        self.assertTrue(queue["good"]["published_at"])
        self.assertNotIn("published_at", queue["bad"])

    def test_dead_token_stops_everything(self):
        common.write_json(self.queue, {"a": {"status": "approved", "url": "https://x"}})
        with mock.patch.object(publish, "publish_one",
                               mock.Mock(side_effect=publish.FatalError("code 190"))), \
                mock.patch.object(sys, "argv", ["publish.py"]):
            with self.assertRaises(publish.FatalError):
                publish.main()

    def test_a_200_carrying_an_error_object_is_not_success(self):
        with mock.patch.object(publish, "request_json",
                               return_value={"error": {"code": 100}}):
            with self.assertRaises(publish.ClipError):
                publish.graph("1/media")


if __name__ == "__main__":
    unittest.main()
