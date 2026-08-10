"""Unit tests for publish.py -- the Instagram publishing step.

`IG_USER` and `TOKEN` are module globals that the script only assigns under
`__main__`, so every test that exercises a Graph call sets them explicitly.
"""

import io
import json
import urllib.error

import pytest


@pytest.fixture(autouse=True)
def graph_credentials(publish, monkeypatch):
    monkeypatch.setattr(publish, "IG_USER", "1789", raising=False)
    monkeypatch.setattr(publish, "TOKEN", "tok", raising=False)


@pytest.fixture
def queue_file(publish, tmp_path, monkeypatch):
    path = tmp_path / "queue.json"
    monkeypatch.setattr(publish, "QUEUE", path)
    return path


def http_error(url, code, payload):
    return urllib.error.HTTPError(
        url, code, "err", None, io.BytesIO(json.dumps(payload).encode())
    )


def approved(**over):
    entry = {"status": "approved", "url": "https://example.com/a.mp4", "caption": "cap"}
    entry.update(over)
    return entry


class TestDieAndEnv:
    def test_die_exits_nonzero_and_writes_to_stderr(self, publish, capsys):
        with pytest.raises(SystemExit) as exc:
            publish.die("broken")
        assert exc.value.code == 1
        assert "broken" in capsys.readouterr().err

    def test_env_returns_a_set_value(self, publish, monkeypatch):
        monkeypatch.setenv("IG_USER_ID", "  123  ")
        assert publish.env("IG_USER_ID", "hint") == "123"

    def test_env_dies_with_the_hint_when_unset(self, publish, monkeypatch, capsys):
        monkeypatch.setenv("IG_USER_ID", "   ")
        with pytest.raises(SystemExit):
            publish.env("IG_USER_ID", "get it from the dashboard")
        assert "get it from the dashboard" in capsys.readouterr().err


class TestGraph:
    def test_get_puts_params_in_the_query_string(
        self, publish, monkeypatch, fake_response
    ):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["data"] = req.data
            return fake_response(json.dumps({"id": "1"}))

        monkeypatch.setattr(publish.urllib.request, "urlopen", fake_urlopen)
        assert publish.graph("1789/media", {"fields": "status"}) == {"id": "1"}
        assert "fields=status" in seen["url"]
        assert "access_token=tok" in seen["url"]
        assert seen["data"] is None

    def test_post_sends_a_urlencoded_body(self, publish, monkeypatch, fake_response):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = req.data.decode()
            return fake_response(json.dumps({"id": "2"}))

        monkeypatch.setattr(publish.urllib.request, "urlopen", fake_urlopen)
        publish.graph("1789/media", {"caption": "hi"}, method="POST")
        assert "caption=hi" in seen["body"]
        assert "access_token=tok" in seen["body"]
        assert "?" not in seen["url"]

    @pytest.mark.parametrize(
        "code,expected",
        [
            (190, "expire"),
            (4, "rate limit"),
            (32, "rate limit"),
            (100, "Graph API error"),
        ],
    )
    def test_graph_errors_are_explained_not_traced(
        self, publish, monkeypatch, capsys, code, expected
    ):
        def boom(req, timeout=None):
            raise http_error(req.full_url, 400, {"error": {"code": code, "message": "m"}})

        monkeypatch.setattr(publish.urllib.request, "urlopen", boom)
        with pytest.raises(SystemExit):
            publish.graph("1789/media")
        assert expected in capsys.readouterr().err

    def test_non_json_error_body_is_surfaced_verbatim(
        self, publish, monkeypatch, capsys
    ):
        def boom(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 500, "err", None, io.BytesIO(b"<html>gateway</html>")
            )

        monkeypatch.setattr(publish.urllib.request, "urlopen", boom)
        with pytest.raises(SystemExit):
            publish.graph("1789/media")
        assert "gateway" in capsys.readouterr().err

    def test_network_failure_is_reported(self, publish, monkeypatch, capsys):
        def boom(req, timeout=None):
            raise urllib.error.URLError("dns went away")

        monkeypatch.setattr(publish.urllib.request, "urlopen", boom)
        with pytest.raises(SystemExit):
            publish.graph("1789/media")
        assert "Could not reach the Graph API" in capsys.readouterr().err


class TestQueueIO:
    def test_missing_queue_dies(self, publish, queue_file, capsys):
        with pytest.raises(SystemExit):
            publish.load_queue()
        assert "No queue at" in capsys.readouterr().err

    def test_invalid_json_dies_with_the_path(self, publish, queue_file, capsys):
        queue_file.write_text("{oops", encoding="utf-8")
        with pytest.raises(SystemExit):
            publish.load_queue()
        assert "not valid JSON" in capsys.readouterr().err

    def test_round_trips_a_queue(self, publish, queue_file):
        publish.save_queue({"a": approved()})
        assert publish.load_queue() == {"a": approved()}


class TestRemainingQuota:
    def test_subtracts_reported_usage_from_the_daily_cap(self, publish, monkeypatch):
        monkeypatch.setattr(
            publish, "graph", lambda *a, **k: {"data": [{"quota_usage": 5}]}
        )
        assert publish.remaining_quota() == publish.DAILY_CAP - 5

    def test_never_returns_a_negative_quota(self, publish, monkeypatch):
        monkeypatch.setattr(
            publish, "graph", lambda *a, **k: {"data": [{"quota_usage": 999}]}
        )
        assert publish.remaining_quota() == 0

    def test_unreadable_quota_assumes_full_capacity(self, publish, monkeypatch, capsys):
        def boom(*a, **k):
            raise RuntimeError("nope")

        monkeypatch.setattr(publish, "graph", boom)
        assert publish.remaining_quota() == publish.DAILY_CAP
        assert "could not read publishing quota" in capsys.readouterr().out

    def test_a_fatal_graph_error_still_propagates(self, publish, monkeypatch):
        def boom(*a, **k):
            raise SystemExit(1)

        monkeypatch.setattr(publish, "graph", boom)
        with pytest.raises(SystemExit):
            publish.remaining_quota()


class TestWaitForContainer:
    @pytest.fixture(autouse=True)
    def no_sleeping(self, publish, monkeypatch):
        monkeypatch.setattr(publish.time, "sleep", lambda s: None)

    def test_returns_once_the_container_is_finished(self, publish, monkeypatch):
        monkeypatch.setattr(
            publish, "graph", lambda *a, **k: {"status_code": "FINISHED"}
        )
        assert publish.wait_for_container("c1") is True

    def test_polls_until_finished(self, publish, monkeypatch):
        statuses = iter(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
        monkeypatch.setattr(
            publish, "graph", lambda *a, **k: {"status_code": next(statuses)}
        )
        assert publish.wait_for_container("c1") is True

    def test_error_status_explains_the_usual_causes(self, publish, monkeypatch, capsys):
        monkeypatch.setattr(
            publish,
            "graph",
            lambda *a, **k: {"status_code": "ERROR", "status": "bad aspect ratio"},
        )
        with pytest.raises(SystemExit):
            publish.wait_for_container("c1")
        err = capsys.readouterr().err
        assert "could not process the video" in err
        assert "bad aspect ratio" in err

    def test_gives_up_after_the_timeout(self, publish, monkeypatch, capsys):
        monkeypatch.setattr(
            publish, "graph", lambda *a, **k: {"status_code": "IN_PROGRESS"}
        )
        with pytest.raises(SystemExit):
            publish.wait_for_container("c1")
        assert "still not FINISHED" in capsys.readouterr().err


class TestPublishOne:
    def test_creates_waits_then_publishes(self, publish, monkeypatch, capsys):
        seen = []

        def fake_graph(path, params=None, method="GET"):
            seen.append((path, params, method))
            if path.endswith("/media"):
                return {"id": "container-1"}
            return {"id": "media-1"}

        monkeypatch.setattr(publish, "graph", fake_graph)
        monkeypatch.setattr(publish, "wait_for_container", lambda cid: True)

        assert publish.publish_one("a", approved()) == "media-1"
        assert seen[0][0] == "1789/media"
        assert seen[0][1]["media_type"] == "REELS"
        assert seen[0][1]["video_url"] == "https://example.com/a.mp4"
        assert seen[1] == ("1789/media_publish", {"creation_id": "container-1"}, "POST")

    def test_caption_is_truncated_to_the_instagram_limit(self, publish, monkeypatch):
        seen = {}

        def fake_graph(path, params=None, method="GET"):
            seen.setdefault(path, params)
            return {"id": "x"}

        monkeypatch.setattr(publish, "graph", fake_graph)
        monkeypatch.setattr(publish, "wait_for_container", lambda cid: True)
        publish.publish_one("a", approved(caption="y" * 3000))
        assert len(seen["1789/media"]["caption"]) == 2200

    @pytest.mark.parametrize("url", [None, "", "out/a.mp4"])
    def test_entries_without_a_fetchable_url_are_skipped(
        self, publish, monkeypatch, capsys, url
    ):
        monkeypatch.setattr(
            publish, "graph", lambda *a, **k: pytest.fail("must not call Graph")
        )
        assert publish.publish_one("a", approved(url=url)) is None
        assert "no public URL" in capsys.readouterr().out

    def test_missing_container_id_is_fatal(self, publish, monkeypatch, capsys):
        monkeypatch.setattr(publish, "graph", lambda *a, **k: {})
        with pytest.raises(SystemExit):
            publish.publish_one("a", approved())
        assert "No container id" in capsys.readouterr().err

    def test_missing_media_id_is_fatal(self, publish, monkeypatch, capsys):
        def fake_graph(path, params=None, method="GET"):
            return {"id": "c1"} if path.endswith("/media") else {}

        monkeypatch.setattr(publish, "graph", fake_graph)
        monkeypatch.setattr(publish, "wait_for_container", lambda cid: True)
        with pytest.raises(SystemExit):
            publish.publish_one("a", approved())
        assert "Publish returned no media id" in capsys.readouterr().err


class TestMain:
    def run(self, publish, monkeypatch, argv):
        monkeypatch.setattr(publish.sys, "argv", ["publish.py"] + argv)
        publish.main()

    def test_nothing_approved_is_a_clean_no_op(
        self, publish, queue_file, monkeypatch, capsys
    ):
        publish.save_queue({"a": {"status": "pending"}})
        self.run(publish, monkeypatch, [])
        assert "nothing approved and unpublished" in capsys.readouterr().out

    def test_already_published_entries_are_not_reconsidered(
        self, publish, queue_file, monkeypatch, capsys
    ):
        publish.save_queue({"a": approved(published_at=123)})
        self.run(publish, monkeypatch, [])
        assert "nothing approved and unpublished" in capsys.readouterr().out

    def test_dry_run_lists_candidates_and_changes_nothing(
        self, publish, queue_file, monkeypatch, capsys
    ):
        publish.save_queue({"a": approved(), "b": approved(url=None)})
        monkeypatch.setattr(
            publish, "publish_one", lambda *a: pytest.fail("dry run must not publish")
        )
        self.run(publish, monkeypatch, ["--dry-run"])
        out = capsys.readouterr().out
        assert "would publish a" in out
        assert "SKIP b" in out
        assert "1 would publish" in out
        assert publish.load_queue()["a"] == approved()

    def test_publishes_and_records_the_media_id(
        self, publish, queue_file, monkeypatch, capsys
    ):
        publish.save_queue({"a": approved()})
        monkeypatch.setattr(publish, "remaining_quota", lambda: 25)
        monkeypatch.setattr(publish, "publish_one", lambda cid, e: "media-1")
        self.run(publish, monkeypatch, [])

        entry = publish.load_queue()["a"]
        assert entry["ig_media_id"] == "media-1"
        assert isinstance(entry["published_at"], int)
        assert "published 1 clip(s)" in capsys.readouterr().out

    def test_a_skipped_clip_is_not_marked_published(
        self, publish, queue_file, monkeypatch, capsys
    ):
        publish.save_queue({"a": approved()})
        monkeypatch.setattr(publish, "remaining_quota", lambda: 25)
        monkeypatch.setattr(publish, "publish_one", lambda cid, e: None)
        self.run(publish, monkeypatch, [])
        assert "published_at" not in publish.load_queue()["a"]
        assert "published 0 clip(s)" in capsys.readouterr().out

    def test_limit_caps_the_batch(self, publish, queue_file, monkeypatch, capsys):
        publish.save_queue({str(i): approved() for i in range(4)})
        monkeypatch.setattr(publish, "remaining_quota", lambda: 25)
        done = []
        monkeypatch.setattr(
            publish, "publish_one", lambda cid, e: done.append(cid) or f"m{cid}"
        )
        self.run(publish, monkeypatch, ["--limit", "2"])
        assert len(done) == 2
        assert "publishing 2 of 4 this run" in capsys.readouterr().out

    def test_exhausted_quota_stops_before_any_call(
        self, publish, queue_file, monkeypatch, capsys
    ):
        publish.save_queue({"a": approved()})
        monkeypatch.setattr(publish, "remaining_quota", lambda: 0)
        monkeypatch.setattr(
            publish, "publish_one", lambda *a: pytest.fail("quota is exhausted")
        )
        self.run(publish, monkeypatch, [])
        assert "quota exhausted" in capsys.readouterr().out
