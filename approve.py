#!/usr/bin/env python3
"""Telegram approval gate for finished clips.

Nothing publishes without a tap. `send` pushes rendered clips out with
Approve/Reject buttons; `poll` picks up the taps on a schedule and records the
verdict. Publishing reads only what `poll` marked approved.

    python approve.py send --manifest out/manifest.json
    python approve.py poll

Stdlib only.
"""

import argparse
import json
import os
import time
from pathlib import Path

from common import (
    STATE_DIR,
    read_json,
    telegram,
    telegram_chat_id,
    utf8_stdout,
    write_json,
)

QUEUE = STATE_DIR / "queue.json"
OFFSET = STATE_DIR / "tg_offset.json"

# Telegram caps a media group / burst; five is also about as many clips as a
# person will actually review carefully in one sitting.
BATCH = int(os.environ.get("APPROVE_BATCH", "5"))


def keyboard(clip_id):
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"ok:{clip_id}"},
                    {"text": "❌ Reject", "callback_data": f"no:{clip_id}"},
                ]
            ]
        }
    )


def cmd_send(args):
    """Read a manifest of rendered clips and push a review batch."""
    clips = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    queue = read_json(QUEUE, {})

    pending = [c for c in clips if c["id"] not in queue][: args.batch]
    if not pending:
        print("nothing new to review")
        return

    for clip in pending:
        cpm = clip.get("cpm")
        caption = (
            f"<b>{clip.get('campaign', 'clip')}</b>\n"
            f"{clip.get('caption', '')}\n\n"
            + (f"<i>${cpm}/1k · pays from "
               f"{clip.get('min_views', 0):,} views</i>" if cpm else "")
        )
        # Prefer uploading the bytes over handing Telegram a URL. GitHub release
        # downloads 302 to a signed objects.githubusercontent.com address, and
        # Telegram's fetcher fails that with "failed to get HTTP URL content"
        # even though the asset is public and returns 200 to curl.
        #
        # Direct upload allows 50MB (vs 20MB by URL), and these clips are ~8MB.
        # The public `url` is still recorded on the queue entry below, because
        # publish.py genuinely needs a fetchable URL for the Graph API.
        local = clip.get("file")
        if local and Path(local).exists():
            video, is_url = local, False
        else:
            video = clip.get("url")
            is_url = str(video).startswith("http")

        res = telegram(
            "sendVideo",
            {
                "chat_id": telegram_chat_id(),
                "caption": caption[:1024],
                "parse_mode": "HTML",
                "reply_markup": keyboard(clip["id"]),
                **({"video": video} if is_url else {}),
            },
            files=None if is_url else {"video": video},
        )

        queue[clip["id"]] = {
            "status": "pending",
            "campaign": clip.get("campaign"),
            "caption": clip.get("caption"),
            "url": clip.get("url"),
            "file": clip.get("file"),
            "message_id": res.get("result", {}).get("message_id"),
            "sent_at": int(time.time()),
        }
        print(f"sent {clip['id']}")
        time.sleep(1)  # stay clear of the Bot API rate limit

    write_json(QUEUE, queue)


def cmd_poll(args):
    """Drain callback taps and record each verdict."""
    queue = read_json(QUEUE, {})
    offset = read_json(OFFSET, {}).get("offset", 0)

    res = telegram(
        "getUpdates",
        {"offset": offset, "timeout": 0, "allowed_updates": '["callback_query"]'},
    )
    updates = res.get("result", [])
    if not updates:
        print("no taps")
        return

    changed = 0
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        cq = u.get("callback_query")
        if not cq:
            continue

        action, _, clip_id = cq.get("data", "").partition(":")
        entry = queue.get(clip_id)
        if not entry:
            telegram("answerCallbackQuery",
                     {"callback_query_id": cq["id"], "text": "Unknown clip"})
            continue

        entry["status"] = "approved" if action == "ok" else "rejected"
        entry["decided_at"] = int(time.time())
        changed += 1

        verdict = "Approved — queued to publish" if action == "ok" else "Rejected"
        telegram("answerCallbackQuery",
                 {"callback_query_id": cq["id"], "text": verdict})
        # Replace the buttons so a clip cannot be voted on twice.
        if entry.get("message_id"):
            telegram(
                "editMessageReplyMarkup",
                {
                    "chat_id": telegram_chat_id(),
                    "message_id": entry["message_id"],
                    "reply_markup": json.dumps(
                        {"inline_keyboard": [[{
                            "text": f"{'✅' if action == 'ok' else '❌'} {verdict}",
                            "callback_data": "done",
                        }]]}
                    ),
                },
            )

    write_json(QUEUE, queue)
    write_json(OFFSET, {"offset": offset})
    print(f"recorded {changed} verdict(s)")


def cmd_status(args):
    queue = read_json(QUEUE, {})
    counts = {}
    for e in queue.values():
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    print(json.dumps(counts, indent=1) if counts else "queue empty")
    for cid, e in queue.items():
        if e["status"] == "approved":
            print(f"  approved: {cid} -> {e.get('url') or e.get('file')}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="push a review batch to Telegram")
    s.add_argument("--manifest", default="out/manifest.json")
    s.add_argument("--batch", type=int, default=BATCH)
    s.set_defaults(fn=cmd_send)

    p = sub.add_parser("poll", help="collect approve/reject taps")
    p.set_defaults(fn=cmd_poll)

    st = sub.add_parser("status", help="show queue state")
    st.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    utf8_stdout()
    args.fn(args)


if __name__ == "__main__":
    main()
