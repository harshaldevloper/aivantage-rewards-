# aivantage-rewards

Cloud pipeline for earning from Content Rewards campaigns on **@aivantage_ai**,
without running anything locally and without paying for a subscription.

New here? Start with [SETUP.md](SETUP.md).

## The loop

```
Content Rewards public feed
        │  watch.py — filter to AI niche, rank by real payout
        ▼
   Telegram digest ......................... daily, 09:00 IST
        │  you pick a campaign
        ▼
   Render clips workflow ................... studio/ on a GitHub runner
        │  MP4s published as release assets (public URLs, free)
        ▼
   Telegram: 5 clips, Approve / Reject ..... nothing posts without a tap
        │  approve.py poll, every 15 min
        ▼
   Approved queue
        │  publish.py — Instagram Graph API, 10:00 and 18:00 IST
        ▼
   Live on the account ..................... published markers committed back
```

## Why it's shaped this way

**The campaign feed is public.** `contentrewards.com/api/discover-temp` returns
every live campaign as JSON with no auth, so there's no scraper to maintain and
no Apify bill.

**Ranking is not just CPM.** A $10 CPM with $200 of budget left is worse than a
$2 CPM with $24,000 left, and a campaign that pays nothing under 28,000 views is
useless to an account that does 2,000. `score()` in `watch.py` weighs CPM
against remaining budget and penalises unreachable view floors.

**Approval stays human.** Content Rewards requires genuinely new videos, allows
each video once, and rejects submissions that look duplicated across clippers.
A tap per clip is what keeps a batch from getting voided — and it's the
difference between automation and a content farm, which platforms actively
demote.

**State lives in git.** Verdicts and seen-campaign IDs are committed by the
workflows. No database, nothing to host, full history for free.

## Files

| Path | What it does |
|---|---|
| `watch.py` | Pulls campaigns, filters, ranks, sends the digest |
| `approve.py` | `send` / `poll` / `status` — the approval gate |
| `publish.py` | Posts approved clips to Instagram; `--dry-run` is safe |
| `studio/` | Reel renderer: edge-tts → Puppeteer frames → ffmpeg |
| `.github/workflows/watch.yml` | Daily digest |
| `.github/workflows/render.yml` | Cloud render + review batch (manual trigger) |
| `.github/workflows/approve.yml` | Drains taps every 15 min |
| `.github/workflows/publish.yml` | Publishes approved clips, twice daily |
| `state/` | `seen.json`, `queue.json` — committed by CI |

## Tuning the filter

Environment variables, override in the workflow or locally:

| Var | Default | Meaning |
|---|---|---|
| `MIN_CPM` | `1.0` | Ignore anything paying less per 1k views |
| `MAX_MIN_VIEWS` | `5000` | Ignore campaigns whose payout floor you can't hit |
| `MIN_BUDGET` | `500` | Ignore near-exhausted budgets |
| `PLATFORM` | `instagram` | Which payout row to read |

Raise `MAX_MIN_VIEWS` as the account grows — more campaigns unlock as your
typical reel climbs past their floors.

```bash
python watch.py --dry-run
```

Prints the digest to your terminal without touching Telegram. Good for checking
filter changes.

## Note on the studio copy

`studio/` is a copy of `~/.claude/aivantage/studio`, minus `node_modules`,
render output, and `client_secret.json` — that Google OAuth secret must never
reach GitHub. The copy's only code change is a cross-platform Python path so it
runs on Linux runners. Improvements to the renderer still belong upstream; sync
them over deliberately.
