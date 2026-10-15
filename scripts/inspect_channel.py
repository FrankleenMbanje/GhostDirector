"""Read-only channel snapshot using existing consent; never starts OAuth or publishes."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from pipeline.analytics import channel_growth_metrics


def inspect(channel):
    token_path = config.youtube_token_path(channel)
    if not token_path.exists():
        raise RuntimeError(f"No token bound to channel {channel}; no legacy fallback permitted")
    creds = Credentials.from_authorized_user_file(str(token_path))
    if not creds.valid:
        creds.refresh(Request())
    youtube = build("youtube", "v3", credentials=creds)
    channels = youtube.channels().list(part="snippet,statistics,contentDetails", mine=True).execute().get("items", [])
    result = {"at": datetime.now(timezone.utc).isoformat(), "profile": channel,
              "channels": channels, "videos": []}
    for ch in channels:
        token = None
        for _ in range(10):
            page = youtube.playlistItems().list(
                part="contentDetails", playlistId=ch["contentDetails"]["relatedPlaylists"]["uploads"],
                maxResults=50, pageToken=token).execute()
            ids = ",".join(it["contentDetails"]["videoId"] for it in page.get("items", []))
            if ids:
                result["videos"].extend(youtube.videos().list(
                    part="snippet,statistics,status,contentDetails", id=ids).execute().get("items", []))
            token = page.get("nextPageToken")
            if not token:
                break
    try:
        yta = build("youtubeAnalytics", "v2", credentials=creds)
        result["growth"] = channel_growth_metrics(youtube, yta, datetime.now(timezone.utc).date())
    except Exception as exc:
        result["analytics_error"] = {"type": type(exc).__name__,
                                     "status": getattr(getattr(exc, "resp", None), "status", None)}
    path = config.OUTPUT_DIR / f"channel_inspection_{channel}_{datetime.now(timezone.utc).date()}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for ch in channels:
        print(ch["snippet"]["title"], ch.get("statistics", {}))
    print("Snapshot:", path)
    if result.get("analytics_error"):
        print("Analytics unavailable:", result["analytics_error"])
    else:
        print("Growth:", json.dumps(result["growth"]))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default="famefiles")
    inspect(parser.parse_args().channel)
