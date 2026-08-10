#!/usr/bin/env python3
"""Publish approved clips to Instagram as Reels.

    python publish.py --dry-run     # show what would post, touch nothing
    python publish.py               # actually publish
    python publish.py --limit 3     # publish at most 3 this run

Reads `state/queue.json`, takes every entry `approve.py poll` marked
"approved" that has not been published yet, and posts it. Publishing writes
`published_at` and `ig_media_id` back onto the entry, so a re-run never
double-posts -- the workflow commits that file, and that commit is the record.

WHY THIS IS SAFE TO RUN ON A SCHEDULE
Only "approved" entries are eligible. A clip reaches that state solely because
you tapped Approve in Telegram. This script cannot decide to post something on
its own, which is deliberate: Content Rewards voids duplicated submissions, and
platforms demote accounts that post unattended. The tap stays.

HOW INSTAGRAM PUBLISHING ACTUALLY WORKS (three calls, not one)
  1. POST /{ig_user}/media          -> creates a container, returns creation_id
  2. GET  /{creation_id}?status_code -> Instagram transcodes; wait for FINISHED
  3. POST /{ig_user}/media_publish  -> the post goes live
Step 2 is the one people miss. Publishing a container that is still IN_PROGRESS
fails, so this polls until it is ready or gives up loudly.

Stdlib only, same as watch.py and approve.py, so the workflow needs no install
step and cannot break from a dependency update.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from common import HttpError, PipelineError, read_json, request_json, write_json

ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "state" / "queue.json"

API_VERSION = os.environ.get("IG_API_VERSION", "v21.0")
GRAPH = f"https://graph.facebook.com/{API_VERSION}"

# Instagram allows 25 published posts per rolling 24h. Staying well under it is
# free; hitting it makes every later call fail for hours.
DAILY_CAP = 25

# Transcode wait. Long clips genuinely take minutes; failing fast would leave
# containers stranded and look like a broken token.
POLL_INTERVAL = 5
POLL_TIMEOUT = 300

# Set in main() once the arguments say whether credentials are needed at all.
IG_USER = ""
TOKEN = ""


class FatalError(PipelineError):
    """Nothing later in the run can succeed either -- stop the whole batch.

    A dead token or an exhausted rate limit is fatal. A single clip Instagram
    will not transcode is not: the rest of the batch is still publishable.
    """


class ClipError(PipelineError):
    """This clip failed. Others may still publish, but the run is not a success."""


def die(msg):
    print(f"\n✗ {msg}\n", file=sys.stderr)
    sys.exit(1)


def env(name, hint):
    val = os.environ.get(name, "").strip()
    if not val:
        die(f"{name} is not set.\n  {hint}")
    return val


def graph(path, params=None, method="GET"):
    """One Graph API call. Surfaces Instagram's own error text, not a traceback."""
    params = dict(params or {})
    params["access_token"] = TOKEN
    url = f"{GRAPH}/{path}"

    if method == "GET":
        req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}")
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode())

    try:
        res = request_json(req, timeout=60, label=f"Graph API {path}")
    except HttpError as e:
        try:
            err = json.loads(e.body).get("error", {})
            code = err.get("code")
            message = err.get("message", e.body)
        except ValueError:
            code, message = None, e.body

        if code == 190:
            raise FatalError(
                "Instagram rejected the access token (code 190).\n"
                "  Long-lived tokens expire after ~60 days. Generate a new one in\n"
                "  the Meta app dashboard and update the IG_ACCESS_TOKEN secret."
            ) from e
        if code in (4, 32):
            raise FatalError(
                f"Instagram rate limit reached (code {code}).\n"
                "  Nothing was lost -- approved clips stay queued. Re-run later."
            ) from e
        raise ClipError(f"Graph API error on {path}\n  code={code}\n  {message}") from e

    if not isinstance(res, dict):
        raise ClipError(f"Graph API returned {type(res).__name__} for {path}")
    # A 200 carrying an `error` object is still a failure, and reading only the
    # field we wanted turns it into a None that surfaces much later.
    if "error" in res:
        raise ClipError(f"Graph API error on {path}\n  {res['error']}")
    return res


def load_queue():
    queue = read_json(QUEUE, what="publish queue")
    if queue is None:
        raise FatalError(
            f"No queue at {QUEUE}\n  Nothing has been rendered or approved yet."
        )
    if not isinstance(queue, dict):
        raise FatalError(f"{QUEUE} should hold an object keyed by clip id.")
    return queue


def save_queue(queue):
    # Atomic: this file is the only record of what already went live, and a
    # truncated one means the next run re-posts it.
    write_json(QUEUE, queue)


def remaining_quota():
    """Ask Instagram how many posts are left in the rolling 24h window."""
    try:
        res = graph(f"{IG_USER}/content_publishing_limit",
                    {"fields": "quota_usage"})
        used = (res.get("data") or [{}])[0].get("quota_usage", 0)
        return max(0, DAILY_CAP - int(used))
    except FatalError:
        # A dead token or a rate limit is not a quota-reading hiccup.
        raise
    except (PipelineError, LookupError, TypeError, ValueError) as e:
        # Non-fatal: the cap is a safety net, not the point of the run. Still
        # say why, so "assuming capacity" is never the whole story in the log.
        print(f"  (could not read publishing quota: {e}; assuming capacity)")
        return DAILY_CAP


def wait_for_container(creation_id):
    """Block until Instagram finishes transcoding, or explain why it never will."""
    waited = 0
    while waited < POLL_TIMEOUT:
        res = graph(creation_id, {"fields": "status_code,status"})
        status = res.get("status_code")

        if status == "FINISHED":
            return True
        if status == "ERROR":
            raise ClipError(
                f"Instagram could not process the video.\n"
                f"  {res.get('status', 'no detail given')}\n"
                "  Usual causes: the URL is not publicly fetchable, the file is not\n"
                "  MP4/H.264+AAC, or the aspect ratio is outside 0.01:1--10:1."
            )
        if status is None:
            raise ClipError(
                f"Container {creation_id} reported no status_code: {res}"
            )
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

    raise ClipError(
        f"Container {creation_id} still not FINISHED after {POLL_TIMEOUT}s.\n"
        "  It may finish later -- re-run and it will be retried as a new upload."
    )


def publish_one(clip_id, entry):
    """Container -> wait -> publish. Returns the live media id."""
    video_url = entry.get("url")
    if not video_url or not str(video_url).startswith("http"):
        raise ClipError(
            f"{clip_id} has no public URL — the Graph API can only fetch one.\n"
            "  Render it again so the workflow attaches a release asset."
        )

    caption = (entry.get("caption") or "").strip()

    print(f"  creating container for {clip_id}")
    container = graph(
        f"{IG_USER}/media",
        {"media_type": "REELS", "video_url": video_url, "caption": caption[:2200]},
        method="POST",
    )
    creation_id = container.get("id")
    if not creation_id:
        raise ClipError(f"No container id returned for {clip_id}: {container}")

    print(f"  waiting for transcode ({creation_id})")
    wait_for_container(creation_id)

    print("  publishing")
    published = graph(
        f"{IG_USER}/media_publish", {"creation_id": creation_id}, method="POST"
    )
    media_id = published.get("id")
    if not media_id:
        # The post may or may not exist. Say so rather than marking it published
        # (which would hide a missing post) or unpublished (which risks a double).
        raise ClipError(
            f"Publish returned no media id for {clip_id}: {published}\n"
            "  Check the account before re-running — this clip is not marked "
            "published."
        )
    return media_id


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would publish, change nothing")
    ap.add_argument("--limit", type=int, default=5,
                    help="max clips to publish this run (default 5)")
    args = ap.parse_args()

    global IG_USER, TOKEN
    if args.dry_run:
        IG_USER, TOKEN = os.environ.get("IG_USER_ID", "dry"), "dry"
    else:
        # Resolved before anything else: failing here is clearer than failing
        # mid-batch with half the clips published.
        IG_USER = env(
            "IG_USER_ID",
            "This is the Instagram *Business account* id (numeric), not your @handle.\n"
            "  Get it from the Meta app dashboard or the Graph API Explorer.",
        )
        TOKEN = env(
            "IG_ACCESS_TOKEN",
            "A long-lived Instagram Graph API token with instagram_content_publish.\n"
            "  Short-lived tokens expire in an hour and will fail overnight.",
        )

    queue = load_queue()
    ready = [
        (cid, e) for cid, e in queue.items()
        if e.get("status") == "approved" and not e.get("published_at")
    ]

    if not ready:
        print("nothing approved and unpublished — done")
        return

    print(f"{len(ready)} clip(s) approved and awaiting publish")

    if args.dry_run:
        publishable = 0
        for cid, e in ready:
            url = e.get("url")
            if not url or not str(url).startswith("http"):
                # Would be skipped at publish time. Say so here rather than
                # letting a real run be the first place you find out.
                print(f"  SKIP {cid}")
                print("    no public URL on this entry — render it again so the")
                print("    workflow attaches a release asset")
                continue
            publishable += 1
            print(f"  would publish {cid}")
            print(f"    url     {url}")
            print(f"    caption {(e.get('caption') or '')[:70]}")
        print(f"\ndry run — {publishable} would publish, nothing was changed")
        return

    quota = remaining_quota()
    budget = min(args.limit, quota, len(ready))
    if budget <= 0:
        print(f"daily publishing quota exhausted ({quota} left) — try later")
        return
    if budget < len(ready):
        print(f"publishing {budget} of {len(ready)} this run (quota/limit)")

    published = 0
    failures = []
    for clip_id, entry in ready[:budget]:
        print(f"\n{clip_id}")
        try:
            media_id = publish_one(clip_id, entry)
        except ClipError as e:
            # One unusable clip should not strand the rest of the batch, but it
            # must still colour the exit code -- a green run that published
            # nothing is the failure mode this whole file exists to avoid.
            print(f"  ✗ {e}", file=sys.stderr)
            failures.append(clip_id)
            continue

        entry["published_at"] = int(time.time())
        entry["ig_media_id"] = media_id
        # Save after each success. A crash mid-batch must never re-post what
        # already went live.
        save_queue(queue)
        published += 1
        print(f"  ✓ live as {media_id}")

    print(f"\npublished {published} clip(s)")
    if failures:
        die(f"{len(failures)} clip(s) failed to publish: {', '.join(failures)}")


if __name__ == "__main__":
    try:
        main()
    except PipelineError as e:
        die(str(e))
