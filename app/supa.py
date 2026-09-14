"""Durable long-term archive of every DM in Supabase.

Writes only ever touch our own ig_bot_* tables/bucket in this project - never
the existing ERP schema. Everything here is best-effort and fire-and-forget:
if Supabase is slow or down, that must never delay or break a reply. Local
SQLite (store.py) stays the source of truth for anything the reply path
actually depends on (dedup, mute state, prompt history).
"""

import logging
import time
from typing import Optional

import httpx

from .config import SUPABASE_SERVICE_KEY, SUPABASE_URL

log = logging.getLogger(__name__)

BUCKET = "ig-dm-media"

_headers = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
}
_http = httpx.AsyncClient(timeout=10.0)


async def log_message(
    convo: str,
    role: str,
    content: str,
    media_type: Optional[str] = None,
    media_path: Optional[str] = None,
    referral: Optional[str] = None,
    reply_to_mid: Optional[str] = None,
    mid: Optional[str] = None,
) -> None:
    """Append one message to the archive. Swallows every error - a failed
    write here should never surface to the customer."""
    try:
        response = await _http.post(
            f"{SUPABASE_URL}/rest/v1/ig_bot_messages",
            headers={**_headers, "Content-Type": "application/json"},
            json={
                "convo": convo,
                "role": role,
                "content": content,
                "media_type": media_type,
                "media_path": media_path,
                "referral": referral,
                "reply_to_mid": reply_to_mid,
                "mid": mid,
            },
        )
        if response.status_code >= 400:
            log.warning("supabase log_message failed (%s): %s", response.status_code, response.text)
    except httpx.HTTPError as exc:
        log.warning("supabase log_message error: %s", exc)


async def upload_media(convo: str, data: bytes, mime_type: str) -> Optional[str]:
    """Re-host a Meta CDN attachment in our own storage - Meta's signed URLs
    expire, so this is the only way it is still reachable months later.
    Returns the storage path, or None on failure."""
    ext = mime_type.split("/")[-1].split(";")[0] or "bin"
    path = f"{convo.replace(':', '/')}/{int(time.time() * 1000)}.{ext}"
    try:
        response = await _http.post(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}",
            headers={**_headers, "Content-Type": mime_type},
            content=data,
        )
        if response.status_code >= 400:
            log.warning("supabase upload_media failed (%s): %s", response.status_code, response.text)
            return None
        return path
    except httpx.HTTPError as exc:
        log.warning("supabase upload_media error: %s", exc)
        return None


async def sign_media_url(path: str, expires_in: int = 3600) -> Optional[str]:
    """A temporary public link to an archived attachment, for the dashboard to
    render - the bucket is private, so the raw storage path is not fetchable
    on its own."""
    try:
        response = await _http.post(
            f"{SUPABASE_URL}/storage/v1/object/sign/{BUCKET}/{path}",
            headers={**_headers, "Content-Type": "application/json"},
            json={"expiresIn": expires_in},
        )
        if response.status_code >= 400:
            log.warning("sign_media_url failed (%s): %s", response.status_code, response.text)
            return None
        signed = response.json().get("signedURL")
        return f"{SUPABASE_URL}/storage/v1{signed}" if signed else None
    except httpx.HTTPError as exc:
        log.warning("sign_media_url error: %s", exc)
        return None


async def aclose() -> None:
    await _http.aclose()
