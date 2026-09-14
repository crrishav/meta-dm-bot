"""Try every route to Instagram message data and report what each one says.

    .venv/Scripts/python.exe tools/test_instagram.py

Re-run this after any change in the Meta dashboard. If a PASS ever appears on
one of the conversation rows, the export works and tools/export_conversations.py
will pull real history.
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

GRAPH = f"https://graph.facebook.com/{os.environ.get('GRAPH_VERSION', 'v26.0')}"
TOKEN = os.environ["META_PAGE_TOKEN"]
APP_ID = os.environ.get("META_APP_ID", "")
APP_SECRET = os.environ.get("META_APP_SECRET", "")

HTTP = httpx.Client(timeout=45.0)


def attempt(label: str, url: str, params: dict) -> bool:
    """Returns True if the call actually returned data."""
    try:
        response = HTTP.get(url, params=dict(params, access_token=TOKEN))
    except httpx.HTTPError as exc:
        print(f"  TIMEOUT  {label}")
        print(f"           {type(exc).__name__} - Graph hangs here when the app lacks")
        print(f"           the capability, so treat this as a failure.")
        return False

    if response.status_code < 400:
        body = response.json()
        count = len(body.get("data", [])) if "data" in body else 1
        print(f"  OK       {label}  -> {count} item(s)")
        return count > 0

    err = response.json().get("error", {})
    print(f"  FAIL     {label}")
    print(f"           code {err.get('code')}: {err.get('message', '')[:100]}")
    return False


def main() -> int:
    page = HTTP.get(f"{GRAPH}/me", params={"fields": "id,name", "access_token": TOKEN}).json()
    page_id = page["id"]
    ig = HTTP.get(
        f"{GRAPH}/{page_id}",
        params={"fields": "instagram_business_account{id,username}", "access_token": TOKEN},
    ).json().get("instagram_business_account", {})
    ig_id = ig.get("id", "")

    print(f"Page : {page.get('name')} ({page_id})")
    print(f"IG   : @{ig.get('username')} ({ig_id})\n")

    print("BASELINE - can the token see the Instagram account at all?")
    attempt("read IG profile", f"{GRAPH}/{ig_id}",
            {"fields": "id,username,followers_count,media_count"})

    print("\nGRANULAR SCOPES - which assets each permission actually covers")
    if APP_ID and APP_SECRET:
        data = HTTP.get(f"{GRAPH}/debug_token", params={
            "input_token": TOKEN, "access_token": f"{APP_ID}|{APP_SECRET}"}).json().get("data", {})
        granular = data.get("granular_scopes", [])
        if not granular:
            print("  (none reported)")
        for entry in granular:
            scope = entry.get("scope")
            targets = entry.get("target_ids")
            if scope and "instagram" in scope:
                print(f"  {scope}: {targets if targets else 'NO TARGET ASSETS'}")
        for entry in granular:
            if entry.get("scope") == "instagram_manage_messages":
                if ig_id in (entry.get("target_ids") or []):
                    print("  -> instagram_manage_messages DOES cover this IG account")
                else:
                    print("  -> instagram_manage_messages does NOT cover this IG account")

    print("\nCONVERSATION ROUTES")
    results = [
        attempt("page/conversations?platform=instagram",
                f"{GRAPH}/{page_id}/conversations", {"platform": "instagram", "fields": "id", "limit": 5}),
        attempt("me/conversations?platform=instagram",
                f"{GRAPH}/me/conversations", {"platform": "instagram", "fields": "id", "limit": 5}),
        attempt("ig_id/conversations",
                f"{GRAPH}/{ig_id}/conversations", {"fields": "id", "limit": 5}),
        attempt("page/conversations (no platform)",
                f"{GRAPH}/{page_id}/conversations", {"fields": "id", "limit": 5}),
    ]

    print("\n" + "=" * 62)
    if any(results):
        print("VERDICT: at least one route works. Run:")
        print("  .venv/Scripts/python.exe tools/export_conversations.py instagram")
    else:
        print("VERDICT: no route returns Instagram messages.")
        print()
        print("This is Advanced Access on instagram_manage_messages, granted only")
        print("through App Review + Business Verification. It is not a setting you")
        print("can toggle. The same gate blocks the bot from replying to real leads,")
        print("so it has to be cleared either way.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
