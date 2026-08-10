"""Unit tests for approve.py -- the Telegram approval gate."""

import argparse
import json
import urllib.error

import pytest


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


@pytest.fixture
def state(approve, tmp_path, monkeypatch):
    monkeypatch.setattr(approve, "QUEUE", tmp_path / "queue.json")
    monkeypatch.setattr(approve, "OFFSET", tmp_path / "tg_offset.json")
    return approve


@pytest.fixture
def calls(approve, monkeypatch):
    """Record every Bot API call instead of making it."""
    recorded = []

    def fake_call(method, params=None, files=None):
        recorded.append((method, params or {}, files))
        return {"ok": True, "result": {"message_id": 100 + len(recorded)}}

    monkeypatch.setattr(approve, "call", fake_call)
    return recorded


class TestCredentials:
    def test_token_and_chat_id_are_read_from_the_environment(self, approve):
        assert approve.token() == "tok"
        assert approve.chat_id() == "42"

    def test_missing_token_exits(self, approve, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
        with pytest.raises(SystemExit):
            approve.token()

    def test_missing_chat_id_exits(self, approve, monkeypatch):
        monkeypatch.delenv("TELEGRAM_CHAT_ID")
        with pytest.raises(SystemExit):
            approve.chat_id()


class TestCall:
    def test_urlencodes_params_and_drops_nones(
        self, approve, monkeypatch, fake_response
    ):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = req.data.decode()
            seen["ctype"] = req.get_header("Content-type")
            return fake_response(json.dumps({"ok": True}))

        monkeypatch.setattr(approve.urllib.request, "urlopen", fake_urlopen)
        assert approve.call("sendMessage", {"text": "hi", "extra": None})["ok"]
        assert seen["url"] == "https://api.telegram.org/bottok/sendMessage"
        assert seen["body"] == "text=hi"
        assert seen["ctype"] != "multipart/form-data"

    def test_file_uploads_use_multipart(
        self, approve, monkeypatch, tmp_path, fake_response
    ):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"\x00binary")
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["ctype"] = req.get_header("Content-type")
            seen["body"] = req.data
            return fake_response(json.dumps({"ok": True}))

        monkeypatch.setattr(approve.urllib.request, "urlopen", fake_urlopen)
        approve.call("sendVideo", {"chat_id": "42"}, files={"video": clip})

        assert seen["ctype"].startswith("multipart/form-data; boundary=")
        assert b'name="chat_id"' in seen["body"]
        assert b'filename="clip.mp4"' in seen["body"]
        assert b"Content-Type: video/mp4" in seen["body"]
        assert b"\x00binary" in seen["body"]

    def test_http_error_becomes_a_readable_exit(self, approve, monkeypatch):
        def boom(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", None, __import__("io").BytesIO(
                    b'{"description":"chat not found"}'
                )
            )

        monkeypatch.setattr(approve.urllib.request, "urlopen", boom)
        with pytest.raises(SystemExit) as exc:
            approve.call("sendMessage", {"text": "hi"})
        assert "chat not found" in str(exc.value)
        assert "400" in str(exc.value)


class TestLoadSave:
    def test_load_returns_the_default_when_the_file_is_absent(self, approve, tmp_path):
        assert approve.load(tmp_path / "nope.json", {"a": 1}) == {"a": 1}

    def test_save_creates_parents_and_load_round_trips(self, approve, tmp_path):
        path = tmp_path / "nested" / "queue.json"
        approve.save(path, {"clip": {"status": "pending"}})
        assert approve.load(path, {}) == {"clip": {"status": "pending"}}


class TestKeyboard:
    def test_offers_approve_and_reject_with_the_clip_id(self, approve):
        row = json.loads(approve.keyboard("clip-1"))["inline_keyboard"][0]
        assert [b["callback_data"] for b in row] == ["ok:clip-1", "no:clip-1"]


class TestCmdSend:
    def manifest(self, tmp_path, clips):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(clips), encoding="utf-8")
        return path

    def args(self, manifest, batch=5):
        return argparse.Namespace(manifest=str(manifest), batch=batch)

    def test_uploads_local_files_and_queues_them_pending(
        self, state, tmp_path, calls, monkeypatch
    ):
        monkeypatch.setattr(state.time, "sleep", lambda s: None)
        video = tmp_path / "a.mp4"
        video.write_bytes(b"mp4")
        manifest = self.manifest(
            tmp_path,
            [
                {
                    "id": "a",
                    "campaign": "Dreamina",
                    "caption": "cap",
                    "cpm": "10.00",
                    "min_views": 1000,
                    "file": str(video),
                    "url": "https://example.com/a.mp4",
                }
            ],
        )
        state.cmd_send(self.args(manifest))

        method, params, files = calls[0]
        assert method == "sendVideo"
        assert files == {"video": str(video)}
        assert "video" not in params
        assert "$10.00/1k" in params["caption"]

        entry = state.load(state.QUEUE, {})["a"]
        assert entry["status"] == "pending"
        assert entry["url"] == "https://example.com/a.mp4"
        assert entry["message_id"] == 101

    def test_falls_back_to_the_public_url_when_no_local_file(
        self, state, tmp_path, calls, monkeypatch
    ):
        monkeypatch.setattr(state.time, "sleep", lambda s: None)
        manifest = self.manifest(
            tmp_path, [{"id": "b", "url": "https://example.com/b.mp4"}]
        )
        state.cmd_send(self.args(manifest))
        _, params, files = calls[0]
        assert files is None
        assert params["video"] == "https://example.com/b.mp4"

    def test_caption_omits_payout_line_without_a_cpm(
        self, state, tmp_path, calls, monkeypatch
    ):
        monkeypatch.setattr(state.time, "sleep", lambda s: None)
        manifest = self.manifest(tmp_path, [{"id": "c", "url": "https://x/c.mp4"}])
        state.cmd_send(self.args(manifest))
        assert "pays from" not in calls[0][1]["caption"]

    def test_caption_is_truncated_to_the_telegram_limit(
        self, state, tmp_path, calls, monkeypatch
    ):
        monkeypatch.setattr(state.time, "sleep", lambda s: None)
        manifest = self.manifest(
            tmp_path, [{"id": "d", "url": "https://x/d.mp4", "caption": "x" * 2000}]
        )
        state.cmd_send(self.args(manifest))
        assert len(calls[0][1]["caption"]) == 1024

    def test_batch_limits_how_many_clips_go_out(
        self, state, tmp_path, calls, monkeypatch
    ):
        monkeypatch.setattr(state.time, "sleep", lambda s: None)
        clips = [{"id": str(i), "url": f"https://x/{i}.mp4"} for i in range(4)]
        state.cmd_send(self.args(self.manifest(tmp_path, clips), batch=2))
        assert len(calls) == 2
        assert sorted(state.load(state.QUEUE, {})) == ["0", "1"]

    def test_already_queued_clips_are_not_resent(
        self, state, tmp_path, calls, capsys
    ):
        state.save(state.QUEUE, {"a": {"status": "pending"}})
        manifest = self.manifest(tmp_path, [{"id": "a", "url": "https://x/a.mp4"}])
        state.cmd_send(self.args(manifest))
        assert calls == []
        assert "nothing new to review" in capsys.readouterr().out


class TestCmdPoll:
    def poll_with(self, state, monkeypatch, updates):
        made = []

        def fake_call(method, params=None, files=None):
            made.append((method, params or {}))
            if method == "getUpdates":
                return {"result": updates}
            return {"ok": True}

        monkeypatch.setattr(state, "call", fake_call)
        state.cmd_poll(argparse.Namespace())
        return made

    def update(self, update_id, data, cq_id="q1"):
        return {"update_id": update_id, "callback_query": {"id": cq_id, "data": data}}

    def test_no_taps_leaves_state_untouched(self, state, monkeypatch, capsys):
        self.poll_with(state, monkeypatch, [])
        assert "no taps" in capsys.readouterr().out
        assert not state.OFFSET.exists()

    def test_approve_tap_marks_the_clip_approved_and_disables_buttons(
        self, state, monkeypatch, capsys
    ):
        state.save(state.QUEUE, {"a": {"status": "pending", "message_id": 7}})
        made = self.poll_with(state, monkeypatch, [self.update(11, "ok:a")])

        entry = state.load(state.QUEUE, {})["a"]
        assert entry["status"] == "approved"
        assert isinstance(entry["decided_at"], int)
        assert state.load(state.OFFSET, {})["offset"] == 12

        methods = [m for m, _ in made]
        assert methods == ["getUpdates", "answerCallbackQuery", "editMessageReplyMarkup"]
        assert "Approved" in made[1][1]["text"]
        markup = json.loads(made[2][1]["reply_markup"])
        assert markup["inline_keyboard"][0][0]["callback_data"] == "done"
        assert "recorded 1 verdict(s)" in capsys.readouterr().out

    def test_reject_tap_marks_the_clip_rejected(self, state, monkeypatch):
        state.save(state.QUEUE, {"a": {"status": "pending", "message_id": 7}})
        made = self.poll_with(state, monkeypatch, [self.update(1, "no:a")])
        assert state.load(state.QUEUE, {})["a"]["status"] == "rejected"
        assert made[1][1]["text"] == "Rejected"

    def test_entry_without_message_id_skips_the_markup_edit(self, state, monkeypatch):
        state.save(state.QUEUE, {"a": {"status": "pending"}})
        made = self.poll_with(state, monkeypatch, [self.update(1, "ok:a")])
        assert "editMessageReplyMarkup" not in [m for m, _ in made]

    def test_tap_for_an_unknown_clip_is_answered_and_ignored(
        self, state, monkeypatch, capsys
    ):
        state.save(state.QUEUE, {})
        made = self.poll_with(state, monkeypatch, [self.update(5, "ok:ghost")])
        assert made[1][1]["text"] == "Unknown clip"
        assert "recorded 0 verdict(s)" in capsys.readouterr().out

    def test_non_callback_updates_only_advance_the_offset(self, state, monkeypatch):
        self.poll_with(state, monkeypatch, [{"update_id": 9, "message": {"text": "hi"}}])
        assert state.load(state.OFFSET, {})["offset"] == 10

    def test_stored_offset_is_sent_back_to_telegram(self, state, monkeypatch):
        state.save(state.OFFSET, {"offset": 77})
        made = self.poll_with(state, monkeypatch, [])
        assert made[0][1]["offset"] == 77


class TestCmdStatus:
    def test_empty_queue_reports_empty(self, state, capsys):
        state.cmd_status(argparse.Namespace())
        assert "queue empty" in capsys.readouterr().out

    def test_counts_statuses_and_lists_approved_targets(self, state, capsys):
        state.save(
            state.QUEUE,
            {
                "a": {"status": "approved", "url": "https://x/a.mp4"},
                "b": {"status": "approved", "file": "/tmp/b.mp4"},
                "c": {"status": "rejected"},
            },
        )
        state.cmd_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert '"approved": 2' in out
        assert '"rejected": 1' in out
        assert "approved: a -> https://x/a.mp4" in out
        assert "approved: b -> /tmp/b.mp4" in out


class TestMain:
    def test_dispatches_to_the_named_subcommand(self, approve, monkeypatch):
        seen = []
        monkeypatch.setattr(approve, "cmd_status", lambda args: seen.append(args))
        monkeypatch.setattr(approve.sys, "argv", ["approve.py", "status"])
        approve.main()
        assert len(seen) == 1

    def test_send_accepts_manifest_and_batch_flags(self, approve, monkeypatch):
        seen = []
        monkeypatch.setattr(approve, "cmd_send", lambda args: seen.append(args))
        monkeypatch.setattr(
            approve.sys,
            "argv",
            ["approve.py", "send", "--manifest", "m.json", "--batch", "2"],
        )
        approve.main()
        assert seen[0].manifest == "m.json"
        assert seen[0].batch == 2

    def test_a_subcommand_is_required(self, approve, monkeypatch):
        monkeypatch.setattr(approve.sys, "argv", ["approve.py"])
        with pytest.raises(SystemExit):
            approve.main()
