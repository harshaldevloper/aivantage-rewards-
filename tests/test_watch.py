"""Unit tests for watch.py -- filtering, ranking and digest rendering."""

import json
import re

import pytest


def campaign(**over):
    base = {
        "id": "c1",
        "title": "Dreamina AI UGC",
        "description": "Make clips with our AI video tool",
        "category": "Product",
        "budgetRemaining": "$17,070.77",
        "payouts": [
            {
                "platform": "instagram",
                "pricePerThousandViews": 10.0,
                "minViewsRequired": 1000,
                "maxPayoutPerSubmission": 50,
            }
        ],
    }
    base.update(over)
    return base


class TestMoney:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("$17,070.77", 17070.77),
            ("$0", 0.0),
            ("1234", 1234.0),
            (2500, 2500.0),
            ("", 0.0),
            (None, 0.0),
        ],
    )
    def test_parses_currency_strings(self, watch, raw, expected):
        assert watch.money(raw) == pytest.approx(expected)

    def test_unparseable_value_is_zero_not_an_exception(self, watch):
        # Two decimal points survive the strip but not float().
        assert watch.money("$1.2.3") == 0.0


class TestPayoutFor:
    def test_returns_the_matching_platform_row(self, watch):
        p = watch.payout_for(campaign(), "instagram")
        assert p["pricePerThousandViews"] == 10.0

    def test_returns_none_when_the_platform_is_absent(self, watch):
        assert watch.payout_for(campaign(), "tiktok") is None

    def test_returns_none_when_there_are_no_payouts(self, watch):
        assert watch.payout_for({"payouts": []}, "instagram") is None
        assert watch.payout_for({}, "instagram") is None


class TestRelevant:
    def test_technology_category_always_matches(self, watch):
        assert watch.relevant({"category": "Technology", "title": "Socks"})

    @pytest.mark.parametrize(
        "title",
        ["An AI tool", "a.i. clips", "GPT wrapper", "no-code builder", "LLM agent"],
    )
    def test_keywords_match_in_the_title(self, watch, title):
        assert watch.relevant({"category": "Product", "title": title})

    def test_keywords_match_in_the_description(self, watch):
        assert watch.relevant(
            {"category": "Product", "title": "Thing", "description": "a chatbot"}
        )

    def test_unrelated_campaign_is_rejected(self, watch):
        assert not watch.relevant(
            {"category": "Fashion", "title": "Sneakers", "description": "shoe drop"}
        )

    def test_missing_fields_do_not_raise(self, watch):
        assert not watch.relevant({})


class TestScore:
    def test_bigger_remaining_budget_scores_higher(self, watch):
        p = {"pricePerThousandViews": 5, "minViewsRequired": 1000}
        rich = watch.score(campaign(budgetRemaining="$20,000"), p)
        poor = watch.score(campaign(budgetRemaining="$200"), p)
        assert rich > poor

    def test_high_view_floor_is_penalised(self, watch):
        c = campaign(budgetRemaining="$20,000")
        low = watch.score(c, {"pricePerThousandViews": 5, "minViewsRequired": 1000})
        high = watch.score(c, {"pricePerThousandViews": 5, "minViewsRequired": 28000})
        assert high < low

    def test_budget_multiplier_is_capped(self, watch):
        p = {"pricePerThousandViews": 1, "minViewsRequired": 1}
        huge = watch.score(campaign(budgetRemaining="$1,000,000"), p)
        big = watch.score(campaign(budgetRemaining="$15,000"), p)
        assert huge == pytest.approx(big)

    def test_missing_numbers_default_to_zero_cpm(self, watch):
        assert watch.score(campaign(), {}) == 0.0


class TestEvaluate:
    @pytest.fixture(autouse=True)
    def isolated_state(self, watch, tmp_path, monkeypatch):
        monkeypatch.setattr(watch, "STATE", tmp_path / "seen.json")

    def test_keeps_matching_campaigns_and_marks_them_new(self, watch, monkeypatch):
        monkeypatch.setattr(watch, "fetch", lambda kind: [campaign(id=kind)])
        picks = watch.evaluate()
        assert [c["id"] for c in picks] == ["Clipping", "UGC"]
        assert all(c["_new"] for c in picks)
        assert {c["_kind"] for c in picks} == {"Clipping", "UGC"}

    def test_seen_ids_are_not_marked_new(self, watch, monkeypatch):
        watch.STATE.parent.mkdir(parents=True, exist_ok=True)
        watch.STATE.write_text(json.dumps(["Clipping"]), encoding="utf-8")
        monkeypatch.setattr(watch, "fetch", lambda kind: [campaign(id=kind)])
        new = {c["id"]: c["_new"] for c in watch.evaluate()}
        assert new == {"Clipping": False, "UGC": True}

    @pytest.mark.parametrize(
        "over",
        [
            {"category": "Fashion", "title": "Sneakers", "description": ""},
            {"payouts": [{"platform": "tiktok", "pricePerThousandViews": 9}]},
            {"budgetRemaining": "$10"},
        ],
    )
    def test_filters_out_campaigns_that_do_not_qualify(self, watch, monkeypatch, over):
        monkeypatch.setattr(watch, "fetch", lambda kind: [campaign(**over)])
        assert watch.evaluate() == []

    def test_low_cpm_is_filtered(self, watch, monkeypatch):
        c = campaign()
        c["payouts"][0]["pricePerThousandViews"] = 0.1
        monkeypatch.setattr(watch, "fetch", lambda kind: [c])
        monkeypatch.setattr(watch, "MIN_CPM", 1.0)
        assert watch.evaluate() == []

    def test_unreachable_view_floor_is_filtered(self, watch, monkeypatch):
        c = campaign()
        c["payouts"][0]["minViewsRequired"] = 50000
        monkeypatch.setattr(watch, "fetch", lambda kind: [c])
        monkeypatch.setattr(watch, "MAX_MIN_VIEWS", 5000)
        assert watch.evaluate() == []

    def test_results_are_sorted_by_score_descending(self, watch, monkeypatch):
        weak = campaign(id="weak", budgetRemaining="$600")
        strong = campaign(id="strong", budgetRemaining="$30,000")
        monkeypatch.setattr(
            watch, "fetch", lambda kind: [weak, strong] if kind == "Clipping" else []
        )
        assert [c["id"] for c in watch.evaluate()] == ["strong", "weak"]


class TestFetch:
    def test_requests_the_feed_and_returns_campaigns(
        self, watch, monkeypatch, fake_response
    ):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = req.headers
            return fake_response(json.dumps({"campaigns": [{"id": "x"}]}))

        monkeypatch.setattr(watch.urllib.request, "urlopen", fake_urlopen)
        assert watch.fetch("Clipping") == [{"id": "x"}]
        assert "type=Clipping" in seen["url"]
        assert "limit=200" in seen["url"]

    def test_missing_campaigns_key_yields_empty_list(
        self, watch, monkeypatch, fake_response
    ):
        monkeypatch.setattr(
            watch.urllib.request, "urlopen", lambda *a, **k: fake_response("{}")
        )
        assert watch.fetch("UGC") == []


class TestRender:
    def test_empty_picks_render_a_no_match_message(self, watch):
        assert "No campaigns matched" in watch.render([])

    def test_digest_lists_campaigns_with_payout_detail(self, watch):
        c = campaign()
        c.update(_payout=c["payouts"][0], _kind="Clipping", _new=True, _score=1.0)
        out = watch.render([c])
        assert "1 live for AI niche" in out
        assert "1 new since last check." in out
        assert "🆕 " in out
        assert "$10.00/1k" in out
        assert "pays from 1,000 views" in out
        assert "cap $50/clip" in out
        assert out.rstrip().endswith("https://contentrewards.com")

    def test_seen_campaign_has_no_new_marker_and_no_cap_line(self, watch):
        c = campaign()
        c["payouts"][0].pop("maxPayoutPerSubmission")
        c.update(_payout=c["payouts"][0], _kind="UGC", _new=False, _score=1.0)
        out = watch.render([c])
        assert "🆕" not in out
        assert "new since last check" not in out
        assert "cap $" not in out

    def test_limit_caps_how_many_campaigns_are_listed(self, watch):
        picks = []
        for i in range(5):
            c = campaign(id=f"c{i}", title=f"Campaign {i}")
            c.update(_payout=c["payouts"][0], _kind="UGC", _new=False, _score=1.0)
            picks.append(c)
        out = watch.render(picks, limit=2)
        assert "Campaign 1" in out
        assert "Campaign 3" not in out

    def test_titles_are_escaped_and_truncated(self, watch):
        c = campaign(title="<b>Ampersand & Co</b> " + "x" * 80)
        c.update(_payout=c["payouts"][0], _kind="UGC", _new=False, _score=1.0)
        out = watch.render([c])
        assert "&lt;b&gt;Ampersand &amp; Co" in out
        assert "x" * 80 not in out


class TestEsc:
    def test_escapes_html_control_characters(self, watch):
        assert watch.esc("<a & b>") == "&lt;a &amp; b&gt;"

    def test_non_strings_are_coerced(self, watch):
        assert watch.esc(12) == "12"


class TestSend:
    def test_exits_when_credentials_are_missing(self, watch, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        with pytest.raises(SystemExit):
            watch.send("hi")

    def test_posts_html_to_the_bot_api(self, watch, monkeypatch, fake_response):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = req.data.decode()
            return fake_response(json.dumps({"ok": True}))

        monkeypatch.setattr(watch.urllib.request, "urlopen", fake_urlopen)
        assert watch.send("hello")["ok"] is True
        assert captured["url"].endswith("/bottok/sendMessage")
        assert "chat_id=42" in captured["body"]
        assert "parse_mode=HTML" in captured["body"]


class TestMain:
    @pytest.fixture(autouse=True)
    def isolated_state(self, watch, tmp_path, monkeypatch):
        monkeypatch.setattr(watch, "STATE", tmp_path / "seen.json")

    def _pick(self, is_new=True):
        c = campaign()
        c.update(_payout=c["payouts"][0], _kind="UGC", _new=is_new, _score=1.0)
        return c

    def test_dry_run_prints_plain_text_and_sends_nothing(
        self, watch, monkeypatch, capsys
    ):
        monkeypatch.setattr(watch, "evaluate", lambda: [self._pick()])
        monkeypatch.setattr(
            watch, "send", lambda text: pytest.fail("dry run must not send")
        )
        monkeypatch.setattr(watch.sys, "argv", ["watch.py", "--dry-run"])
        watch.main()
        out = capsys.readouterr().out
        assert not re.search(r"<[a-z/]", out)
        assert "Dreamina AI UGC" in out
        assert not watch.STATE.exists()

    def test_new_only_stays_quiet_when_nothing_is_new(self, watch, monkeypatch, capsys):
        monkeypatch.setattr(watch, "evaluate", lambda: [self._pick(is_new=False)])
        monkeypatch.setattr(watch.sys, "argv", ["watch.py", "--new-only"])
        watch.main()
        assert "staying quiet" in capsys.readouterr().out

    def test_sending_records_the_seen_ids(self, watch, monkeypatch, capsys):
        sent = []
        monkeypatch.setattr(watch, "evaluate", lambda: [self._pick()])
        monkeypatch.setattr(watch, "send", lambda text: sent.append(text))
        monkeypatch.setattr(watch.sys, "argv", ["watch.py"])
        watch.main()
        assert sent and "Dreamina" in sent[0]
        assert json.loads(watch.STATE.read_text(encoding="utf-8")) == ["c1"]
        assert "sent digest: 1 campaigns" in capsys.readouterr().out
