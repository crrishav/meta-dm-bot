# Deploying to Render

## 1. Push to GitHub

Already done if you're reading this from the repo. If not:

```bash
git init
git add -A
git commit -m "meta dm bot"
gh repo create <name> --private --source=. --remote=origin --push
```

`.env` is gitignored and will NOT be pushed - secrets go into Render's own
variable store instead, in step 3.

## 2. Create the service

[dashboard.render.com](https://dashboard.render.com) -> **New -> Blueprint**
-> connect the GitHub repo. Render reads `render.yaml` from the repo root and
sets up the web service automatically (Python runtime, free plan, correct
start command). Give it a name and click **Apply**.

If you'd rather set it up by hand instead of using the Blueprint: **New ->
Web Service**, connect the repo, and set:

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Plan: Free

## 3. Set the environment variables

Service -> **Environment** -> add each of these. Copy the values straight
out of your local `.env`:

```
META_APP_ID
META_APP_SECRET
META_VERIFY_TOKEN
GRAPH_VERSION
META_IG_ID
META_IG_TOKEN
META_IG_APP_SECRET
GOOGLE_API_KEY
GEMINI_MODEL
PERSONA_FILE
DEBOUNCE_SECONDS
HANDOFF_HOURS
MAX_HISTORY
SUPABASE_URL
SUPABASE_SERVICE_KEY
```

Do not set `PORT` - Render injects it.

No persistent disk needed. Every stateful thing (dedup, mute state,
conversation history) lives in Supabase, not on Render's filesystem - which
matters because Render's free tier wipes local disk on every spin-down.

## 4. Get the public URL and confirm it's alive

Render gives you a permanent URL like `https://kazi-dm-bot.onrender.com`.
Check `https://<your-domain>/health` in a browser - it should return
`{"ok":true}`.

## 5. Point Meta at it

Two separate places need the new URL - both matter, confirmed the hard way:

**a) App-level webhook subscription** (Meta for Developers -> your app ->
Use cases -> Instagram API -> Customize -> Webhooks -> Instagram product
row):

- Callback URL: `https://<your-domain>/webhook`
- Verify token: same string as `META_VERIFY_TOKEN`
- **Verify and save**
- Subscribe to the `messages` and `messaging_referral` fields

**b) The Instagram-Login-specific webhook box** (same app -> Use cases ->
Instagram API -> Customize -> **API setup with Instagram login** -> step 3
"Configure webhooks"): this is the one that actually gates live delivery for
this product - paste the same Callback URL and Verify token here too, and
**Verify and save**.

Skipping (b) is why messages can silently never arrive even though (a) looks
fully configured and verifies fine.

## 6. Keep it awake

Render's free tier spins down after 15 minutes with no traffic, and the
filesystem resets every time it does. A cold request after that takes about
a minute to wake back up - too slow for Meta's webhook delivery, which can
give up and mark you as unresponsive if you don't answer fast.

Fix: [UptimeRobot](https://uptimerobot.com) (free) -> **Add New Monitor** ->
HTTP(s) -> URL `https://<your-domain>/health` -> interval 5 minutes. That
keeps a request landing well under the 15-minute spin-down window, so the
service basically never sleeps.

## Updating the bot's knowledge

`persona.md` is deployed with the code, so changing what the bot knows means
editing that file and pushing. Render redeploys automatically on push to the
connected branch.
