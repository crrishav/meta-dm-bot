"""Webhook server for Messenger + Instagram DM auto-reply.

Meta pushes every message to POST /webhook. We acknowledge in milliseconds and
do the slow work (the model call, Send API) on a background task - if we take longer
than a few seconds to return 200, Meta retries and eventually unsubscribes us.
"""

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from . import brain, dashboard, meta, store, supa
from .brain import assess_value, describe_media, draft_reply
from .config import DEBOUNCE_SECONDS, HANDOFF_HOURS, VERIFY_TOKEN

# Sent instead of an AI reply when the customer's message is just a photo or
# voice note with no typed text - we don't show media to the model anymore,
# so a person needs to look at it themselves.
MEDIA_HOLDING_REPLY = "Thanks for sending that - one of our team will take a look and reply shortly."
MEDIA_ATTACHMENT_TYPES = {"image", "audio"}

# How often a thread's lead-value score is allowed to refresh - keeps the
# Gemini scoring call proportional to real activity, not one per message.
VALUE_COOLDOWN_MINUTES = 15

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bot")

# Bumped every time a customer sends something. A queued reply checks whether
# it is still the newest before spending a model call - this is what stops the
# bot answering each line of a three-line message separately.
_arrival_counter: dict[str, int] = defaultdict(int)
# One reply at a time per conversation.
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
# Images/voice notes seen during the debounce window, drained by whichever
# task ends up replying - so "[image][a second later] any size discounts?"
# still lets the model see the picture, even though only the second
# message's task runs.
_pending_media: dict[str, list[str]] = defaultdict(list)
# Whether the message that triggered the currently-running debounce task was
# a bare photo/voice note (no typed text) - checked by the winning task to
# decide between a real reply and MEDIA_HOLDING_REPLY.
_media_only: dict[str, bool] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    log.info("ready")
    yield
    await meta.aclose()
    await supa.aclose()
    await store.aclose()
    await dashboard.aclose()
    await brain.aclose()


app = FastAPI(lifespan=lifespan)

# The dashboard API is called from the browser (the website's Leads tab), so
# it needs actual CORS headers - unlike /webhook, which only Meta's servers
# ever call. Wide open on origin because the real gate is the bearer token
# `require_leads_view`/`require_leads_edit` check on every route: without a
# valid, permitted session, an allowed origin buys an attacker nothing.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(dashboard.router, prefix="/dashboard")


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/webhook")
async def verify(request: Request) -> Response:
    """Meta calls this once when you save the webhook URL."""
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge", ""))
    log.warning("webhook verification rejected")
    return PlainTextResponse("forbidden", status_code=403)


@app.post("/webhook")
async def receive(request: Request) -> Response:
    raw = await request.body()
    if not meta.verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        log.warning("bad signature - dropping payload")
        return PlainTextResponse("forbidden", status_code=403)

    payload = await request.json()
    platform = "instagram" if payload.get("object") == "instagram" else "messenger"

    for entry in payload.get("entry", []):
        entry_id = entry.get("id", "")
        for event in entry.get("messaging", []):
            asyncio.create_task(_safe_handle(platform, entry_id, event))

    # Always 200, even on garbage - a non-200 makes Meta retry the whole batch.
    return PlainTextResponse("ok")


async def _safe_handle(platform: str, entry_id: str, event: dict) -> None:
    try:
        await _handle(platform, entry_id, event)
    except Exception:
        log.exception("failed handling event")


MAX_MEDIA_PER_TURN = 3
# Types Gemini can actually be shown; a picture is a picture whether it came
# in as a raw upload or as a shared post/reel/story mention.
VIEWABLE_ATTACHMENT_TYPES = {"image", "audio", "story_mention", "share", "post", "ig_post", "reel", "ig_reel"}


def _extract_text(event: dict) -> Optional[str]:
    message = event.get("message") or {}
    if message.get("text"):
        return message["text"]
    if message.get("attachments"):
        labels = []
        for a in message["attachments"]:
            kind = a.get("type", "file")
            title = (a.get("payload") or {}).get("title")
            labels.append(f"{kind} - {title}" if title else kind)
        return f"[customer sent: {', '.join(labels)}]"
    postback = event.get("postback") or {}
    return postback.get("title") or postback.get("payload")


def _extract_reply_to(event: dict) -> Optional[str]:
    """The mid of the message this one quoted, if the customer tapped
    "reply" on a specific earlier message rather than just sending fresh."""
    message = event.get("message") or {}
    return (message.get("reply_to") or {}).get("mid")


def _extract_media_urls(event: dict) -> list[str]:
    """URLs worth showing Gemini - a shared post/reel/story is just as much
    "an image the customer is pointing at" as a raw upload."""
    message = event.get("message") or {}
    urls = []
    for attachment in message.get("attachments") or []:
        if attachment.get("type") in VIEWABLE_ATTACHMENT_TYPES:
            url = (attachment.get("payload") or {}).get("url")
            if url:
                urls.append(url)
    return urls


def _extract_referral(event: dict) -> Optional[str]:
    """A short human-readable note on how this thread started, if Meta sent
    one - a paid ad click, an ig.me shortlink, or anything else with a
    'referral' object. Not just ads: any source is worth knowing."""
    referral = event.get("referral")
    if not referral:
        return None
    ad_title = (referral.get("ads_context_data") or {}).get("ad_title")
    if ad_title:
        return f"{ad_title} (ad {referral.get('ad_id', '')})"
    if referral.get("ad_id"):
        return f"ad {referral['ad_id']}"
    source = referral.get("source", "unknown source")
    ref = referral.get("ref")
    return f"{source} link (ref: {ref})" if ref else source


async def _handle_echo(platform: str, entry_id: str, event: dict, message: dict) -> None:
    """A message we did not send went out from the Page - a teammate is
    handling this lead. Go quiet rather than talking over them.

    Meta echoes our own outgoing messages back to this same webhook, and
    that echo can arrive before our own record_sent_mid() write for it has
    landed - a real race, not a hypothetical one. A single failed check
    must not be enough to conclude "human sent this"; give our own
    bookkeeping a moment to catch up before believing it.
    """
    mid = message.get("mid", "")
    if not mid:
        return
    if await store.was_sent_by_us(mid):
        return
    await asyncio.sleep(1.5)
    if await store.was_sent_by_us(mid):
        return
    customer_id = (event.get("recipient") or {}).get("id")
    if not customer_id:
        return
    convo = f"{platform}:{entry_id}:{customer_id}"
    text = message.get("text", "")
    await store.add_message(convo, "assistant", text, mid=mid)
    await store.mute(convo, HANDOFF_HOURS)
    log.info("human replied in %s - muting bot for %sh", convo, HANDOFF_HOURS)


async def _handle(platform: str, entry_id: str, event: dict) -> None:
    message = event.get("message") or {}

    # An echo is a copy of an outgoing message, so it arrives *from* the Page.
    # It must be checked before the self-loop guard below, which would
    # otherwise drop it.
    if message.get("is_echo"):
        await _handle_echo(platform, entry_id, event, message)
        return

    sender_id = (event.get("sender") or {}).get("id")
    if not sender_id or sender_id == entry_id:
        return

    convo = f"{platform}:{entry_id}:{sender_id}"

    referral = _extract_referral(event)
    if referral:
        await store.set_referral(convo, referral)

    # Delivery receipts, read receipts, reactions.
    if not message and "postback" not in event:
        return

    mid = message.get("mid")
    if mid and not await store.claim_mid(mid):
        return  # redelivery

    text = _extract_text(event)
    if not text:
        return

    reply_to_mid = _extract_reply_to(event)
    await store.add_message(convo, "user", text, referral=referral, reply_to_mid=reply_to_mid, mid=mid)
    asyncio.create_task(_maybe_assess_value(convo, entry_id, sender_id))
    # Trimmed to the cap on every append (not just at send time) so a muted
    # conversation can't accumulate an unbounded list over a long mute.
    pending = _pending_media[convo] + _extract_media_urls(event)
    _pending_media[convo] = pending[-MAX_MEDIA_PER_TURN:]

    attachment_types = {a.get("type") for a in message.get("attachments") or []}
    _media_only[convo] = bool(attachment_types & MEDIA_ATTACHMENT_TYPES) and not message.get("text")

    if await store.is_muted(convo):
        log.info("%s is muted - logged but not replying", convo)
        return

    _arrival_counter[convo] += 1
    my_turn = _arrival_counter[convo]

    # Give them a moment to finish typing the rest of their thought.
    await asyncio.sleep(DEBOUNCE_SECONDS)
    if _arrival_counter[convo] != my_turn:
        return  # a newer message arrived; that task will answer

    async with _locks[convo]:
        await meta.send_typing(entry_id, sender_id)
        name = await meta.profile_name(entry_id, sender_id)

        media_urls = _pending_media.pop(convo, [])
        downloads = [await meta.download_media(url) for url in media_urls]
        media = [item for item in downloads if item is not None]
        for data, mime_type in media:
            asyncio.create_task(_archive_media(convo, data, mime_type))

        if _media_only.pop(convo, False):
            # A bare photo/voice note - we no longer show media to the model,
            # so hand this straight to a person rather than guessing at it.
            reply, handoff = MEDIA_HOLDING_REPLY, True
        else:
            history = await store.history(convo)
            reply, handoff = await draft_reply(history, name, await store.get_referral(convo))

        sent_mid = await meta.send_text(entry_id, sender_id, reply[:1900], platform)
        if sent_mid:
            await store.record_sent_mid(sent_mid)
        await store.add_message(convo, "assistant", reply, mid=sent_mid)

        if handoff:
            await store.mute(convo, HANDOFF_HOURS)
            await notify_team(convo, await store.history(convo))


async def _maybe_assess_value(convo: str, entry_id: str, sender_id: str) -> None:
    """Score how much this lead looks worth chasing - fire-and-forget, same
    as _archive_media. Rate limited via store.should_assess_value so a fast
    back-and-forth doesn't spend a Gemini call on every single line."""
    try:
        if not await store.should_assess_value(convo, VALUE_COOLDOWN_MINUTES):
            return
        name = await meta.profile_name(entry_id, sender_id)
        history = await store.history(convo)
        result = await assess_value(history, name, await store.get_referral(convo))
        if result:
            await store.set_value(convo, result["score"], result["tier"], result["reasons"])
    except Exception:
        log.exception("value assessment failed for %s", convo)


async def _archive_media(convo: str, data: bytes, mime_type: str) -> None:
    """Re-host an attachment in our own storage and log it - Meta's CDN URLs
    expire, so this is the only copy that is still around in 3 months. Logged
    with an actual transcript/description, not just a type label, so the
    archive is searchable without anyone opening the raw file."""
    path = await supa.upload_media(convo, data, mime_type)
    if not path:
        return
    description = await describe_media(data, mime_type)
    content = description or f"[attachment: {mime_type}]"
    await supa.log_message(convo, "user", content, media_type=mime_type, media_path=path)


async def notify_team(convo: str, history: list[dict]) -> None:
    """Wire this to Slack, email, or your CRM - it fires when a lead is hot or
    stuck and a person needs to pick it up."""
    log.warning("HANDOFF NEEDED in %s | last: %s", convo, history[-1]["content"][:200])
