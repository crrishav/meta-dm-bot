"""Supabase-backed conversation state - the operational store, not just an
archive.

Render's free tier wipes the local disk on every spin-down (which happens
after as little as 15 minutes of inactivity), so anything the bot actually
depends on - dedup, mute state, prompt history - has to live somewhere that
isn't local disk. Local SQLite would silently reset several times a day.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from .config import MAX_HISTORY, SUPABASE_SERVICE_KEY, SUPABASE_URL

log = logging.getLogger(__name__)

_headers = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}
_http = httpx.AsyncClient(timeout=10.0)


def init() -> None:
    """No-op - tables live in Supabase, created once via supabase_schema.sql,
    not per-process. Kept so the app's startup hook does not need to change."""


async def claim_mid(mid: str) -> bool:
    """Return True the first time we see a message id, False on redelivery.

    Meta retries webhooks it thinks failed, so without this the customer gets
    the same reply two or three times.
    """
    try:
        response = await _http.post(
            f"{SUPABASE_URL}/rest/v1/ig_bot_seen_mids",
            headers=_headers,
            json={"mid": mid},
        )
    except httpx.HTTPError as exc:
        log.warning("claim_mid error (%s) - failing open", exc)
        return True  # never block a real reply over a dedup-check hiccup
    if response.status_code == 201:
        return True
    if response.status_code == 409:
        return False
    log.warning("claim_mid unexpected status (%s): %s", response.status_code, response.text)
    return True


async def record_sent_mid(mid: str) -> None:
    try:
        await _http.post(
            f"{SUPABASE_URL}/rest/v1/ig_bot_sent_mids",
            headers={**_headers, "Prefer": "resolution=ignore-duplicates"},
            json={"mid": mid},
        )
    except httpx.HTTPError as exc:
        log.warning("record_sent_mid failed: %s", exc)


async def was_sent_by_us(mid: str) -> bool:
    try:
        response = await _http.get(
            f"{SUPABASE_URL}/rest/v1/ig_bot_sent_mids",
            headers=_headers,
            params={"mid": f"eq.{mid}", "select": "mid"},
        )
        return response.status_code == 200 and len(response.json()) > 0
    except httpx.HTTPError as exc:
        # "Couldn't check" is not "a human sent it". The only caller mutes the
        # bot for hours on a False, so a Supabase blip would otherwise silence
        # every conversation the bot replies to while it lasts.
        log.warning("was_sent_by_us error (%s) - assuming our own echo", exc)
        return True


async def add_message(
    convo: str,
    role: str,
    text: str,
    referral: Optional[str] = None,
    reply_to_mid: Optional[str] = None,
    mid: Optional[str] = None,
) -> None:
    try:
        response = await _http.post(
            f"{SUPABASE_URL}/rest/v1/ig_bot_messages",
            headers=_headers,
            json={
                "convo": convo,
                "role": role,
                "content": text,
                "referral": referral,
                "reply_to_mid": reply_to_mid,
                "mid": mid,
            },
        )
        if response.status_code >= 400:
            log.error("add_message failed (%s): %s", response.status_code, response.text)
    except httpx.HTTPError as exc:
        log.error("add_message error: %s", exc)


async def history(convo: str) -> list[dict]:
    """Recent turns, oldest first, as {role, content} dicts."""
    try:
        response = await _http.get(
            f"{SUPABASE_URL}/rest/v1/ig_bot_messages",
            headers=_headers,
            params={
                "convo": f"eq.{convo}",
                "select": "role,content",
                "order": "created_at.desc",
                "limit": str(MAX_HISTORY),
            },
        )
        if response.status_code >= 400:
            log.error("history fetch failed (%s): %s", response.status_code, response.text)
            return []
        rows = response.json()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    except httpx.HTTPError as exc:
        log.error("history fetch error: %s", exc)
        return []


async def mute(convo: str, hours: float) -> None:
    until = int(time.time() + hours * 3600)
    await _upsert_thread(convo, {"muted_until": until})


async def unmute(convo: str) -> None:
    """End a human takeover early and hand the conversation back to the bot."""
    await _upsert_thread(convo, {"muted_until": 0})


async def is_muted(convo: str) -> bool:
    row = await _get_thread(convo)
    return bool(row) and (row.get("muted_until") or 0) > int(time.time())


async def set_referral(convo: str, referral: str) -> None:
    """Record which ad started this conversation - first-touch only, so a
    later message from an unrelated ad click mid-thread does not overwrite
    the story of how the lead actually arrived."""
    row = await _get_thread(convo)
    if row and row.get("referral"):
        return
    await _upsert_thread(convo, {"referral": referral})


async def get_referral(convo: str) -> Optional[str]:
    row = await _get_thread(convo)
    return row.get("referral") if row else None


async def set_value(convo: str, score: int, tier: str, reasons: list[str]) -> None:
    """Record how much this lead looks worth chasing, per app.brain.assess_value.
    Never used by the reply logic - purely for the dashboard's Leads list."""
    await _upsert_thread(
        convo,
        {
            "value_score": score,
            "value_tier": tier,
            "value_reasons": reasons,
            "value_updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


async def should_assess_value(convo: str, cooldown_minutes: float) -> bool:
    """Whether it's been long enough since the last score to justify another
    Gemini call - keeps scoring cost proportional to real activity instead of
    running on every single message."""
    row = await _get_thread(convo)
    updated_at = row.get("value_updated_at") if row else None
    if not updated_at:
        return True
    try:
        last = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() > cooldown_minutes * 60


async def _get_thread(convo: str) -> Optional[dict]:
    try:
        response = await _http.get(
            f"{SUPABASE_URL}/rest/v1/ig_bot_threads",
            headers=_headers,
            params={"convo": f"eq.{convo}", "select": "muted_until,referral,value_updated_at"},
        )
        if response.status_code >= 400:
            log.warning("thread fetch failed (%s): %s", response.status_code, response.text)
            return None
        rows = response.json()
        return rows[0] if rows else None
    except httpx.HTTPError as exc:
        log.warning("thread fetch error: %s", exc)
        return None


async def _upsert_thread(convo: str, fields: dict) -> None:
    try:
        response = await _http.post(
            f"{SUPABASE_URL}/rest/v1/ig_bot_threads",
            headers={**_headers, "Prefer": "resolution=merge-duplicates"},
            json={"convo": convo, **fields},
        )
        if response.status_code >= 400:
            log.warning("thread upsert failed (%s): %s", response.status_code, response.text)
    except httpx.HTTPError as exc:
        log.warning("thread upsert error: %s", exc)


async def aclose() -> None:
    await _http.aclose()
