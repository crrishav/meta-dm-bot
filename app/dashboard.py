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
import re
import time
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
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

# The picture URL Meta returns expires after a few days (per their own docs),
# so this is cached briefly rather than for the process lifetime the way the
# old name-only cache was.
PROFILE_CACHE_SECONDS = 12 * 3600
_profile_cache: dict[str, tuple[dict, float]] = {}

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
# Meta fetches the attachment URL synchronously at send time, not on a queue -
# a short-lived signed URL is plenty, and keeps the file from being fetchable
# by anyone who intercepts the request any longer than necessary.
SEND_URL_TTL_SECONDS = 300


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


async def _profile(convo: str, entry_id: str, sender_id: str) -> dict:
    cached = _profile_cache.get(convo)
    if cached and time.time() - cached[1] < PROFILE_CACHE_SECONDS:
        return cached[0]
    info = await meta.profile_info(entry_id, sender_id)
    _profile_cache[convo] = (info, time.time())
    return info


async def _fetch_threads() -> dict[str, dict]:
    response = await _http.get(
        f"{SUPABASE_URL}/rest/v1/ig_bot_threads",
        headers=_headers,
        params={"select": "convo,muted_until,referral,value_score,value_tier,value_reasons"},
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
        profile = await _profile(convo, entry_id, sender_id)
        return {
            "convo": convo,
            "platform": platform,
            "name": profile.get("name"),
            "profilePic": profile.get("profilePic"),
            "referral": thread.get("referral"),
            "mutedUntil": muted_until,
            "isMuted": muted_until > int(time.time()),
            "value": {
                "score": thread.get("value_score"),
                "tier": thread.get("value_tier"),
                "reasons": thread.get("value_reasons") or [],
            },
            "lastMessage": {
                "role": last["role"],
                "content": last["content"],
                "mediaType": last.get("media_type"),
                "createdAt": last["created_at"],
            },
        }

    leads = await asyncio.gather(*(build(convo, last) for convo, last in last_by_convo.items()))
    return sorted(leads, key=lambda lead: lead["lastMessage"]["createdAt"], reverse=True)


_PLACEHOLDER_RE = re.compile(r"^\[customer sent: .+\]$")


def _parse_ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _drop_placeholder_duplicates(rows: list[dict]) -> list[dict]:
    """main.py logs a placeholder like "[customer sent: image]" synchronously
    for every inbound attachment, so Gemini has something to read before the
    media finishes downloading - then _archive_media logs a second row with
    the real media a moment later. Both land in this same table, so without
    this the dashboard would show two bubbles for one attachment. Drop the
    placeholder only when a real media row for the same side shows up within
    a few seconds right after it."""
    out = []
    for i, row in enumerate(rows):
        if row.get("media_path") or not _PLACEHOLDER_RE.match(row.get("content") or ""):
            out.append(row)
            continue
        this_time = _parse_ts(row["created_at"])
        shadowed = any(
            other["role"] == row["role"]
            and other.get("media_path")
            and abs(_parse_ts(other["created_at"]) - this_time) <= 30
            for other in rows[i + 1 : i + 4]
        )
        if not shadowed:
            out.append(row)
    return out


def _reply_label(target: dict) -> str:
    if target.get("content"):
        return target["content"]
    media_type = target.get("media_type") or ""
    return "Photo" if media_type.startswith("image/") else "Voice note" if media_type else ""


def _attach_reply_previews(rows: list[dict]) -> list[dict]:
    """The customer's own `reply_to_mid` (captured on receive) resolved to an
    actual earlier row via that row's own `mid`, so the thread can show a
    quote strip the way the Instagram app itself does."""
    by_mid = {row["mid"]: row for row in rows if row.get("mid")}
    out = []
    for row in rows:
        target = by_mid.get(row.get("reply_to_mid"))
        if target and target["id"] != row["id"]:
            row = {**row, "replyTo": {"id": target["id"], "content": _reply_label(target), "mediaType": target.get("media_type")}}
        out.append(row)
    return out


@router.get("/leads/{convo}/messages", dependencies=[Depends(require_staff)])
async def lead_messages(convo: str):
    _split_convo(convo)
    response = await _http.get(
        f"{SUPABASE_URL}/rest/v1/ig_bot_messages",
        headers=_headers,
        params={
            "convo": f"eq.{convo}",
            "select": "id,role,content,media_type,media_path,referral,reply_to_mid,mid,created_at",
            "order": "created_at.asc",
            "limit": "500",
        },
    )
    if response.status_code >= 400:
        log.error("message fetch failed (%s): %s", response.status_code, response.text)
        raise HTTPException(502, "Could not load this conversation.")

    rows = _attach_reply_previews(_drop_placeholder_duplicates(response.json()))

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
    await store.add_message(convo, "assistant", text, mid=mid)
    await store.mute(convo, HANDOFF_HOURS)
    return {"ok": True}


@router.post("/leads/{convo}/attachment", dependencies=[Depends(require_staff)])
async def send_attachment(convo: str, file: UploadFile = File(...)):
    platform, entry_id, sender_id = _split_convo(convo)
    content_type = file.content_type or ""
    if content_type.startswith("image/"):
        kind, label = "image", "Photo"
    elif content_type.startswith("audio/") or content_type.startswith("video/"):
        # Browser voice recordings commonly arrive as audio/webm; a handful of
        # browsers hand MediaRecorder a video/* container for an audio-only
        # capture the same way Instagram's own voice notes do on receive
        # (app/meta.py's download_media has the same note) - both are a
        # voice note as far as sending is concerned.
        kind, label = "audio", "Voice note"
    else:
        raise HTTPException(400, "Only images and voice notes can be sent from here.")

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "That file is too large.")

    path = await supa.upload_media(convo, data, content_type)
    if not path:
        raise HTTPException(502, "Could not store that file.")

    url = await supa.sign_media_url(path, expires_in=SEND_URL_TTL_SECONDS)
    if not url:
        raise HTTPException(502, "Could not prepare that file to send.")

    mid = await meta.send_attachment(entry_id, sender_id, kind, url)
    if not mid:
        raise HTTPException(502, "Instagram did not accept that file.")

    await store.record_sent_mid(mid)
    await supa.log_message(convo, "assistant", label, media_type=content_type, media_path=path, mid=mid)
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
