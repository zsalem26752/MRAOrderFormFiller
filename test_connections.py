"""test_connections.py — End-to-end connectivity test.

Verifies that all required external APIs are reachable with the configured credentials.

Usage:
    python test_connections.py

Expected output:
    [ClickUp] ... PASS — found N task(s) with status 'GIVEN TO JAY'
    [Dropbox] ... PASS — connected as Name <email>; test upload/delete OK
    [Zoho]    ... PASS — OAuth token refreshed; found N invoice(s)
"""

import os
import sys

# Make sure .env is loaded
from dotenv import load_dotenv
load_dotenv()

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"


def test_clickup():
    print("[ClickUp] Connecting...", end=" ", flush=True)
    try:
        import requests
        api_key = os.environ["CLICKUP_API_KEY"]
        list_id = os.environ["CLICKUP_LIST_ID"]
        status  = os.environ.get("TRIGGER_STATUS", "GIVEN TO JAY")

        headers = {"Authorization": api_key}
        url     = f"https://api.clickup.com/api/v2/list/{list_id}/task"
        params  = {"statuses[]": status, "include_closed": "false", "page": 0}
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        tasks = r.json().get("tasks", [])
        print(f"{PASS} — found {len(tasks)} task(s) with status '{status}'")
        if tasks:
            print(f"         Tasks: {[t['name'] for t in tasks[:5]]}")
        return True
    except KeyError as e:
        print(f"{FAIL} — missing env var: {e}")
    except Exception as e:
        print(f"{FAIL} — {e}")
    return False


def test_dropbox():
    print("[Dropbox] Connecting...", end=" ", flush=True)
    token = os.environ.get("DROPBOX_ACCESS_TOKEN", "")
    if not token:
        print(f"{SKIP} — DROPBOX_ACCESS_TOKEN not set (local dev mode, no Dropbox needed)")
        return True

    try:
        import dropbox
        from dropbox.files import WriteMode

        dbx = dropbox.Dropbox(token)
        account = dbx.users_get_current_account()
        name    = account.name.display_name
        email   = account.email

        # Upload a small test file then delete it
        folder  = os.environ.get("DROPBOX_FOLDER", "/MRA Order Forms")
        test_path = f"{folder.rstrip('/')}/_connection_test.txt"
        dbx.files_upload(b"connection test", test_path, mode=WriteMode.overwrite)
        dbx.files_delete_v2(test_path)

        print(f"{PASS} — connected as {name} <{email}>; test upload/delete OK")
        return True
    except Exception as e:
        print(f"{FAIL} — {e}")
    return False


def test_zoho():
    print("[Zoho]    Connecting...", end=" ", flush=True)
    try:
        import requests
        client_id     = os.environ["ZOHO_CLIENT_ID"]
        client_secret = os.environ["ZOHO_CLIENT_SECRET"]
        refresh_token = os.environ["ZOHO_REFRESH_TOKEN"]
        org_id        = os.environ["ZOHO_ORGANIZATION_ID"]
        region        = os.environ.get("ZOHO_REGION", "com")

        # Step 1: refresh the access token
        token_url = f"https://accounts.zoho.{region}/oauth/v2/token"
        r = requests.post(token_url, params={
            "refresh_token": refresh_token,
            "client_id":     client_id,
            "client_secret": client_secret,
            "grant_type":    "refresh_token",
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError(f"No access_token in response: {data}")

        # Step 2: hit the invoices endpoint to confirm the org is accessible
        inv_url = f"https://www.zohoapis.{region}/invoice/v3/invoices"
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "X-com-zoho-invoice-organizationid": org_id,
        }
        r2 = requests.get(inv_url, headers=headers, params={"page": 1, "per_page": 1}, timeout=15)
        r2.raise_for_status()
        body  = r2.json()
        count = body.get("page_context", {}).get("total", "?")
        print(f"{PASS} — OAuth token refreshed; {count} total invoice(s) in org")
        return True
    except KeyError as e:
        print(f"{FAIL} — missing env var: {e}")
    except Exception as e:
        print(f"{FAIL} — {e}")
    return False


if __name__ == "__main__":
    print("=" * 55)
    print("  MRA Order Form Filler — Connection Test")
    print("=" * 55)

    results = [
        test_clickup(),
        test_dropbox(),
        test_zoho(),
    ]

    print("=" * 55)
    if all(results):
        print("All tests passed. ✓")
        sys.exit(0)
    else:
        print("One or more tests FAILED. See details above.")
        sys.exit(1)
