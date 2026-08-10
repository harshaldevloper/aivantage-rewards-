#!/usr/bin/env python3
"""Content Rewards campaign watcher for @aivantage_ai.

Pulls the public campaign feed, keeps only the ones worth an AI-niche account's
time, and posts a digest to Telegram. Stdlib only -- no pip install, no paid API.

    python watch.py            # send digest to Telegram
    python watch.py --dry-run  # print to stdout instead
"""

import argparse
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from common import PipelineError, fail, read_json, request_json, warn, write_json

FEED = "https://contentrewards.com/api/discover-temp"
STATE = Path(__file__).parent / "state" / "seen.json"

# Campaigns worth posting about on an AI-tools account. Category alone is too
# narrow -- plenty of AI SaaS campaigns get filed under "Product".
KEYWORDS = re.compile(
    r"\b(a\.?i\.?|artificial intelligence|gpt|llm|chatbot|automation|saas|"
    r"agent|prompt|no.?code|productivity|workflow|app builder|video gen|"
    r"image gen|voice|copilot|assistant)\b",
    re.I,
)

# Below this many views a submission earns nothing, so a campaign demanding more
# than a new account can realistically hit is noise.
MAX_MIN_VIEWS = int(os.environ.get("MAX_MIN_VIEWS", "5000"))
MIN_CPM = float(os.environ.get("MIN_CPM", "1.0"))
MIN_BUDGET = float(os.environ.get("MIN_BUDGET", "500"))
PLATFORM = os.environ.get("PLATFORM", "instagram")


def money(s):
    """'$17,070.77' -> 17070.77, or None when the feed sends something else.

    None rather than 0.0 on purpose: a budget that could not be parsed is not a
    budget of zero, and the caller has to decide whether to drop the campaign or
    say so out loud.
    """
    if s is None:
        return None
    digits = re.sub(r"[^\d.]", "", str(s))
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def fetch(campaign_type):
    qs = urllib.parse.urlencode(
        {"limit": 200, "type": campaign_type, "sort": "Highest Budget"}
    )
    req = urllib.request.Request(
        f"{FEED}?{qs}",
        headers={"accept": "application/json", "user-agent": "Mozilla/5.0"},
    )
    payload = request_json(req, timeout=30, label=f"campaign feed ({campaign_type})")
    if not isinstance(payload, dict):
        raise PipelineError(
            f"campaign feed returned {type(payload).__name__}, expected an object"
        )
    campaigns = payload.get("campaigns")
    if campaigns is None:
        # The feed is public and unversioned, so a shape change shows up here
        # first. Silently ranking zero campaigns would look like a quiet day.
        raise PipelineError(
            f"campaign feed has no 'campaigns' key (got {sorted(payload)[:8]}). "
            "The public API shape probably changed."
        )
    if not isinstance(campaigns, list):
        raise PipelineError("campaign feed 'campaigns' is not a list")
    return campaigns


def payout_for(campaign, platform):
    """The payout terms for our platform, or None if the campaign skips it."""
    for p in campaign.get("payouts") or []:
        if isinstance(p, dict) and p.get("platform") == platform:
            return p
    return None


def relevant(campaign):
    text = f"{campaign.get('title','')} {campaign.get('description','')}"
    return campaign.get("category") == "Technology" or bool(KEYWORDS.search(text))


def number(value, default=0.0):
    """Feed fields arrive as strings, nulls and occasionally junk."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def score(campaign, payout, budget):
    """Rank by earnings potential, penalising hard-to-reach view floors and
    near-exhausted budgets -- a $10 CPM with $200 left is not an opportunity."""
    cpm = number(payout.get("pricePerThousandViews"))
    min_views = number(payout.get("minViewsRequired"), 1) or 1
    reach_penalty = min(1.0, 2000 / max(min_views, 1))
    return cpm * reach_penalty * min(budget / 5000, 3.0)


def evaluate():
    seen = set(read_json(STATE, default=[], what="seen-campaign list") or [])

    picks = []
    unparseable = 0
    for kind in ("Clipping", "UGC"):
        for c in fetch(kind):
            if not isinstance(c, dict) or not c.get("id") or not c.get("title"):
                unparseable += 1
                continue
            if not relevant(c):
                continue
            p = payout_for(c, PLATFORM)
            if not p:
                continue
            cpm = number(p.get("pricePerThousandViews"))
            if cpm < MIN_CPM:
                continue
            if number(p.get("minViewsRequired")) > MAX_MIN_VIEWS:
                continue
            budget = money(c.get("budgetRemaining"))
            if budget is None:
                # Keep it, flagged: dropping a campaign because one field is
                # oddly formatted is how a good campaign goes unnoticed.
                warn(f"{c['title'][:40]!r}: unreadable budget "
                     f"{c.get('budgetRemaining')!r} — keeping it, unranked on budget")
                budget = MIN_BUDGET
            elif budget < MIN_BUDGET:
                continue
            c["_payout"] = p
            c["_kind"] = kind
            c["_score"] = score(c, p, budget)
            c["_new"] = c["id"] not in seen
            picks.append(c)

    if unparseable:
        warn(f"{unparseable} feed entrie(s) had no id/title and were skipped")

    picks.sort(key=lambda c: c["_score"], reverse=True)
    return picks


def esc(s):
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def render(picks, limit=8):
    if not picks:
        return "<b>Content Rewards</b>\nNo campaigns matched your filters today."

    fresh = [c for c in picks if c["_new"]]
    head = f"<b>Content Rewards — {len(picks)} live for AI niche</b>"
    if fresh:
        head += f"\n{len(fresh)} new since last check."

    lines = [head, ""]
    for c in picks[:limit]:
        p = c["_payout"]
        cpm = number(p.get("pricePerThousandViews"))
        mv = int(number(p.get("minViewsRequired")))
        cap = p.get("maxPayoutPerSubmission")
        tag = "🆕 " if c["_new"] else ""
        lines.append(f"{tag}<b>{esc(c['title'][:60])}</b>")
        lines.append(
            f"   ${cpm:.2f}/1k · {esc(c.get('budgetRemaining', 'unknown'))} left · "
            f"{c['_kind'].lower()}"
        )
        lines.append(
            f"   pays from {mv:,} views"
            + (f" · cap ${cap}/clip" if cap else "")
            + f" · at 1k views = ${cpm:.2f}"
        )
        lines.append("")
    lines.append("https://contentrewards.com")
    return "\n".join(lines)


def send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    body = urllib.parse.urlencode(
        {
            "chat_id": chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body
    )
    res = request_json(req, timeout=30, label="Telegram sendMessage")
    # Telegram answers some failures with HTTP 200 and ok=false. Treating that
    # as sent is what marks every campaign 'seen' for a digest nobody received.
    if not isinstance(res, dict) or not res.get("ok"):
        raise PipelineError(
            "Telegram accepted the request but reported a failure: "
            f"{res.get('description') if isinstance(res, dict) else res}"
        )
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--new-only", action="store_true",
                    help="stay silent unless a campaign is new")
    args = ap.parse_args()

    # Windows consoles default to cp1252 and choke on the emoji in the digest.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        picks = evaluate()
    except PipelineError as e:
        fail(str(e))

    if args.new_only and not any(c["_new"] for c in picks):
        print("no new campaigns; staying quiet")
        return

    text = render(picks)
    if args.dry_run:
        plain = re.sub(r"<[^>]+>", "", text)
        print(plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
        return

    # Order matters: marking campaigns as seen before the digest is delivered
    # would suppress them forever on the strength of a message that never sent.
    try:
        send(text)
        write_json(STATE, sorted(c["id"] for c in picks))
    except PipelineError as e:
        fail(str(e))
    print(f"sent digest: {len(picks)} campaigns")


if __name__ == "__main__":
    main()
