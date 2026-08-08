#!/usr/bin/env python3
"""Check every reel config before you burn runner minutes rendering it.

    python3 validate-reels.py              # check all of reels/
    python3 validate-reels.py c07-receipt  # check specific ones

Exists because a bad config fails *inside* make-reel.js, several minutes into a
render, with a stack trace about an undefined property rather than "your palette
is misspelled". This catches the whole class of that in under a second, with no
AI needed to interpret it.

Stdlib only. Rules are derived from scene-mascot.html and the configs that have
already rendered successfully -- if you add a palette or pose to the scene, add
it here too.
"""
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
REELS = HERE / "reels"

PALETTES = {"cinema", "ember", "mint", "violet"}
POSES = {"idle", "present", "point", "hush", "shrug", "cheer"}
EXPRS = {"neutral", "wide", "happy", "sly"}
ACTS = {"edit", "slides", "video", "process"}

# `scene` is deliberately not required: make-reel.js falls back to scene.html
# (see its `cfg.scene || 'scene.html'`).
REQUIRED = ["name", "keyword", "script"]

# Scaffolds used to eyeball a single shot, not real reels. They legitimately
# have half the fields missing, so checking them only produces noise.
TEST_SUFFIXES = ("-test",)

# ~150 wpm at rate +8%. Instagram cuts attention hard after about 30s, and the
# demo window in these configs ends around 11.5s, so a very long script leaves
# the mascot talking over a finished demo.
WORDS_WARN = 75


def check(path):
    """Return (errors, warnings) for one config."""
    errs, warns = [], []

    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return [f"not valid JSON: {e}"], []

    for key in REQUIRED:
        if not cfg.get(key):
            errs.append(f"missing required field: {key}")
    if errs:
        return errs, warns

    if cfg["name"] != path.stem:
        errs.append(f'name "{cfg["name"]}" does not match filename "{path.stem}"')

    scene_name = cfg.get("scene", "scene.html")
    if not (HERE / scene_name).exists():
        errs.append(f"scene file not found: {scene_name}")

    pal = cfg.get("palette", "cinema")
    if pal not in PALETTES:
        errs.append(f'palette "{pal}" unknown — pick one of {sorted(PALETTES)}')

    demo = cfg.get("demo") or {}
    if demo:
        act = demo.get("act")
        if act not in ACTS:
            errs.append(f'demo.act "{act}" unknown — pick one of {sorted(ACTS)}')
        start, end = demo.get("start"), demo.get("end")
        if start is not None and end is not None and start >= end:
            errs.append(f"demo.start ({start}) must be before demo.end ({end})")

    for i, beat in enumerate(cfg.get("beats", [])):
        if beat.get("pose") not in POSES:
            errs.append(f'beats[{i}].pose "{beat.get("pose")}" unknown')
        if beat.get("expr") not in EXPRS:
            errs.append(f'beats[{i}].expr "{beat.get("expr")}" unknown')

    # YouTube cuts are a different format: long-form, rendered through
    # scene-yt.html, and with no comment-keyword mechanic (they carry
    # keyword "NONE"). Applying the reel rules to them is all false positives.
    is_yt = (
        cfg.get("scene") == "scene-yt.html"
        or cfg["name"].startswith("yt-")
        or cfg["name"].endswith("-yt")
    )

    # Substring, not tokenised. Python's \w excludes Devanagari combining marks
    # (the virama and the matras), so re.findall would split "फ्री" into "फ" and
    # "र" and then report it as missing from a script it is plainly in. A plain
    # substring check answers the real question -- "does this word appear?" --
    # identically in every script.
    script = cfg["script"].lower()

    # `hot` is a global emphasis dictionary reused across configs; words that
    # don't occur are simply not emphasised. Only `highlight` is authored per
    # script, so only that one is worth flagging.
    missing = [w for w in cfg.get("highlight", []) if w.lower() not in script]
    if missing and not is_yt:
        warns.append(f"highlight words never appear in script: {missing}")

    kw = cfg.get("keyword", "")
    if not is_yt:
        if kw and kw.lower() not in script:
            errs.append(
                f'keyword "{kw}" is never spoken in the script — nobody will type it'
            )
        if kw and kw != kw.upper():
            warns.append(f'keyword "{kw}" is not uppercase')

    wc = len(cfg["script"].split())
    if not is_yt and wc > WORDS_WARN:
        warns.append(f"script is {wc} words — long for a reel, consider trimming")

    return errs, warns


def main():
    targets = sys.argv[1:]
    if targets:
        paths = [REELS / (t if t.endswith(".json") else f"{t}.json") for t in targets]
        missing = [p for p in paths if not p.exists()]
        if missing:
            print("no such config: " + ", ".join(p.name for p in missing))
            return 1
    else:
        paths = sorted(REELS.glob("*.json"))

    if not paths:
        print(f"no configs found in {REELS}")
        return 1

    bad = warned = skipped = 0
    for p in paths:
        if p.stem.endswith(TEST_SUFFIXES):
            skipped += 1
            continue
        errs, warns = check(p)
        if errs:
            bad += 1
            print(f"\n✗ {p.name}")
            for e in errs:
                print(f"    ERROR  {e}")
            for w in warns:
                print(f"    warn   {w}")
        elif warns:
            warned += 1
            print(f"\n~ {p.name}")
            for w in warns:
                print(f"    warn   {w}")

    total = len(paths) - skipped
    clean = total - bad - warned
    tail = f", {skipped} test scaffold(s) skipped" if skipped else ""
    print(f"\n{total} config(s): {clean} clean, {warned} with warnings, {bad} broken{tail}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
