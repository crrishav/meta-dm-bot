# Deploying to Railway

## 1. Push to a git repo

```bash
git init
git add .
git commit -m "meta dm bot"
```

Push to GitHub, then in Railway: **New Project -> Deploy from GitHub repo**.

`.env` is gitignored and will NOT be pushed. That is deliberate - secrets go in
Railway's own variable store instead.

## 2. Set the variables

Railway project -> **Variables** -> paste these (Railway has a "Raw editor" that
accepts the whole block at once):

```
META_APP_ID=...
META_APP_SECRET=...
META_PAGE_TOKEN=...
META_VERIFY_TOKEN=...
GRAPH_VERSION=v26.0
GOOGLE_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
PERSONA_FILE=persona.md
DEBOUNCE_SECONDS=2
HANDOFF_HOURS=12
MAX_HISTORY=20
DB_PATH=/data/bot.db
```

Do not set `PORT` - Railway injects it.

## 3. Add a volume (important)

Railway wipes the filesystem on every deploy. Without a volume you lose every
conversation and every processed-message id each time you push, which means the
bot re-replies to old messages.

Railway project -> your service -> **Volumes** -> **New Volume**, mount path
`/data`. That matches the `DB_PATH=/data/bot.db` above.

## 4. Get the public URL

**Settings -> Networking -> Generate Domain.** You get something like
`kazi-bot-production.up.railway.app`.

Your webhook callback URL is that plus `/webhook`:

```
https://kazi-bot-production.up.railway.app/webhook
```

Check it is alive: open `https://<your-domain>/health` in a browser - it should
return `{"ok":true}`.

## 5. Point Meta at it

App Dashboard -> Use cases -> Messenger -> **Messenger API Settings**:

- Callback URL: the `/webhook` URL above
- Verify token: the same string as `META_VERIFY_TOKEN`
- **Verify and save**
- Then **Add Subscriptions** on the Page row: `messages`,
  `messaging_postbacks`, `message_echoes`, `messaging_referrals`

Do the same under **Instagram settings** for `messages`.

## Updating the bot's knowledge

`persona.md` is deployed with the code, so changing what the bot knows means
editing that file and pushing. Railway redeploys automatically.
