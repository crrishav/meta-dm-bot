"""Pull existing Page conversations so they can be read and turned into a prompt.

    python tools/export_conversations.py            # both platforms
    python tools/export_conversations.py instagram  # just one

Writes exports/<platform>.md (readable transcripts) and exports/<platform>.json.

Fetches in two phases - conversation ids first, then each conversation's
messages separately. Asking Graph for both at once ("...conversations?fields=
messages{...}") reliably trips its error code 1, which is a catch-all that
looks like a permissions problem but is really a request-size problem.
"""

import json
import os
import pathlib
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

GRAPH = f"https://graph.facebook.com/{os.environ.get('GRAPH_VERSION', 'v26.0')}"
TOKEN = os.environ.get("META_PAGE_TOKEN", "")
OUT = pathlib.Path("exports")


def get(url: str, params: dict | None) -> dict | None:
    response = httpx.get(url, params=params, timeout=60.0)
    if response.status_code >= 400:
        print(f"    ! {response.text[:200]}")
        return None
    return response.json()


def list_conversations(page_id: str, platform: str) -> list[dict]:
    """Phase one: just the ids. Light enough that Graph never complains."""
    found: list[dict] = []
    url = f"{GRAPH}/{page_id}/conversations"
    params = {
        "platform": platform,
        "fields": "id,updated_time,message_count",
        "limit": 100,
        "access_token": TOKEN,
    }
    while url:
        body = get(url, params)
        if not body:
            break
        found.extend(body.get("data", []))
        url = body.get("paging", {}).get("next")
        params = None
        print(f"    {len(found)} conversations found")
    return found


def load_conversation(convo_id: str) -> dict:
    """Phase two: participants and messages for one conversation."""
    detail = get(
        f"{GRAPH}/{convo_id}",
        {"fields": "participants,updated_time", "access_token": TOKEN},
    ) or {}

    messages: list[dict] = []
    url = f"{GRAPH}/{convo_id}/messages"
    params = {"fields": "message,from,created_time", "limit": 100, "access_token": TOKEN}
    while url:
        body = get(url, params)
        if not body:
            break
        messages.extend(body.get("data", []))
        url = body.get("paging", {}).get("next")
        params = None
        if len(messages) >= 500:  # no single lead conversation needs more
            break

    detail["messages"] = messages
    return detail


def to_markdown(conversations: list[dict], platform: str) -> str:
    lines = [f"# {platform} conversations ({len(conversations)})", ""]
    for convo in conversations:
        names = [
            p.get("name", p.get("username", p.get("id", "?")))
            for p in convo.get("participants", {}).get("data", [])
        ]
        lines.append(f"## {' <-> '.join(names) or convo.get('id', '?')}")
        lines.append(f"_last active {convo.get('updated_time', '?')}_")
        lines.append("")
        # Graph returns newest first; read them the way they happened.
        for message in reversed(convo.get("messages", [])):
            who = message.get("from", {}).get("name") or message.get("from", {}).get("username", "?")
            text = (message.get("message") or "").strip()
            if text:
                lines.append(f"- **{who}:** {text}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    if not TOKEN:
        print("META_PAGE_TOKEN is not set in .env")
        return 1

    me = get(f"{GRAPH}/me", {"fields": "id,name", "access_token": TOKEN})
    if not me:
        return 1
    print(f"Page: {me['name']} ({me['id']})\n")

    OUT.mkdir(exist_ok=True)
    total = 0

    for platform in (sys.argv[1:] or ["messenger", "instagram"]):
        print(f"{platform}:")
        stubs = list_conversations(me["id"], platform)

        conversations = []
        for i, stub in enumerate(stubs, 1):
            print(f"    fetching {i}/{len(stubs)}", end="\r")
            conversations.append(load_conversation(stub["id"]))
            time.sleep(0.15)  # stay well under Graph's rate limit
        print(" " * 40, end="\r")

        total += len(conversations)
        (OUT / f"{platform}.json").write_text(
            json.dumps(conversations, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (OUT / f"{platform}.md").write_text(
            to_markdown(conversations, platform), encoding="utf-8"
        )
        msgs = sum(len(c.get("messages", [])) for c in conversations)
        print(f"  -> exports/{platform}.md  ({len(conversations)} conversations, {msgs} messages)\n")

    if total == 0:
        print("Nothing came back - most likely Standard Access, which limits the")
        print("API to people who hold a role on your app. The Business Suite inbox")
        print("shows everything because that is you logged in as a human, not an app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
