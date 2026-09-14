"""End-to-end smoke test with Meta and Gemini stubbed out.

    .venv/Scripts/python.exe test_smoke.py

Exercises the tricky parts: signature rejection, deduplication of retried
webhooks, debouncing a burst of messages, and going quiet when a human replies.
"""

import asyncio
import hashlib
import hmac
import json
import os
import pathlib

os.environ.update(
    META_APP_SECRET="test-secret",
    META_VERIFY_TOKEN="test-verify",
    META_PAGE_TOKEN="test-page-token",
    GOOGLE_API_KEY="not-used-in-this-test",
    DEBOUNCE_SECONDS="0.15",
    DB_PATH="test_smoke.db",
)
pathlib.Path("test_smoke.db").unlink(missing_ok=True)

import httpx  # noqa: E402

from app import main, meta, store  # noqa: E402

PAGE_ID = "111"
USER_ID = "222"

sent: list[str] = []


async def fake_send_text(entry_id, recipient_id, text, platform):
    sent.append(text)
    return f"mid.sent.{len(sent)}"


async def fake_send_typing(entry_id, recipient_id):
    return None


async def fake_profile_name(entry_id, user_id):
    return "Sita"


async def fake_draft_reply(history, name):
    turns = [m["content"] for m in history if m["role"] == "user"]
    return (f"reply#{len(sent) + 1} to {len(turns)} user turn(s)", False)


meta.send_text = fake_send_text
meta.send_typing = fake_send_typing
meta.profile_name = fake_profile_name
main.meta.send_text = fake_send_text
main.meta.send_typing = fake_send_typing
main.meta.profile_name = fake_profile_name
main.draft_reply = fake_draft_reply


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def inbound(mid: str, text: str, obj: str = "page") -> dict:
    return {
        "object": obj,
        "entry": [{
            "id": PAGE_ID,
            "messaging": [{
                "sender": {"id": USER_ID},
                "recipient": {"id": PAGE_ID},
                "message": {"mid": mid, "text": text},
            }],
        }],
    }


def echo(mid: str, text: str) -> dict:
    return {
        "object": "page",
        "entry": [{
            "id": PAGE_ID,
            "messaging": [{
                "sender": {"id": PAGE_ID},
                "recipient": {"id": USER_ID},
                "message": {"mid": mid, "text": text, "is_echo": True},
            }],
        }],
    }


async def post(client, payload):
    body = json.dumps(payload).encode()
    return await client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)}
    )


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    return condition


async def run() -> int:
    store.init()
    results = []
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        r = await client.get("/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "test-verify", "hub.challenge": "42"})
        results.append(check("verification echoes the challenge", r.text == "42", r.text))

        r = await client.get("/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42"})
        results.append(check("verification rejects a wrong token", r.status_code == 403))

        body = json.dumps(inbound("m0", "hi")).encode()
        r = await client.post("/webhook", content=body,
                              headers={"X-Hub-Signature-256": "sha256=deadbeef"})
        results.append(check("forged signature is rejected", r.status_code == 403))
        await asyncio.sleep(0.4)
        results.append(check("forged payload produced no reply", not sent, str(sent)))

        await post(client, inbound("m1", "is this available?"))
        await asyncio.sleep(0.5)
        results.append(check("a real message gets exactly one reply", len(sent) == 1, str(sent)))

        await post(client, inbound("m1", "is this available?"))
        await asyncio.sleep(0.5)
        results.append(check("a retried webhook is ignored", len(sent) == 1, str(sent)))

        sent.clear()
        for i, line in enumerate(["hello", "how much", "do you deliver"]):
            await post(client, inbound(f"burst{i}", line))
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.6)
        results.append(check("a burst of 3 messages gets one reply", len(sent) == 1, str(sent)))
        results.append(check("that reply saw all 3 messages", "4 user turn(s)" in sent[0], str(sent)))

        sent.clear()
        await post(client, echo("human.mid.1", "Hi, Ram here, let me help"))
        await asyncio.sleep(0.3)
        convo = f"messenger:{PAGE_ID}:{USER_ID}"
        results.append(check("a human reply mutes the thread", store.is_muted(convo)))

        await post(client, inbound("m2", "ok thanks"))
        await asyncio.sleep(0.5)
        results.append(check("bot stays quiet on a muted thread", not sent, str(sent)))

        sent.clear()
        ig_user = "999"
        payload = inbound("ig1", "price?", obj="instagram")
        payload["entry"][0]["messaging"][0]["sender"]["id"] = ig_user
        await post(client, payload)
        await asyncio.sleep(0.5)
        results.append(check("instagram payloads route through the same path", len(sent) == 1, str(sent)))

    await meta.aclose()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
