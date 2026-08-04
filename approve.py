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
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).parent
QUEUE = ROOT / "state" / "queue.json"
OFFSET = ROOT / "state" / "tg_offset.json"
API = "https://api.telegram.org/bot{token}/{method}"

# Telegram caps a media group / burst; five is also about as many clips as a
# person will actually review carefully in one sitting.
BATCH = int(os.environ.get("APPROVE_BATCH", "5"))


def token():
    t = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not t:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    return t


def chat_id():
    c = os.environ.get("TELEGRAM_CHAT_ID")
    if not c:
        sys.exit("TELEGRAM_CHAT_ID is not set")
    return c


def call(method, params=None, files=None):
    """POST to the Bot API. Uses multipart only when uploading a local file."""
    url = API.format(token=token(), method=method)
    params = {k: v for k, v in (params or {}).items() if v is not None}

    if not files:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data)
    else:
        boundary = uuid.uuid4().hex
        body = bytearray()
        for k, v in params.items():
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
            ).encode()
        for field, path in files.items():
            path = Path(path)
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field}";'
                f' filename="{path.name}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode()
            body += path.read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=bytes(body))
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit(f"Telegram {method} failed: {e.code} {detail}")


def load(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1), encoding="utf-8")


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
    queue = load(QUEUE, {})

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
        video = clip.get("url") or clip.get("file")
        is_url = str(video).startswith("http")

        res = call(
            "sendVideo",
            {
                "chat_id": chat_id(),
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

    save(QUEUE, queue)


def cmd_poll(args):
    """Drain callback taps and record each verdict."""
    queue = load(QUEUE, {})
    offset = load(OFFSET, {}).get("offset", 0)

    res = call(
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
            call("answerCallbackQuery",
                 {"callback_query_id": cq["id"], "text": "Unknown clip"})
            continue

        entry["status"] = "approved" if action == "ok" else "rejected"
        entry["decided_at"] = int(time.time())
        changed += 1

        verdict = "Approved — queued to publish" if action == "ok" else "Rejected"
        call("answerCallbackQuery",
             {"callback_query_id": cq["id"], "text": verdict})
        # Replace the buttons so a clip cannot be voted on twice.
        if entry.get("message_id"):
            call(
                "editMessageReplyMarkup",
                {
                    "chat_id": chat_id(),
                    "message_id": entry["message_id"],
                    "reply_markup": json.dumps(
                        {"inline_keyboard": [[{
                            "text": f"{'✅' if action == 'ok' else '❌'} {verdict}",
                            "callback_data": "done",
                        }]]}
                    ),
                },
            )

    save(QUEUE, queue)
    save(OFFSET, {"offset": offset})
    print(f"recorded {changed} verdict(s)")


def cmd_status(args):
    queue = load(QUEUE, {})
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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args.fn(args)


if __name__ == "__main__":
    main()
