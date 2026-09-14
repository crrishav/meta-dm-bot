"""Turn the short-lived token from the Graph API Explorer into a Page token.

The Explorer token dies in about an hour. This exchanges it for a 60-day user
token, then reads the non-expiring Page token off it.

    python tools/get_page_token.py <APP_ID> <APP_SECRET> <EXPLORER_USER_TOKEN>
"""

import sys

import httpx

GRAPH = "https://graph.facebook.com/v26.0"


def diagnose(app_id: str, app_secret: str, user_token: str, accounts_body: str) -> None:
    """Called when /me/accounts comes back empty - work out which of the three
    usual causes it is instead of making the user guess."""
    info = httpx.get(
        f"{GRAPH}/debug_token",
        params={"input_token": user_token, "access_token": f"{app_id}|{app_secret}"},
    ).json().get("data", {})

    scopes = info.get("scopes", [])
    token_app = str(info.get("app_id", ""))

    print("No Pages returned. Diagnosing...\n")
    print(f"  token type : {info.get('type')}")
    print(f"  app_id     : {token_app}  (you passed {app_id})")
    print(f"  user_id    : {info.get('user_id')}")
    print(f"  scopes     : {', '.join(scopes) if scopes else '(none)'}")
    print(f"\n  raw /me/accounts: {accounts_body[:300]}\n")

    if info.get("type") != "USER":
        print("  >> This is not a USER token. In the Explorer set 'User or Page'")
        print("     to 'User Token' and press Generate.")
    elif token_app and token_app != str(app_id):
        print("  >> Minted for a DIFFERENT app. Pick the right one in the")
        print("     Explorer's 'Meta App' dropdown, then Generate again.")
    elif "pages_show_list" not in scopes:
        print("  >> pages_show_list is missing from this token. Ticking the box")
        print("     does not update a token that was already issued - re-tick it")
        print("     and press Generate to mint a new one.")
    else:
        print("  >> Scopes are fine, so this is a Page ROLE problem: the logged-in")
        print("     user administers no Page. Check the Explorer host dropdown is")
        print("     .facebook.com/ and not .instagram.com/, and that the Page")
        print("     still exists and you still have a role on it.")


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 1
    app_id, app_secret, short_token = sys.argv[1:4]

    long_lived = httpx.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
    )
    if long_lived.status_code >= 400:
        print("Could not exchange the token:\n", long_lived.text)
        return 1
    user_token = long_lived.json()["access_token"]
    print("long-lived user token acquired\n")

    accounts = httpx.get(
        f"{GRAPH}/me/accounts",
        params={
            "fields": "name,access_token,instagram_business_account{id,username}",
            "access_token": user_token,
        },
    )
    if accounts.status_code >= 400:
        print("Could not list Pages:\n", accounts.text)
        return 1

    pages = accounts.json().get("data", [])
    if not pages:
        diagnose(app_id, app_secret, user_token, accounts.text)
        return 1

    for page in pages:
        instagram = page.get("instagram_business_account")
        print(f"Page:        {page['name']}")
        print(f"  PAGE_ID:   {page['id']}")
        if instagram:
            print(f"  IG:        @{instagram.get('username')} ({instagram['id']})")
        else:
            print("  IG:        (no Instagram business account linked)")
        print(f"  META_PAGE_TOKEN={page['access_token']}\n")

    print("The token printed above is a PAGE token. The one you passed on the")
    print("command line was a USER token - they look almost identical.")
    print("Copy the printed one into .env as META_PAGE_TOKEN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
