"""Thin wrapper over the Graph API bits we actually use."""

import hashlib
import hmac
import logging
from typing import Optional

import httpx

from .config import GRAPH_IG, IG_APP_SECRET, IG_TOKEN

log = logging.getLogger(__name__)

# One client for the process; Graph is fine with keep-alive.
_http = httpx.AsyncClient(timeout=15.0)

# A malicious or misbehaving CDN response should never be allowed to blow up
# memory or the Gemini request - cap what we'll pull down per attachment.
MAX_MEDIA_BYTES = 8 * 1024 * 1024


def verify_signature(raw_body: bytes, header: Optional[str]) -> bool:
    """Check X-Hub-Signature-256.

    Skip this and anyone who learns your URL can inject fake customers into
    your CRM, so it is not optional. Meta signs Instagram webhook payloads
    with the Instagram app's own secret (IG_APP_SECRET), not the outer
    Facebook app's secret - confirmed by testing, every delivery was
    rejected until this was used instead.
    """
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(IG_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256=") :])


async def send_text(entry_id: str, recipient_id: str, text: str, platform: str) -> Optional[str]:
    """Send a DM via the Instagram API with Instagram Login. Returns the
    message id Meta assigns, so we can recognise our own echo later.

    `entry_id` is the Instagram account id from the webhook's entry object -
    kept as a parameter for parity with the webhook shape, unused here since
    sends always go through the IG account tied to IG_TOKEN.
    """
    response = await _http.post(
        f"{GRAPH_IG}/{entry_id}/messages",
        params={"access_token": IG_TOKEN},
        json={"recipient": {"id": recipient_id}, "message": {"text": text}},
    )
    if response.status_code >= 400:
        log.error("send_text failed (%s): %s", response.status_code, response.text)
        return None
    return response.json().get("message_id")


async def send_attachment(entry_id: str, recipient_id: str, kind: str, url: str) -> Optional[str]:
    """Send an image or voice note by URL - same shape as send_text, just an
    attachment instead of text. `url` must be reachable by Meta's servers at
    the moment they fetch it; a short-lived Supabase signed URL is fine since
    the fetch happens synchronously on their side, not queued.

    There is no attachment_id/upload-first path here: the Attachment Upload
    API is Messenger-Platform/Page-only and does not work with the Instagram
    API with Instagram Login this bot uses, so every send goes by URL.
    """
    response = await _http.post(
        f"{GRAPH_IG}/{entry_id}/messages",
        params={"access_token": IG_TOKEN},
        json={"recipient": {"id": recipient_id}, "message": {"attachment": {"type": kind, "payload": {"url": url}}}},
    )
    if response.status_code >= 400:
        log.error("send_attachment failed (%s): %s", response.status_code, response.text)
        return None
    return response.json().get("message_id")


async def send_typing(entry_id: str, recipient_id: str) -> None:
    """Best effort - a failed typing bubble should never block the reply."""
    try:
        await _http.post(
            f"{GRAPH_IG}/{entry_id}/messages",
            params={"access_token": IG_TOKEN},
            json={"recipient": {"id": recipient_id}, "sender_action": "typing_on"},
        )
    except httpx.HTTPError as exc:
        log.warning("typing indicator failed: %s", exc)


async def profile_name(entry_id: str, user_id: str) -> Optional[str]:
    """First name, when the permission allows it. Returns None rather than
    raising - plenty of profiles simply are not readable."""
    try:
        response = await _http.get(
            f"{GRAPH_IG}/{user_id}",
            params={"fields": "name", "access_token": IG_TOKEN},
        )
        if response.status_code >= 400:
            return None
        full_name = response.json().get("name")
        return full_name.split()[0] if full_name else None
    except httpx.HTTPError:
        return None


async def profile_info(entry_id: str, user_id: str) -> dict:
    """Full name and profile picture, for the dashboard's lead list. A
    separate call from profile_name() (used on the live reply path) so this
    stays purely additive - one Graph call, never touched by message sending.
    The picture URL Meta returns expires after a few days, so callers should
    cache this briefly, not indefinitely."""
    try:
        response = await _http.get(
            f"{GRAPH_IG}/{user_id}",
            params={"fields": "name,profile_pic", "access_token": IG_TOKEN},
        )
        if response.status_code >= 400:
            return {"name": None, "profilePic": None}
        data = response.json()
        return {"name": data.get("name"), "profilePic": data.get("profile_pic")}
    except httpx.HTTPError:
        return {"name": None, "profilePic": None}


async def download_media(url: str) -> Optional[tuple[bytes, str]]:
    """Fetch an image or voice-note attachment so we can show/play it to
    Gemini. Returns (bytes, mime_type), or None on anything unexpected - a
    broken attachment should degrade to a text-only reply, never crash the
    handler.
    """
    try:
        async with _http.stream("GET", url) as response:
            if response.status_code >= 400:
                log.warning("media download failed (%s): %s", response.status_code, url)
                return None
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            # Instagram voice notes arrive as video/mp4 (an audio-only track in
            # an mp4 container), not audio/* - confirmed by testing. Gemini
            # handles video/mp4 directly, audio included, so we pass it through
            # as-is rather than trying to re-mux it.
            viewable = content_type.startswith("image/") or content_type.startswith("audio/")
            if not (viewable or content_type == "video/mp4"):
                log.warning("attachment was not image/audio/mp4 (%s): %s", content_type, url)
                return None
            declared_length = response.headers.get("content-length")
            if declared_length and int(declared_length) > MAX_MEDIA_BYTES:
                log.warning("attachment too large (%s bytes): %s", declared_length, url)
                return None

            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_MEDIA_BYTES:
                    log.warning("attachment exceeded size cap mid-download: %s", url)
                    return None
            return bytes(chunks), content_type
    except httpx.HTTPError as exc:
        log.warning("media download error: %s", exc)
        return None


async def aclose() -> None:
    await _http.aclose()
