import os
import json
from pathlib import Path
from typing import Optional

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from models import Script, VideoProject
import config
from utils.logger import get_logger

logger = get_logger(__name__)

# Scopes required for uploading videos and setting thumbnails
UPLOAD_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube"
]

# Analytics loop (Phase 2.7 / plan A3): read-only retention + watch-time data.
# A stored token granted without this scope triggers a one-time re-consent.
ANALYTICS_SCOPES = UPLOAD_SCOPES + [
    "https://www.googleapis.com/auth/yt-analytics.readonly"
]

SCOPES = UPLOAD_SCOPES  # backward-compat alias

# Copyright remediation (FIX-055): every music bed now ships from the
# verified Kevin MacLeod / incompetech library, licensed CC-BY 4.0 — which
# REQUIRES credit on each upload. Append the license line to every
# description automatically so no video can ever ship without it.
MACLEOD_ATTRIBUTION = (
    "\n\nMusic: Kevin MacLeod (incompetech.com)\n"
    "Licensed under Creative Commons: By Attribution 4.0\n"
    "https://creativecommons.org/licenses/by/4.0/"
)


def _yt_service_with_scopes(kind: str = "upload", channel: str | None = None):
    """Authenticate and return the YouTube Data API service.

    kind="analytics" additionally requests yt-analytics.readonly (the
    analytics learning loop). One token file serves both; requesting the
    broader scope re-runs the flow once and stores the wider grant.

    channel: which channel profile to authenticate as. Each channel gets its
    own token file (config.youtube_token_path) — one browser consent per
    channel, and uploads always land on the intended channel. "default"
    keeps the legacy youtube_token.json path. If the per-channel token file
    does not exist yet but the legacy token does, the legacy token is used
    (it belongs to whichever channel it was consented for — pre-split runs).
    """
    required = ANALYTICS_SCOPES if kind == "analytics" else UPLOAD_SCOPES
    token_path = config.youtube_token_path(channel)

    # Split-era graceful fallback: a channel whose own token was never
    # minted may still be served by the legacy consent (same Google account).
    if not token_path.exists():
        legacy = config.PROJECT_ROOT / "youtube_token.json"
        if channel and channel != "default" and legacy.exists():
            logger.warning(
                f"No token for channel '{channel}' — falling back to legacy "
                f"youtube_token.json (same-account consent). Run an OAuth flow "
                f"for this channel to bind its own token."
            )
            token_path = legacy

    creds = None

    # Load existing credentials if available
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), required)
        except Exception as e:
            logger.warning(f"Failed to load existing YouTube token: {e}")

        if creds and creds.valid and kind == "analytics":
            have = set(creds.scopes or [])
            if not set(required).issubset(have):
                logger.info("Stored token lacks yt-analytics.readonly — one-time re-consent needed.")
                creds = None

    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired YouTube credentials...")
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.warning(f"Token refresh failed ({e}); re-running OAuth flow.")
                creds = None
        if not creds:
            if not config.YOUTUBE_CLIENT_SECRETS_FILE.exists():
                raise FileNotFoundError(
                    f"Missing {config.YOUTUBE_CLIENT_SECRETS_FILE}! "
                    "You must download your OAuth 2.0 Client IDs JSON from Google Cloud Console "
                    "and place it in the project root."
                )
            logger.info("Starting new OAuth flow. Please check your browser to authorize.")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(config.YOUTUBE_CLIENT_SECRETS_FILE), required
            )
            # This opens a local webserver for the OAuth callback
            creds = flow.run_local_server(port=0)

        # Save the credentials for the next run
        token_path.write_text(creds.to_json(), encoding="utf-8")
        logger.info(f"Saved new YouTube token to {token_path}")

    return build("youtube", "v3", credentials=creds)


def get_authenticated_service(channel: str | None = None):
    """Authenticate and return the YouTube Data API service (upload scopes)."""
    return _yt_service_with_scopes(channel=channel)


def post_pinned_comment(youtube, video_id: str, text: str) -> bool:
    """Post a top-level comment thread (Phase 3.11 batch ops). Returns success."""
    try:
        youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {"snippet": {"textOriginal": text}},
                }
            },
        ).execute()
        logger.info("Pinned comment posted.")
        return True
    except Exception as e:
        logger.warning(f"Pinned comment failed: {e}")
        return False


def _companion_comment_text(companion_url: str, companion_is_doc: bool) -> str:
    """FIX-077: the bridge comment posted on BOTH siblings of a story.
    Unlisted during production; the operator pins it when publishing."""
    if companion_is_doc:
        return (f"👀 The FULL story is here: {companion_url} "
                f"(8-min breakdown of everything in this Short)")
    return (f"⏱️ In a hurry? Watch the 45-second version: {companion_url}")


def post_companion_bridge(uploaded_video_id: str, companion_video_id: str,
                          companion_is_doc: bool) -> bool:
    """FIX-077: cross-link a shipped video to its story sibling.

    The 28-day analytics showed the bridge is the whole funnel: the Khaled
    short pulled 1.4k views while the doc on the SAME story got 23 — there
    was no path from one to the other. The Data API cannot PIN a comment,
    so this posts it and the operator pins it with one click at publish
    time (checklist item 'pin_bridge_comment').
    """
    try:
        youtube = get_authenticated_service()
        text = _companion_comment_text(
            f"https://youtu.be/{companion_video_id}", companion_is_doc)
        ok = post_pinned_comment(youtube, uploaded_video_id, text)
        if ok:
            logger.info(f"Bridge comment posted on {uploaded_video_id} -> "
                        f"{companion_video_id} (pin it when publishing)")
        return ok
    except Exception as e:
        logger.warning(f"Bridge comment failed: {e}")
        return False


def _reassert_after_processing(youtube, video_id: str, privacy_status: str,
                               max_wait_s: int = 900) -> None:
    """Post-upload guard (2026-09-26): poll until YouTube finishes transcoding,
    then re-assert the requested privacy. Why both halves exist:
    (1) a completed upload sat in `processing` for 33h (transcode wedged —
    a fresh re-upload of the same bytes processed in 60s); the duration
    stayed P0D and the operator could not review the video;
    (2) after the stuck sibling was deleted, the fresh copy briefly showed
    `public` although it was uploaded `unlisted`. The operator's cardinal
    rule is UNLISTED delivery until he publishes — so privacy is verified
    and re-asserted on every poll, never trusted once."""
    import time as _time
    deadline = _time.time() + max_wait_s
    while _time.time() < deadline:
        try:
            items = youtube.videos().list(
                part="status,processingDetails", id=video_id
            ).execute().get("items", [])
            if items:
                st = items[0].get("status", {})
                proc = (items[0].get("processingDetails", {}) or {}).get("processingStatus")
                cur = st.get("privacyStatus")
                if cur and cur != privacy_status:
                    youtube.videos().update(part="status", body={
                        "id": video_id,
                        "status": {"privacyStatus": privacy_status,
                                   "selfDeclaredMadeForKids": False,
                                   "containsSyntheticMedia": True}}).execute()
                    logger.warning(
                        f"Privacy drifted to '{cur}' post-upload — "
                        f"re-asserted '{privacy_status}'.")
                if proc in ("succeeded", "terminated") or st.get("uploadStatus") == "processed":
                    logger.info(
                        f"Post-upload guard: processing={proc}, "
                        f"privacy={privacy_status} confirmed.")
                    return
                logger.info(f"Post-upload guard: processing={proc} — waiting...")
        except Exception as e:
            logger.warning(f"Post-upload guard check failed ({e}); continuing.")
            return
        _time.sleep(30)
    logger.warning(
        "Post-upload guard: processing still incomplete after wait — "
        "check `main.py --status` before relying on this video.")


def upload_video(
    project_dir: Path,
    video_path: Path,
    thumbnail_path: Optional[Path] = None,
    privacy_status: str = "private",
    channel: str | None = None,
    title_override: Optional[str] = None,
) -> str:
    """
    Upload a video to YouTube with metadata from metadata.json.
    Returns the YouTube Video ID.

    title_override: use a different title than metadata.json (e.g. a
    " #shorts" suffix when uploading a project's 9:16 companion — without
    it the FIX-054 idempotency guard sees the long video's title and skips).
    """
    logger.info(f"Preparing to upload {video_path.name} to YouTube...")
    youtube = get_authenticated_service(channel=channel)

    # Load metadata
    metadata_path = project_dir / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Metadata not found: {metadata_path}")
    
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    # Prepare video insert payload
    # categoryId comes from metadata.json. Default 24 = Entertainment
    # (celebrity-news lane; operator call 2026-09-26).
    title = title_override or metadata.get("title", "GhostDirector Video")
    description = metadata.get("description", "")
    # CC-BY 4.0 attribution guard: append the MacLeod credit unless the
    # metadata already carries it (idempotent on retries/resumes).
    if "incompetech.com" not in description:
        description = description.rstrip() + MACLEOD_ATTRIBUTION

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": metadata.get("tags", []),
            "categoryId": metadata.get("category", "24"),
            "defaultLanguage": metadata.get("language", "en")
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True
        }
    }

    # Video upload
    # FIX-054: idempotency guard — a "failed" upload may have actually reached
    # YouTube (connection reset after the server accepted it). Check the
    # channel's uploads for the same title first; if present, return that id
    # instead of creating a duplicate the operator has to hunt down.
    try:
        ch = youtube.channels().list(part="contentDetails", mine=True).execute()
        up = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        pl = youtube.playlistItems().list(
            part="contentDetails", playlistId=up, maxResults=50).execute()
        ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
        if ids:
            existing = youtube.videos().list(
                part="snippet,status", id=",".join(ids)).execute()
            for v in existing.get("items", []):
                if v["snippet"].get("title") == title:
                    logger.warning(
                        f"Upload skipped — '{title[:50]}' already on channel "
                        f"as {v['id']} ({v['status'].get('privacyStatus')})")
                    return v["id"]
    except Exception as e:
        logger.warning(f"Idempotency pre-check failed (uploading anyway): {e}")

    logger.info("Uploading video file (this may take a while)...")
    media = MediaFileUpload(
        str(video_path), 
        mimetype="video/mp4", 
        resumable=True, 
        chunksize=1024*1024*5 # 5MB chunks
    )
    
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )
    
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info(f"Upload progress: {int(status.progress() * 100)}%")

    video_id = response["id"]
    logger.info(f"Upload complete! Video ID: {video_id}")
    logger.info(f"URL: https://youtu.be/{video_id}")

    # Phase 3.11: auto-post the pinned comment + surface the manual checklist.
    checklist = metadata.get("checklist", {})
    pinned = metadata.get("pinned_comment")
    if pinned:
        if post_pinned_comment(youtube, video_id, pinned):
            checklist["pinned_comment_posted"] = True

    pending = [k for k, v in checklist.items() if v is False]
    if pending:
        logger.warning(
            f"Manual Studio checklist remaining: {pending} "
            f"(see metadata.json 'checklist')"
        )

    # Persist checklist state (also refreshes thumbnail variant actually used)
    if thumbnail_path:
        checklist["thumbnail_variant_used"] = thumbnail_path.name
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Thumbnail upload
    if thumbnail_path and thumbnail_path.exists():
        logger.info(f"Uploading thumbnail: {thumbnail_path.name}...")
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path))
            ).execute()
            logger.info("Thumbnail uploaded successfully.")
        except Exception as e:
            logger.error(f"Failed to upload thumbnail: {e}")

    # Post-upload guard: confirm transcoding finished and privacy held
    # (catches wedged transcodes AND privacy drift; 2026-09-26 incidents).
    _reassert_after_processing(youtube, video_id, privacy_status)

    return video_id
