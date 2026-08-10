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
import urllib.error
import urllib.parse

from common import (
    STATE_DIR,
    die,
    read_json,
    request_json,
    require_env,
    write_json,
)

QUEUE = STATE_DIR / "queue.json"

API_VERSION = os.environ.get("IG_API_VERSION", "v21.0")
GRAPH = f"https://graph.facebook.com/{API_VERSION}"

# Instagram allows 25 published posts per rolling 24h. Staying well under it is
# free; hitting it makes every later call fail for hours.
DAILY_CAP = 25

# Transcode wait. Long clips genuinely take minutes; failing fast would leave
# containers stranded and look like a broken token.
POLL_INTERVAL = 5
POLL_TIMEOUT = 300


def graph(path, params=None, method="GET"):
    """One Graph API call. Surfaces Instagram's own error text, not a traceback."""
    params = dict(params or {})
    params["access_token"] = TOKEN
    url = f"{GRAPH}/{path}"
    query = urllib.parse.urlencode(params)

    try:
        if method == "GET":
            return request_json(f"{url}?{query}", timeout=60)
        return request_json(url, data=query.encode(), timeout=60)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body).get("error", {})
            code = err.get("code")
            message = err.get("message", body)
        except ValueError:
            code, message = None, body

        if code == 190:
            die(
                "Instagram rejected the access token (code 190).\n"
                "  Long-lived tokens expire after ~60 days. Generate a new one in\n"
                "  the Meta app dashboard and update the IG_ACCESS_TOKEN secret."
            )
        if code == 4 or code == 32:
            die(
                f"Instagram rate limit reached (code {code}).\n"
                "  Nothing was lost -- approved clips stay queued. Re-run later."
            )
        die(f"Graph API error on {path}\n  code={code}\n  {message}")
    except urllib.error.URLError as e:
        die(f"Could not reach the Graph API: {e.reason}")


def load_queue():
    queue = read_json(QUEUE)
    if queue is None:
        die(f"No queue at {QUEUE}\n  Nothing has been rendered or approved yet.")
    return queue


def remaining_quota():
    """Ask Instagram how many posts are left in the rolling 24h window."""
    try:
        res = graph(f"{IG_USER}/content_publishing_limit",
                    {"fields": "quota_usage"})
        used = res.get("data", [{}])[0].get("quota_usage", 0)
        return max(0, DAILY_CAP - int(used))
    except SystemExit:
        raise
    except Exception:
        # Non-fatal: the cap is a safety net, not the point of the run.
        print("  (could not read publishing quota; assuming capacity)")
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
            die(
                f"Instagram could not process the video.\n"
                f"  {res.get('status', 'no detail given')}\n"
                "  Usual causes: the URL is not publicly fetchable, the file is not\n"
                "  MP4/H.264+AAC, or the aspect ratio is outside 0.01:1--10:1."
            )
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

    die(
        f"Container {creation_id} still not FINISHED after {POLL_TIMEOUT}s.\n"
        "  It may finish later -- re-run and it will be retried as a new upload."
    )


def publish_one(clip_id, entry):
    """Container -> wait -> publish. Returns the live media id."""
    video_url = entry.get("url")
    if not video_url or not str(video_url).startswith("http"):
        print(f"  skip {clip_id}: no public URL on this entry")
        return None

    caption = (entry.get("caption") or "").strip()

    print(f"  creating container for {clip_id}")
    container = graph(
        f"{IG_USER}/media",
        {"media_type": "REELS", "video_url": video_url, "caption": caption[:2200]},
        method="POST",
    )
    creation_id = container.get("id")
    if not creation_id:
        die(f"No container id returned for {clip_id}: {container}")

    print(f"  waiting for transcode ({creation_id})")
    wait_for_container(creation_id)

    print("  publishing")
    published = graph(
        f"{IG_USER}/media_publish", {"creation_id": creation_id}, method="POST"
    )
    media_id = published.get("id")
    if not media_id:
        die(f"Publish returned no media id for {clip_id}: {published}")
    return media_id


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would publish, change nothing")
    ap.add_argument("--limit", type=int, default=5,
                    help="max clips to publish this run (default 5)")
    args = ap.parse_args()

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
    for clip_id, entry in ready[:budget]:
        print(f"\n{clip_id}")
        media_id = publish_one(clip_id, entry)
        if not media_id:
            continue

        entry["published_at"] = int(time.time())
        entry["ig_media_id"] = media_id
        # Save after each success. A crash mid-batch must never re-post what
        # already went live.
        write_json(QUEUE, queue)
        published += 1
        print(f"  ✓ live as {media_id}")

    print(f"\npublished {published} clip(s)")


if __name__ == "__main__":
    # Resolved after arg parsing would be tidier, but every path below needs
    # them and failing here gives a clearer message than failing mid-batch.
    if "--dry-run" in sys.argv:
        IG_USER = os.environ.get("IG_USER_ID", "dry")
        TOKEN = os.environ.get("IG_ACCESS_TOKEN", "dry")
    else:
        IG_USER = require_env(
            "IG_USER_ID",
            "This is the Instagram *Business account* id (numeric), not your @handle.\n"
            "  Get it from the Meta app dashboard or the Graph API Explorer.",
        )
        TOKEN = require_env(
            "IG_ACCESS_TOKEN",
            "A long-lived Instagram Graph API token with instagram_content_publish.\n"
            "  Short-lived tokens expire in an hour and will fail overnight.",
        )
    main()
