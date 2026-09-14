"""Subscribe the Page to this app so Meta starts sending webhooks.

Nothing arrives at your server until this runs - configuring the webhook URL in
the dashboard is only half of it.

    python tools/subscribe_page.py <PAGE_ID>

Reads META_PAGE_TOKEN from .env.
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

GRAPH = f"https://graph.facebook.com/{os.environ.get('GRAPH_VERSION', 'v25.0')}"

FIELDS = [
    "messages",
    "messaging_postbacks",
    "message_echoes",       # lets the bot notice a human taking over
    "messaging_optins",
    "messaging_referrals",  # tells you which ad the lead came from
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    page_id = sys.argv[1]
    token = os.environ["META_PAGE_TOKEN"]

    response = httpx.post(
        f"{GRAPH}/{page_id}/subscribed_apps",
        params={"subscribed_fields": ",".join(FIELDS), "access_token": token},
    )
    print(response.status_code, response.text)
    if response.status_code >= 400:
        return 1

    check = httpx.get(f"{GRAPH}/{page_id}/subscribed_apps", params={"access_token": token})
    print("\nCurrently subscribed:", check.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
