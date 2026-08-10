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
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from common import (
    ApiError,
    PipelineError,
    fail,
    read_json,
    request_json,
    warn,
    write_json,
)

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
    """POST to the Bot API and return the `result`.

    Raises rather than returning a half-answer: Telegram reports plenty of
    failures as HTTP 200 with `ok: false`, and a caller reading `result` out of
    one of those gets a None it will not notice until much later.
    """
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
            if not path.exists():
                raise PipelineError(f"{method}: file to upload is missing: {path}")
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

    res = request_json(req, timeout=120, label=f"Telegram {method}")
    if not isinstance(res, dict) or not res.get("ok"):
        raise ApiError(
            f"Telegram {method}",
            (res.get("description") if isinstance(res, dict) else res),
        )
    return res.get("result")


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
    manifest = Path(args.manifest)
    clips = read_json(manifest, what="render manifest")
    if clips is None:
        raise PipelineError(
            f"no manifest at {manifest}. The render step writes it; if the render\n"
            "  failed, there is nothing to review."
        )
    if not isinstance(clips, list):
        raise PipelineError(f"{manifest} should hold a list of clips")

    unidentified = [c for c in clips if not isinstance(c, dict) or not c.get("id")]
    if unidentified:
        raise PipelineError(
            f"{len(unidentified)} manifest entrie(s) have no id. Verdicts are keyed\n"
            "  by id, so sending these would produce clips nobody can approve."
        )

    queue = read_json(QUEUE, default={}, what="approval queue")

    pending = [c for c in clips if c["id"] not in queue][: args.batch]
    if not pending:
        print("nothing new to review")
        return

    sent = 0
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

        if not video:
            raise PipelineError(
                f"{clip['id']} has neither a local file nor a URL to send"
            )

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

        message_id = (res or {}).get("message_id")
        if not message_id:
            # Without it the buttons can never be retired, so a clip could be
            # voted on twice. Better to know now than at approval time.
            warn(f"{clip['id']}: Telegram returned no message_id; "
                 "its buttons cannot be retired after a verdict")

        queue[clip["id"]] = {
            "status": "pending",
            "campaign": clip.get("campaign"),
            "caption": clip.get("caption"),
            "url": clip.get("url"),
            "file": clip.get("file"),
            "message_id": message_id,
            "sent_at": int(time.time()),
        }
        sent += 1
        # Persist per clip, not per batch. A clip that reached Telegram but is
        # missing from the queue cannot be approved and gets sent again on the
        # next run, so a failure halfway must not discard the ones before it.
        write_json(QUEUE, queue)
        print(f"sent {clip['id']}")
        time.sleep(1)  # stay clear of the Bot API rate limit

    print(f"sent {sent} clip(s) for review")


def acknowledge(method, params, clip_id):
    """Cosmetic Bot API call: log a failure, never lose a verdict over one.

    A callback id expires after a few minutes, so a tap made just before this
    run started routinely fails to answer. The verdict itself is already
    recorded locally; aborting here would discard it and every one after it.
    """
    try:
        call(method, params)
        return True
    except PipelineError as e:
        warn(f"{clip_id}: {method} failed ({e}); verdict still recorded")
        return False


def cmd_poll(args):
    """Drain callback taps and record each verdict."""
    queue = read_json(QUEUE, default={}, what="approval queue")
    offset = (read_json(OFFSET, default={}, what="Telegram offset") or {}).get(
        "offset", 0
    )

    updates = call(
        "getUpdates",
        {"offset": offset, "timeout": 0, "allowed_updates": '["callback_query"]'},
    ) or []
    if not updates:
        print("no taps")
        return

    changed = 0
    try:
        for u in updates:
            cq = u.get("callback_query")
            # The offset only advances past an update this run has finished
            # with. Anything left unhandled is redelivered next time instead of
            # being acknowledged into the void.
            offset = max(offset, u["update_id"] + 1)
            if not cq:
                continue

            action, _, clip_id = cq.get("data", "").partition(":")
            entry = queue.get(clip_id)
            if not entry:
                acknowledge("answerCallbackQuery",
                            {"callback_query_id": cq["id"], "text": "Unknown clip"},
                            clip_id or "?")
                warn(f"tap for unknown clip {clip_id!r} — not on the queue")
                continue
            if action not in ("ok", "no"):
                acknowledge("answerCallbackQuery",
                            {"callback_query_id": cq["id"], "text": "Already decided"},
                            clip_id)
                continue

            entry["status"] = "approved" if action == "ok" else "rejected"
            entry["decided_at"] = int(time.time())
            changed += 1

            verdict = "Approved — queued to publish" if action == "ok" else "Rejected"
            acknowledge("answerCallbackQuery",
                        {"callback_query_id": cq["id"], "text": verdict}, clip_id)
            # Replace the buttons so a clip cannot be voted on twice.
            if entry.get("message_id"):
                acknowledge(
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
                    clip_id,
                )
    finally:
        # getUpdates does not redeliver once the offset moves on, so verdicts
        # collected before a failure have to be persisted even on the way out.
        write_json(QUEUE, queue)
        write_json(OFFSET, {"offset": offset})

    print(f"recorded {changed} verdict(s)")


def cmd_status(args):
    queue = read_json(QUEUE, default={}, what="approval queue")
    counts = {}
    for e in queue.values():
        status = e.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    print(json.dumps(counts, indent=1) if counts else "queue empty")
    for cid, e in queue.items():
        if e.get("status") == "approved":
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
    try:
        args.fn(args)
    except PipelineError as e:
        fail(str(e))


if __name__ == "__main__":
    main()
