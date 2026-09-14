"""Read/write API behind the website's Leads tab.

Staff read Instagram conversations and reply to them from the site instead of
the Instagram app itself. Every route here requires the same signed-in
session the rest of the site already runs on (Supabase Auth, or the Firebase
bridge for accounts still on their old password) - checked by handing the
caller's own bearer token to PostgREST and trusting its verdict, the exact
check the website's own Supabase client relies on for every query it makes.
No separate login or API key for staff to manage.

Sending a reply goes through the same Graph API call the bot itself uses
(`meta.send_text`), then mutes the thread - a human answering from here is a
takeover exactly like answering from the Instagram app is, and the bot should
go quiet for the same reason.
"""

import asyncio
import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from . import meta, store, supa
from .config import HANDOFF_HOURS, SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY, SUPABASE_URL

log = logging.getLogger(__name__)
router = APIRouter()

_http = httpx.AsyncClient(timeout=10.0)
_headers = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

# How many recent messages to scan when building the conversation list. Fine
# for a small business's DM volume; a conversation count that outgrows this
# is a good problem to have, and the moment to add a real "last message per
# convo" database view instead.
LIST_SCAN_LIMIT = 1000

# Instagram profile names never change mid-conversation, so a lookup is
# cached for the process lifetime rather than re-fetched on every poll.
_name_cache: dict[str, Optional[str]] = {}


async def require_staff(authorization: Optional[str] = Header(None)) -> None:
    """Trust whatever session the caller's browser is already using for the
    rest of the site. PostgREST verifies the token's signature and expiry for
    us on this one lightweight read - a 401 from Supabase means "not signed
    in", anything else means the token was good."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in to use the lead inbox.")
    token = authorization[len("Bearer ") :]
    try:
        response = await _http.get(
            f"{SUPABASE_URL}/rest/v1/ig_bot_threads",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"},
            params={"select": "convo", "limit": "1"},
        )
    except httpx.HTTPError as exc:
        log.warning("session check failed: %s", exc)
        raise HTTPException(503, "Could not verify your session - try again.")
    if response.status_code == 401:
        raise HTTPException(401, "Your session has expired - sign in again.")


def _split_convo(convo: str) -> tuple[str, str, str]:
    parts = convo.split(":", 2)
    if len(parts) != 3:
        raise HTTPException(400, "Not a recognised conversation id.")
    return parts[0], parts[1], parts[2]


async def _profile_name(convo: str, entry_id: str, sender_id: str) -> Optional[str]:
    if convo not in _name_cache:
        _name_cache[convo] = await meta.profile_name(entry_id, sender_id)
    return _name_cache[convo]


async def _fetch_threads() -> dict[str, dict]:
    response = await _http.get(
        f"{SUPABASE_URL}/rest/v1/ig_bot_threads",
        headers=_headers,
        params={"select": "convo,muted_until,referral"},
    )
    if response.status_code >= 400:
        log.error("thread list fetch failed (%s): %s", response.status_code, response.text)
        return {}
    return {row["convo"]: row for row in response.json()}


class ReplyBody(BaseModel):
    text: str


class TakeoverBody(BaseModel):
    muted: bool


@router.get("/leads", dependencies=[Depends(require_staff)])
async def list_leads():
    response = await _http.get(
        f"{SUPABASE_URL}/rest/v1/ig_bot_messages",
        headers=_headers,
        params={
            "select": "convo,role,content,media_type,created_at",
            "order": "created_at.desc",
            "limit": str(LIST_SCAN_LIMIT),
        },
    )
    if response.status_code >= 400:
        log.error("lead list fetch failed (%s): %s", response.status_code, response.text)
        raise HTTPException(502, "Could not load conversations from the archive.")

    last_by_convo: dict[str, dict] = {}
    for row in response.json():
        convo = row["convo"]
        if convo not in last_by_convo:
            last_by_convo[convo] = row

    threads = await _fetch_threads()

    async def build(convo: str, last: dict) -> dict:
        platform, entry_id, sender_id = convo.split(":", 2)
        thread = threads.get(convo, {})
        muted_until = thread.get("muted_until") or 0
        name = await _profile_name(convo, entry_id, sender_id)
        return {
            "convo": convo,
            "platform": platform,
            "name": name,
            "referral": thread.get("referral"),
            "mutedUntil": muted_until,
            "isMuted": muted_until > int(time.time()),
            "lastMessage": {
                "role": last["role"],
                "content": last["content"],
                "mediaType": last.get("media_type"),
                "createdAt": last["created_at"],
            },
        }

    leads = await asyncio.gather(*(build(convo, last) for convo, last in last_by_convo.items()))
    return sorted(leads, key=lambda lead: lead["lastMessage"]["createdAt"], reverse=True)


@router.get("/leads/{convo}/messages", dependencies=[Depends(require_staff)])
async def lead_messages(convo: str):
    _split_convo(convo)
    response = await _http.get(
        f"{SUPABASE_URL}/rest/v1/ig_bot_messages",
        headers=_headers,
        params={
            "convo": f"eq.{convo}",
            "select": "id,role,content,media_type,media_path,referral,reply_to_mid,created_at",
            "order": "created_at.asc",
            "limit": "500",
        },
    )
    if response.status_code >= 400:
        log.error("message fetch failed (%s): %s", response.status_code, response.text)
        raise HTTPException(502, "Could not load this conversation.")

    rows = response.json()

    async def with_media_url(row: dict) -> dict:
        if row.get("media_path"):
            row = {**row, "mediaUrl": await supa.sign_media_url(row["media_path"])}
        return row

    return await asyncio.gather(*(with_media_url(row) for row in rows))


@router.post("/leads/{convo}/reply", dependencies=[Depends(require_staff)])
async def reply(convo: str, body: ReplyBody):
    platform, entry_id, sender_id = _split_convo(convo)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Message is empty.")

    mid = await meta.send_text(entry_id, sender_id, text[:1900], platform)
    if not mid:
        raise HTTPException(502, "Instagram did not accept the message.")

    await store.record_sent_mid(mid)
    await store.add_message(convo, "assistant", text)
    await store.mute(convo, HANDOFF_HOURS)
    return {"ok": True}


@router.post("/leads/{convo}/takeover", dependencies=[Depends(require_staff)])
async def takeover(convo: str, body: TakeoverBody):
    _split_convo(convo)
    if body.muted:
        await store.mute(convo, HANDOFF_HOURS)
    else:
        await store.unmute(convo)
    return {"ok": True}


async def aclose() -> None:
    await _http.aclose()
