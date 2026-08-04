#!/usr/bin/env python3
"""Content Rewards campaign watcher for @aivantage_ai.

Pulls the public campaign feed, keeps only the ones worth an AI-niche account's
time, and posts a digest to Telegram. Stdlib only -- no pip install, no paid API.

    python watch.py            # send digest to Telegram
    python watch.py --dry-run  # print to stdout instead
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

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
    """'$17,070.77' -> 17070.77"""
    try:
        return float(re.sub(r"[^\d.]", "", str(s)) or 0)
    except ValueError:
        return 0.0


def fetch(campaign_type):
    qs = urllib.parse.urlencode(
        {"limit": 200, "type": campaign_type, "sort": "Highest Budget"}
    )
    req = urllib.request.Request(
        f"{FEED}?{qs}",
        headers={"accept": "application/json", "user-agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8")).get("campaigns", [])


def payout_for(campaign, platform):
    """The payout terms for our platform, or None if the campaign skips it."""
    for p in campaign.get("payouts", []):
        if p.get("platform") == platform:
            return p
    return None


def relevant(campaign):
    text = f"{campaign.get('title','')} {campaign.get('description','')}"
    return campaign.get("category") == "Technology" or bool(KEYWORDS.search(text))


def score(campaign, payout):
    """Rank by earnings potential, penalising hard-to-reach view floors and
    near-exhausted budgets -- a $10 CPM with $200 left is not an opportunity."""
    cpm = float(payout.get("pricePerThousandViews") or 0)
    budget = money(campaign.get("budgetRemaining"))
    min_views = payout.get("minViewsRequired") or 1
    reach_penalty = min(1.0, 2000 / max(min_views, 1))
    return cpm * reach_penalty * min(budget / 5000, 3.0)


def evaluate():
    seen = set()
    if STATE.exists():
        seen = set(json.loads(STATE.read_text(encoding="utf-8")))

    picks = []
    for kind in ("Clipping", "UGC"):
        for c in fetch(kind):
            if not relevant(c):
                continue
            p = payout_for(c, PLATFORM)
            if not p:
                continue
            cpm = float(p.get("pricePerThousandViews") or 0)
            if cpm < MIN_CPM:
                continue
            if (p.get("minViewsRequired") or 0) > MAX_MIN_VIEWS:
                continue
            if money(c.get("budgetRemaining")) < MIN_BUDGET:
                continue
            c["_payout"] = p
            c["_kind"] = kind
            c["_score"] = score(c, p)
            c["_new"] = c["id"] not in seen
            picks.append(c)

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
        cpm = float(p.get("pricePerThousandViews") or 0)
        mv = p.get("minViewsRequired") or 0
        cap = p.get("maxPayoutPerSubmission")
        tag = "🆕 " if c["_new"] else ""
        lines.append(f"{tag}<b>{esc(c['title'][:60])}</b>")
        lines.append(
            f"   ${cpm:.2f}/1k · {esc(c['budgetRemaining'])} left · "
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
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--new-only", action="store_true",
                    help="stay silent unless a campaign is new")
    args = ap.parse_args()

    # Windows consoles default to cp1252 and choke on the emoji in the digest.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    picks = evaluate()
    if args.new_only and not any(c["_new"] for c in picks):
        print("no new campaigns; staying quiet")
        return

    text = render(picks)
    if args.dry_run:
        plain = re.sub(r"<[^>]+>", "", text)
        print(plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
        return

    send(text)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps(sorted(c["id"] for c in picks), indent=1), encoding="utf-8"
    )
    print(f"sent digest: {len(picks)} campaigns")


if __name__ == "__main__":
    main()
