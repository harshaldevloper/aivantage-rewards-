"""Unit tests for studio/validate-reels.py -- the pre-render config check."""

import json

import pytest


def config(**over):
    cfg = {
        "name": "01-clips",
        "keyword": "CLIPS",
        "palette": "cinema",
        "script": "Comment the word clips and I'll send it to you. It is free.",
        "highlight": ["free"],
    }
    cfg.update(over)
    return cfg


@pytest.fixture
def write_config(validate_reels, tmp_path, monkeypatch):
    """Write a config into a temporary reels/ dir wired into the module."""
    reels = tmp_path / "reels"
    reels.mkdir()
    (tmp_path / "scene.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "scene-yt.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(validate_reels, "HERE", tmp_path)
    monkeypatch.setattr(validate_reels, "REELS", reels)

    def _write(cfg, name=None, raw=None):
        stem = name or (cfg or {}).get("name", "cfg")
        path = reels / f"{stem}.json"
        path.write_text(
            raw if raw is not None else json.dumps(cfg), encoding="utf-8"
        )
        return path

    return _write


class TestCheck:
    def test_a_good_config_is_clean(self, validate_reels, write_config):
        assert validate_reels.check(write_config(config())) == ([], [])

    def test_invalid_json_is_reported_once(self, validate_reels, write_config):
        errs, warns = validate_reels.check(write_config(None, name="broken", raw="{"))
        assert len(errs) == 1 and "not valid JSON" in errs[0]
        assert warns == []

    @pytest.mark.parametrize("field", ["name", "keyword", "script"])
    def test_required_fields_are_enforced(self, validate_reels, write_config, field):
        cfg = config()
        del cfg[field]
        errs, _ = validate_reels.check(write_config(cfg, name="01-clips"))
        assert errs == [f"missing required field: {field}"]

    def test_name_must_match_the_filename(self, validate_reels, write_config):
        errs, _ = validate_reels.check(
            write_config(config(name="other"), name="01-clips")
        )
        assert any("does not match filename" in e for e in errs)

    def test_missing_scene_file_is_an_error(self, validate_reels, write_config):
        errs, _ = validate_reels.check(write_config(config(scene="ghost.html")))
        assert any("scene file not found" in e for e in errs)

    def test_unknown_palette_is_an_error(self, validate_reels, write_config):
        errs, _ = validate_reels.check(write_config(config(palette="neon")))
        assert any('palette "neon" unknown' in e for e in errs)

    def test_palette_defaults_to_cinema_when_absent(self, validate_reels, write_config):
        cfg = config()
        del cfg["palette"]
        assert validate_reels.check(write_config(cfg)) == ([], [])

    def test_unknown_demo_act_is_an_error(self, validate_reels, write_config):
        errs, _ = validate_reels.check(
            write_config(config(demo={"act": "dance", "start": 1, "end": 2}))
        )
        assert any('demo.act "dance" unknown' in e for e in errs)

    def test_demo_window_must_move_forwards(self, validate_reels, write_config):
        errs, _ = validate_reels.check(
            write_config(config(demo={"act": "edit", "start": 5, "end": 5}))
        )
        assert any("must be before demo.end" in e for e in errs)

    def test_valid_demo_window_passes(self, validate_reels, write_config):
        assert validate_reels.check(
            write_config(config(demo={"act": "edit", "start": 1, "end": 9}))
        ) == ([], [])

    def test_unknown_beat_pose_and_expression_are_errors(
        self, validate_reels, write_config
    ):
        errs, _ = validate_reels.check(
            write_config(config(beats=[{"pose": "dab", "expr": "smug"}]))
        )
        assert any('beats[0].pose "dab" unknown' in e for e in errs)
        assert any('beats[0].expr "smug" unknown' in e for e in errs)

    def test_valid_beats_pass(self, validate_reels, write_config):
        assert validate_reels.check(
            write_config(config(beats=[{"pose": "point", "expr": "happy"}]))
        ) == ([], [])

    def test_keyword_absent_from_the_script_is_an_error(
        self, validate_reels, write_config
    ):
        errs, _ = validate_reels.check(write_config(config(keyword="NOTSPOKEN")))
        assert any("is never spoken in the script" in e for e in errs)

    def test_lowercase_keyword_only_warns(self, validate_reels, write_config):
        errs, warns = validate_reels.check(write_config(config(keyword="clips")))
        assert errs == []
        assert any("is not uppercase" in w for w in warns)

    def test_unspoken_highlight_words_warn(self, validate_reels, write_config):
        _, warns = validate_reels.check(write_config(config(highlight=["unicorn"])))
        assert any("highlight words never appear" in w for w in warns)

    def test_highlight_matching_is_script_agnostic(self, validate_reels, write_config):
        # Devanagari has no \w word boundaries, so matching is by substring.
        cfg = config(
            script="फ्री में क्लिप्स मिलेंगी। Comment CLIPS.",
            highlight=["फ्री"],
        )
        assert validate_reels.check(write_config(cfg)) == ([], [])

    def test_overlong_script_warns(self, validate_reels, write_config):
        long_script = "clips " * (validate_reels.WORDS_WARN + 1)
        _, warns = validate_reels.check(write_config(config(script=long_script)))
        assert any("long for a reel" in w for w in warns)

    @pytest.mark.parametrize(
        "over",
        [
            {"name": "yt-01-freetier"},
            {"name": "b01-notes-yt"},
            {"name": "01-clips", "scene": "scene-yt.html"},
        ],
    )
    def test_youtube_cuts_skip_the_reel_only_rules(
        self, validate_reels, write_config, over
    ):
        cfg = config(
            keyword="NONE",
            highlight=["unicorn"],
            script="a long form youtube script " * 30,
            **over,
        )
        assert validate_reels.check(write_config(cfg, name=cfg["name"])) == ([], [])


class TestMain:
    def run(self, validate_reels, monkeypatch, argv):
        monkeypatch.setattr(validate_reels.sys, "argv", ["validate-reels.py"] + argv)
        return validate_reels.main()

    def test_clean_directory_exits_zero(
        self, validate_reels, write_config, monkeypatch, capsys
    ):
        write_config(config())
        assert self.run(validate_reels, monkeypatch, []) == 0
        assert "1 config(s): 1 clean" in capsys.readouterr().out

    def test_broken_config_exits_nonzero_and_prints_errors(
        self, validate_reels, write_config, monkeypatch, capsys
    ):
        write_config(config(palette="neon"))
        assert self.run(validate_reels, monkeypatch, []) == 1
        out = capsys.readouterr().out
        assert "ERROR" in out
        assert "1 broken" in out

    def test_warnings_alone_still_exit_zero(
        self, validate_reels, write_config, monkeypatch, capsys
    ):
        write_config(config(highlight=["unicorn"]))
        assert self.run(validate_reels, monkeypatch, []) == 0
        out = capsys.readouterr().out
        assert "warn" in out
        assert "1 with warnings" in out

    def test_errors_are_printed_together_with_warnings(
        self, validate_reels, write_config, monkeypatch, capsys
    ):
        write_config(config(palette="neon", highlight=["unicorn"]))
        self.run(validate_reels, monkeypatch, [])
        out = capsys.readouterr().out
        assert "ERROR" in out and "warn" in out

    def test_test_scaffolds_are_skipped(
        self, validate_reels, write_config, monkeypatch, capsys
    ):
        write_config(config(name="03-mascot-test", palette="neon"))
        assert self.run(validate_reels, monkeypatch, []) == 0
        assert "1 test scaffold(s) skipped" in capsys.readouterr().out

    def test_named_targets_are_checked_with_or_without_the_extension(
        self, validate_reels, write_config, monkeypatch, capsys
    ):
        write_config(config())
        write_config(config(name="04-edit", palette="neon"), name="04-edit")
        assert self.run(validate_reels, monkeypatch, ["01-clips"]) == 0
        assert self.run(validate_reels, monkeypatch, ["01-clips.json"]) == 0
        assert self.run(validate_reels, monkeypatch, ["04-edit"]) == 1

    def test_unknown_target_is_reported(
        self, validate_reels, write_config, monkeypatch, capsys
    ):
        write_config(config())
        assert self.run(validate_reels, monkeypatch, ["ghost"]) == 1
        assert "no such config: ghost.json" in capsys.readouterr().out

    def test_empty_reels_directory_is_reported(
        self, validate_reels, write_config, monkeypatch, capsys
    ):
        assert self.run(validate_reels, monkeypatch, []) == 1
        assert "no configs found" in capsys.readouterr().out


class TestRealConfigs:
    """The configs committed to studio/reels/ must stay renderable."""

    def test_every_committed_config_passes(self, validate_reels):
        broken = {}
        for path in sorted(validate_reels.REELS.glob("*.json")):
            if path.stem.endswith(validate_reels.TEST_SUFFIXES):
                continue
            errs, _ = validate_reels.check(path)
            if errs:
                broken[path.name] = errs
        assert broken == {}
