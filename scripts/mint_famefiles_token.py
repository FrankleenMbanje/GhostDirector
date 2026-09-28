"""
Mint a fresh Fame Files OAuth token (youtube_token.famefiles.json).

Opens the browser for interactive consent, saves the token, and verifies it
resolves to The Fame Files channel. Run from the repo root:

    venv/Scripts/python.exe scripts/mint_famefiles_token.py

NOTE (FIX-058): multiprocessing pool children re-import launcher scripts, so
the flow must live under `if __name__ == "__main__":`.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import config

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

EXPECTED_CHANNEL = "UCkxIY_tv62QYMO5MOsBQp9w"  # The Fame Files


def main() -> int:
    flow = InstalledAppFlow.from_client_secrets_file(
        config.YOUTUBE_CLIENT_SECRETS_FILE, scopes=SCOPES
    )
    creds = flow.run_local_server(port=0, prompt="consent")

    token_path = Path(config.OUTPUT_DIR).parent / "youtube_token.famefiles.json"
    token_path.write_text(
        json.dumps({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }, indent=2),
        encoding="utf-8",
    )

    youtube = build("youtube", "v3", credentials=creds)
    resp = youtube.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        print("ERROR: token does not resolve to any channel")
        return 1
    ch = items[0]
    cid = ch["id"]
    title = ch["snippet"]["title"]
    print(f"Token saved: {token_path}")
    print(f"Channel: {title} ({cid})")
    if cid != EXPECTED_CHANNEL:
        print(f"WARNING: expected {EXPECTED_CHANNEL} — wrong channel signed in!")
        return 1
    print("OK: Fame Files token minted and verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
