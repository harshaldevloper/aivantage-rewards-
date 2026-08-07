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

## 6. Instagram auto-publish (30–45 min, do it last)

`publish.py` and the `Publish approved clips` workflow are built. What's left is
the Meta side, which is the slow part. **Do this only after step 4 works** —
otherwise you're debugging two unfamiliar systems at once.

Nothing here weakens the approval gate. `publish.py` refuses any clip that is
not `status="approved"`, and only your tap in Telegram writes that status.

### 6a. Get the Instagram side eligible

1. Instagram app → **Settings → Account type** → switch to **Business** (or
   Creator). A personal account cannot use the publishing API at all.
2. Link it to a **Facebook Page**. Create an empty one if you don't have one —
   it never needs a post, it just has to exist.

### 6b. Create the Meta app

At [developers.facebook.com](https://developers.facebook.com) → **My Apps →
Create App** → type **Business**. Add the **Instagram** product.

Request these permissions:

| Permission | Why |
|---|---|
| `instagram_basic` | Read the account |
| `instagram_content_publish` | **The one that actually posts.** Without it every call 403s. |
| `pages_show_list` | Find the linked Page |
| `pages_read_engagement` | Resolve the Page → IG account link |

> Meta reshuffles this dashboard often. If the labels don't match, search their
> docs for "Instagram Content Publishing API" — the permission names above are
> stable even when the UI isn't.

### 6c. Get the two values you need

- **`IG_USER_ID`** — the numeric Instagram *Business account* id, not your
  `@handle`. The Graph API Explorer shows it via
  `me/accounts?fields=instagram_business_account`.
- **`IG_ACCESS_TOKEN`** — a **long-lived** token. The default one expires in an
  hour; exchange it for the ~60-day version. A short-lived token here works when
  you test it and fails silently overnight, which is a miserable thing to debug.

Add both as repository secrets, same place as the Telegram ones.

> **Diary note:** long-lived tokens still expire in ~60 days. When publishing
> starts failing with `code 190`, that's all this is — generate a new token and
> update the secret. `publish.py` says so explicitly in the error.

### 6d. Prove it before trusting it

```bash
python publish.py --dry-run
```

Runs with no credentials and touches nothing — it just lists which approved
clips would post. Then from **Actions → Publish approved clips → Run workflow**,
tick **dry_run** for a cloud dry run, and only untick it once the list looks
right.

After that it runs itself at 10:00 and 18:00 IST, publishing at most 3 per run.

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
