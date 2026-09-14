"""Check that the Meta side of the setup is actually wired up.

    .venv/Scripts/python.exe tools/doctor.py

Reads .env. Tells you, in order: is the token real, is it the right kind, which
Page it belongs to, is Instagram linked, can the app actually read Instagram
messages, and is the Page subscribed to your app.
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

GRAPH = f"https://graph.facebook.com/{os.environ.get('GRAPH_VERSION', 'v26.0')}"

# Graph can hang for a long time on the endpoints it is currently erroring on.
HTTP = httpx.Client(timeout=30.0)

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_results: list[str] = []


def report(status: str, label: str, detail: str = "") -> None:
    _results.append(status)
    print(f"{status:4}  {label}")
    if detail:
        for line in detail.split("\n"):
            print(f"      {line}")


def main() -> int:
    app_id = os.environ.get("META_APP_ID", "")
    app_secret = os.environ.get("META_APP_SECRET", "")
    page_token = os.environ.get("META_PAGE_TOKEN", "")

    print("--- environment ---")
    for name in ("META_APP_ID", "META_APP_SECRET", "META_PAGE_TOKEN",
                 "META_VERIFY_TOKEN", "GOOGLE_API_KEY"):
        value = os.environ.get(name, "")
        report(PASS if value else FAIL, name, "" if value else "not set in .env")

    if not page_token:
        print("\nNo page token - nothing else can be checked.")
        return 1

    print("\n--- token ---")
    if app_id and app_secret:
        debug = HTTP.get(
            f"{GRAPH}/debug_token",
            params={"input_token": page_token, "access_token": f"{app_id}|{app_secret}"},
        ).json().get("data", {})

        if not debug.get("is_valid"):
            report(FAIL, "token is valid", str(debug.get("error", debug)))
            return 1
        report(PASS, "token is valid")

        kind = debug.get("type", "?")
        report(PASS if kind == "PAGE" else FAIL,
               f"token type is PAGE (got {kind})",
               "" if kind == "PAGE" else "You saved a user token. Use the Generate "
                                         "button in Messenger API Settings.")

        expires = debug.get("expires_at", 0)
        report(PASS if expires == 0 else WARN,
               "token does not expire" if expires == 0 else f"token expires ({expires})",
               "" if expires == 0 else "Short-lived - regenerate for a permanent one.")

        scopes = set(debug.get("scopes", []))
        needed = {"pages_show_list", "pages_messaging", "pages_manage_metadata",
                  "pages_read_engagement", "instagram_basic", "instagram_manage_messages"}
        missing = needed - scopes
        report(PASS if not missing else FAIL,
               "token carries the six required permissions",
               "" if not missing else f"missing: {', '.join(sorted(missing))}")
    else:
        report(WARN, "token inspection skipped", "set META_APP_ID and META_APP_SECRET")

    print("\n--- page ---")
    me = HTTP.get(f"{GRAPH}/me", params={"fields": "id,name", "access_token": page_token})
    if me.status_code >= 400:
        report(FAIL, "page token works", me.text)
        return 1
    page = me.json()
    page_id = page["id"]
    report(PASS, f"token belongs to: {page['name']}", f"PAGE_ID = {page_id}")

    ig = HTTP.get(
        f"{GRAPH}/{page_id}",
        params={"fields": "instagram_business_account{id,username}", "access_token": page_token},
    ).json().get("instagram_business_account")
    if ig:
        report(PASS, f"instagram linked: @{ig.get('username')}", f"IG_ID = {ig['id']}")
    else:
        report(FAIL, "no instagram account linked",
               "Link it: Meta Business Suite -> Linked accounts, or the Page's\n"
               "Professional dashboard. Instagram DMs cannot work without this.")

    if ig:
        print("\n--- instagram messaging ---")
        try:
            convos = HTTP.get(
                f"{GRAPH}/{page_id}/conversations",
                params={"platform": "instagram", "fields": "id", "limit": 5,
                        "access_token": page_token},
            )
        except httpx.HTTPError as exc:
            report(FAIL, "can read instagram conversations",
                   f"request failed: {exc}. Graph often hangs here when the app "
                   "lacks the Instagram messaging capability.")
            convos = None

        if convos is None:
            pass
        elif convos.status_code < 400:
            count = len(convos.json().get("data", []))
            report(PASS, f"can read instagram conversations ({count} returned)")
        else:
            err = convos.json().get("error", {})
            code = err.get("code")
            if code in (1, 3):
                report(FAIL, "can read instagram conversations",
                       "The app lacks the Instagram messaging capability. Check both:\n"
                       "  a) App Dashboard -> Messenger use case -> Instagram settings\n"
                       "     - is the IG account connected there?\n"
                       "  b) Instagram phone app -> Settings -> Messages and story\n"
                       "     replies -> Connected tools -> 'Allow access to messages'\n"
                       "     must be ON.\n"
                       "Regenerate the Page token after changing either one.")
            else:
                report(FAIL, "can read instagram conversations",
                       f"code {code}: {err.get('message', '')[:150]}")

    print("\n--- webhooks ---")
    subs = HTTP.get(f"{GRAPH}/{page_id}/subscribed_apps", params={"access_token": page_token})
    data = subs.json().get("data", []) if subs.status_code < 400 else []
    if not data:
        report(FAIL, "page is subscribed to your app",
               f"Run: python tools/subscribe_page.py {page_id}")
    else:
        fields = set(data[0].get("subscribed_fields", []))
        report(PASS, f"page is subscribed to: {data[0].get('name', '?')}")
        for field in ("messages", "messaging_postbacks", "message_echoes"):
            report(PASS if field in fields else FAIL, f"  subscribed to '{field}'")

    failures = _results.count(FAIL)
    print(f"\n{_results.count(PASS)} ok, {_results.count(WARN)} warnings, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
