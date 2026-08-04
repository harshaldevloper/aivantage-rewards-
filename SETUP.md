# Setup — 20 minutes, no subscriptions

Everything here runs on free tiers: GitHub Actions (2,000 min/month), the
Telegram Bot API (free), and Content Rewards' own public feed (no key).

Do these in order. Steps 1–4 get the campaign watcher live. Step 5 is only
needed when you want cloud rendering.

---

## 1. Create the Telegram bot (5 min)

1. Open Telegram, search **@BotFather**, hit Start.
2. Send `/newbot`.
3. Give it a name (`aivantage rewards`) and a username ending in `bot`
   (`aivantage_rewards_bot`).
4. BotFather replies with a token like `8123456789:AAH...`. **Copy it.**

That token is a password for the bot. Don't paste it into a chat, a commit, or
a screenshot — it goes only into GitHub Secrets in step 3.

## 2. Get your chat ID (2 min)

1. Send your new bot any message (`hi`) — a bot cannot message you first.
2. Open this in a browser, with your token pasted in:

   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`

3. Find `"chat":{"id":123456789` — that number is your chat ID.

## 3. Push the repo and add secrets (5 min)

Create an **empty private repo** on GitHub called `aivantage-rewards`, then:

```bash
cd ~/aivantage-rewards && git add -A && git commit -m "feat: content rewards pipeline" && git branch -M main && git remote add origin https://github.com/<YOUR_USERNAME>/aivantage-rewards.git && git push -u origin main
```

Then in the repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add two:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from step 1 |
| `TELEGRAM_CHAT_ID` | the number from step 2 |

## 4. Turn the watcher on (2 min)

**Actions** tab → enable workflows if prompted → **Campaign watcher** → **Run
workflow**.

Within a minute Telegram should show the AI-niche campaigns ranked by what they
actually pay. After that it runs itself at 09:00 IST daily.

If nothing arrives, open the failed run and check the log — a wrong chat ID
gives `chat not found`, a wrong token gives `401 Unauthorized`.

---

## 5. Cloud rendering (only when you want it)

The `Render clips` workflow installs ffmpeg, edge-tts and Puppeteer on the
runner, renders your reel configs, publishes the MP4s as release assets, and
sends up to 5 to Telegram with Approve / Reject buttons.

**Actions → Render clips → Run workflow**, then fill in:

- **reels** — `01-clips 04-edit` (config names from `studio/reels/`, no `.json`)
- **campaign** — e.g. `Dreamina AI UGC`
- **cpm** — e.g. `10.00`

Your taps get collected by the `Collect approvals` workflow every 15 minutes.
Check what's approved any time with:

```bash
python approve.py status
```

> First render is slow (~10 min) because Puppeteer downloads Chromium. Later
> runs are faster.

---

## 6. Instagram auto-publish — deliberately not built yet

Publishing needs the Instagram Graph API, which requires a Business/Creator
account linked to a Facebook Page plus a Meta app with
`instagram_content_publish`. That's a 30–45 minute setup and it's worth doing
**after** you've confirmed the approval flow feels right — otherwise you're
debugging two unfamiliar systems at once.

Until then the loop is: approve in Telegram → download the release asset →
post manually. That keeps a human on the publish button, which is also the
safest place to be while the account is young.

---

## Cost check

| Piece | Cost |
|---|---|
| Campaign watcher | free — seconds/day of Actions quota |
| Approval polling | free — ~96 short runs/day, well inside 2,000 min |
| Rendering | free — ~10 min/batch |
| Telegram | free |
| Content Rewards feed | free, public, no key |

Rendering is the only thing that meaningfully consumes quota. At ~10 min a
batch you get roughly 200 batches a month before hitting the free ceiling.
