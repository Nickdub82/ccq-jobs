"""
One-time authorization script.

Run this LOCALLY (not on Railway) the first time you set up Gmail access.

What it does:
    1. Reads credentials.json (OAuth 2.0 client secrets from Google Cloud Console)
    2. Opens your browser to https://accounts.google.com/
    3. You log in as ccq.jobs.montreal@gmail.com
    4. You grant "Read Gmail" permission
    5. Creates token.json with a refresh token (long-lived access)
    6. Prints the token as a single JSON line you can copy into Railway

After this, you only need:
    - credentials.json (stays local, never committed)
    - token.json (stays local, never committed)
    - GMAIL_TOKEN_JSON env var on Railway (paste the output of step 6)

You won't need to re-run this unless:
    - You revoke access in your Google account
    - You change the OAuth scopes
"""
import sys
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SCRIPT_DIR = Path(__file__).parent
CREDENTIALS_PATH = SCRIPT_DIR / "credentials.json"
TOKEN_PATH = SCRIPT_DIR / "token.json"


def main():
    if not CREDENTIALS_PATH.exists():
        print(f"ERROR: {CREDENTIALS_PATH} not found.")
        print("Download it from Google Cloud Console > APIs & Services > Credentials")
        sys.exit(1)

    print("Opening browser for Google OAuth consent...")
    print("Log in as ccq.jobs.montreal@gmail.com and grant Gmail read access.")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_PATH),
        SCOPES,
    )
    creds = flow.run_local_server(port=0)

    # Save to disk for local dev
    TOKEN_PATH.write_text(creds.to_json())
    print(f"\nSuccess! Token saved to: {TOKEN_PATH}")

    # Print single-line JSON for Railway env var
    print()
    print("=" * 70)
    print("To deploy on Railway, copy the line below and paste it as the value")
    print("of the GMAIL_TOKEN_JSON environment variable on the scraper service:")
    print("=" * 70)
    print()
    # Use creds.to_json() which gives a compact single-line representation
    print(creds.to_json())
    print()
    print("=" * 70)

    # Quick sanity test
    print("\nRunning a quick sanity test (fetch 1 email header)...")
    from googleapiclient.discovery import build
    service = build("gmail", "v1", credentials=creds)
    resp = service.users().messages().list(userId="me", maxResults=1).execute()
    messages = resp.get("messages", [])
    if messages:
        print(f"OK: Gmail access works. Found {len(messages)} message(s).")
    else:
        print("OK: Gmail access works (inbox appears empty).")


if __name__ == "__main__":
    main()
