# Meta DM auto-reply bot

Answers Instagram and Messenger DMs from people who clicked your ads, using
Google's Gemini. One Python service handles both platforms - they share the same
webhook, the same Page token, and the same Send API.

## How it works

```
Customer DMs your Page/IG
        |
        v
  Meta webhook  --POST-->  /webhook   (verify signature, return 200 in ms)
                              |
                              v
                    background task
                      - dedupe by message id
                      - wait 2s in case they are still typing
                      - load last 20 turns from SQLite
                      - ask Gemini for a reply
                      - send it back via Graph API
                      - if it needs a person, mute the thread and alert
```

Three things it does that a naive bot gets wrong:

- **Echo detection.** When a teammate answers from the Page inbox, Meta sends
  us an echo of their message. The bot recognises it is not one of ours and
  goes quiet on that thread for 12 hours instead of talking over your staff.
- **Deduplication.** Meta retries webhooks it thinks failed. Message ids are
  claimed in SQLite so a retry cannot produce a second reply.
- **Debounce.** People send "hi" / "is this available" / "how much" as three
  messages. The bot waits for the pause and answers once, with all three in
  context.

## Setup

### 1. Meta app configuration

In the App Dashboard for your app:

1. **Add products:** Messenger, and Instagram (Instagram messaging via
   Facebook Login).
2. **Link your Page** under Messenger -> Settings, and confirm the Instagram
   professional account is connected to that same Page.
3. **Permissions you need:**
   - `pages_messaging`, `pages_manage_metadata`, `pages_show_list`,
     `pages_read_engagement` (Messenger)
   - `instagram_basic`, `instagram_manage_messages` (Instagram)
4. **App Review + Business Verification.** Until these are approved your app
   has Standard Access, which means it can only message people who hold a role
   on the app. Add yourself as a tester and build against that; submit for
   Advanced Access in parallel, because verification takes days to weeks.

### 2. Get a Page token

Generate a user token in the Graph API Explorer with the permissions above,
then:

```bash
python tools/get_page_token.py <APP_ID> <APP_SECRET> <EXPLORER_TOKEN>
```

Put the printed `META_PAGE_TOKEN` in `.env`. It does not expire, as long as the
exchange succeeded - treat it like a password.

### 3. Run the server

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt
cp .env.example .env                                 # then fill it in
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Edit `persona.md` before going live. The bot is instructed never to state a
fact that is not in that file - anything missing becomes a handoff instead of
an invented answer.

### 4. Expose it over HTTPS

Meta pushes to you; there is no polling endpoint, so the server must be
publicly reachable with a valid certificate. Deploy to Render / Railway / Fly /
a VPS behind nginx. For local development:

```bash
cloudflared tunnel --url http://localhost:8000
```

### 5. Point Meta at it

In the dashboard, under Webhooks:

- Callback URL: `https://your-domain/webhook`
- Verify token: the same random string you put in `META_VERIFY_TOKEN`
- Subscribe the **page** object to: `messages`, `messaging_postbacks`,
  `message_echoes`, `messaging_referrals`
- Subscribe the **instagram** object to: `messages`

Then subscribe the Page itself - the dashboard step alone is not enough:

```bash
python tools/subscribe_page.py <PAGE_ID>
```

DM your Page from an account that has a tester role. You should see the reply.

## Things that will bite you

| Symptom | Cause |
|---|---|
| Webhook verification fails | `META_VERIFY_TOKEN` does not match what you typed in the dashboard |
| 403 on every POST | `META_APP_SECRET` is wrong, or a proxy is re-encoding the body before the signature check |
| Nothing arrives at all | `subscribe_page.py` was never run, or the Page is not linked to the app |
| "No matching user found" on send | You are still on Standard Access and that person is not a tester |
| Works for an hour then dies | You put the Explorer token in `.env` instead of the Page token |
| Replies stop after a day | The 24-hour messaging window closed - you may only reply within 24h of the customer's last message |

## Cost

Each reply is roughly 1-2k input tokens and under 100 output.
`gemini-2.5-flash` has a free tier that is generous enough for early testing -
check current rate limits at aistudio.google.com, since a busy ad campaign can
outrun them. Set `GEMINI_MODEL` in `.env` to switch models.

## Where to take it next

- Persist leads to your CRM in `notify_team()` in `app/main.py`.
- Read `messaging_referrals` to learn which ad each lead came from, and put
  that in the prompt.
- Swap SQLite for Postgres if you run more than one instance - the `store`
  module is the only file that touches the database.
