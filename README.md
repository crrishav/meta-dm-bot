# Meta DM auto-reply bot

Replies to Instagram DM text messages using Groq, via the Instagram API with
Instagram Login (no linked Facebook Page required). A photo or voice note
with no typed text gets a short holding reply instead - the model is text
only for now - and the thread is handed to a person.

## How it works

```
Customer DMs your Instagram account
        |
        v
  Meta webhook  --POST-->  /webhook   (verify signature, return 200 in ms)
                              |
                              v
                    background task
                      - dedupe by message id (Supabase)
                      - wait 2s in case they are still typing
                      - download any photo/voice note, archive it permanently
                        in Supabase Storage (Meta's CDN links expire)
                      - a bare photo/voice note (no typed text) gets a
                        holding reply and an immediate handoff - no model call
                      - otherwise load recent history from Supabase and ask
                        Groq for a text reply
                      - send it back via the Graph API
                      - if it needs a person, mute the thread and alert
```

Everything stateful - dedup, mute state, conversation history - lives in
Supabase, not on local disk. That matters if you deploy anywhere with an
ephemeral filesystem (see `DEPLOY.md`): the bot survives a restart with zero
memory loss.

Three things it does that a naive bot gets wrong:

- **Echo detection.** When a teammate answers from the inbox directly, Meta
  sends us an echo of their message. The bot recognises it is not one of
  ours and goes quiet on that thread for 12 hours instead of talking over
  your staff.
- **Deduplication.** Meta retries webhooks it thinks failed. Message ids are
  claimed in Supabase so a retry cannot produce a second reply.
- **Debounce.** People send "hi" / "is this available" / "how much" as three
  separate messages (or a photo, then a voice note a few seconds later). The
  bot waits for the pause and answers once, with everything in context.

## Setup

### 1. Meta app configuration

In [developers.facebook.com](https://developers.facebook.com), your app ->
**Use cases -> Instagram API**:

1. Add the **Instagram API** use case (not Messenger - this bot uses
   Instagram API with Instagram Login, a standalone product that does not
   need a linked Facebook Page).
2. **Permissions and features**: `instagram_business_basic`,
   `instagram_business_manage_messages`. Standard Access is enough as long
   as the account you're messaging is added as an Instagram Tester on the
   app (App Roles -> Instagram Testers) and has accepted the invite.
3. **API setup with Instagram login** -> step 2 "Generate access tokens" ->
   generate a token for your account. Put it in `.env` as `META_IG_TOKEN`,
   and its id as `META_IG_ID`.
4. Same page -> **Instagram app secret** (Show button) - this is a
   *different* secret from your main App Secret. Put it in `.env` as
   `META_IG_APP_SECRET`. Meta signs webhook payloads with this one, not the
   main App Secret - mixing them up looks like a signature bug but isn't.

### 2. Set up Supabase

Run `supabase_schema.sql` in your project's SQL Editor - it creates every
table this bot uses (`ig_bot_*`, clearly separated from anything else in the
same project) plus a private storage bucket for media. Put your project URL
and **service role** key (not the anon key - this is a trusted backend) in
`.env` as `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`.

### 3. Run the server

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt
cp .env.example .env                                 # then fill it in
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Edit `persona.md` before going live. The bot is instructed never to state a
fact that is not in that file - anything missing becomes a handoff instead
of an invented answer.

### 4. Expose it over HTTPS

Meta pushes to you; there is no polling endpoint, so the server must be
publicly reachable with a valid certificate. See `DEPLOY.md` for Render. For
local development:

```bash
cloudflared tunnel --url http://localhost:8000
```

### 5. Point Meta at it - **two places, not one**

**a) App-level:** Use cases -> Instagram API -> Customize -> **Webhooks** ->
select the **Instagram** product row -> Callback URL + Verify token ->
Verify and save -> subscribe to `messages` and `messaging_referral`.

**b) Product-level (the one that actually matters for delivery):** same
Customize section -> **API setup with Instagram login** -> step 3 "Configure
webhooks" -> the *same* Callback URL + Verify token -> Verify and save.

Confirmed the hard way: (a) alone will pass verification and look fully
configured while never actually delivering a single message. Only (b) turns
on live delivery for this product. Do both.

Then self-subscribe the account (one-time, from a Python shell with the venv
active):

```python
import httpx, os
from dotenv import load_dotenv
load_dotenv()
httpx.post(
    f"https://graph.instagram.com/v21.0/{os.environ['META_IG_ID']}/subscribed_apps",
    params={"subscribed_fields": "messages,messaging_referral", "access_token": os.environ["META_IG_TOKEN"]},
)
```

DM the account from a tester account. You should see the reply.

## Things that will bite you

| Symptom | Cause |
|---|---|
| Webhook verification fails | `META_VERIFY_TOKEN` does not match what you typed in the dashboard |
| Verification passes, nothing ever arrives | You only configured webhook (a) above, not (b) - see step 5 |
| 403 "bad signature" on every POST | You're signing/checking against `META_APP_SECRET` instead of `META_IG_APP_SECRET` - they are different secrets |
| Voice note attachment fails to download | Instagram serves voice notes as `video/mp4`, not `audio/*` - `download_media` in `app/meta.py` already accounts for this, but if you touch that check, keep it |
| Token stops working after ~1 hour | You saved a short-lived token. Exchange it, or just regenerate from the API setup page - it's quick |
| Replies silently fall back to the generic message | Check `GROQ_API_KEY`/`GROQ_MODEL` and Groq's status/rate limits at `console.groq.com` - `draft_reply` in `app/brain.py` falls back and hands off on any Groq error |
| Gemini quota exhausted fast | Only affects lead-value scoring and archive descriptions now, not replies. Some Gemini model IDs (notably the newest ones) get a tiny free-tier daily cap - `gemini-2.5-flash` has a far higher one for the same capability, check `aistudio.google.com` |
| Replies stop after a day | The 24-hour messaging window closed - you may only reply within 24h of the customer's last message (or use a `messaging_referral`-tagged first contact) |

## Cost

Each reply is roughly 1-2k input tokens and well under 400 output, via Groq
(`GROQ_MODEL` in `.env`, default `openai/gpt-oss-120b`). Lead-value
scoring and archive descriptions still run on Gemini's free tier - check
current rate limits at aistudio.google.com since volume can outrun them.

## Where to take it next

- Persist leads to your CRM in `notify_team()` in `app/main.py`.
- `app/store.py` and `app/supa.py` are the only files that touch Supabase -
  swap the backend there if you ever outgrow it.
