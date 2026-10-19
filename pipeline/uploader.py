import os
import json
from datetime import datetime, timezone
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
from utils.ffmpeg_cmd import get_duration

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


def _existing_upload_matches_schedule(status: dict, privacy_status: str,
                                      scheduled_at: str | None) -> bool:
    """Whether a same-title upload already has the requested visibility/release."""
    existing_publish_at = status.get("publishAt")
    if scheduled_at:
        return (existing_publish_at == scheduled_at
                and status.get("privacyStatus") in ("private", "public"))
    return (not existing_publish_at
            and status.get("privacyStatus") == privacy_status)


def _scheduled_upload_status(privacy_status: str,
                             publish_at: str | None) -> tuple[str, str | None]:
    """Validate a scheduled release and return its effective privacy and time."""
    raw_publish_at = (publish_at if publish_at is not None
                      else os.environ.get("GD_PUBLISH_AT"))
    scheduled_at = (raw_publish_at or "").strip()
    if not scheduled_at:
        return privacy_status, None
    try:
        publish_dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(
            f"publish_at must be an ISO-8601 timestamp: {scheduled_at!r}") from e
    if publish_dt.tzinfo is None or publish_dt.utcoffset() is None:
        raise ValueError("publish_at must include a timezone, preferably UTC (Z)")
    if publish_dt.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise ValueError("publish_at must be in the future")
    return "private", scheduled_at


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
            # Passing `required` here overwrites the file's granted scopes
            # and makes an upload-only token appear Analytics-authorized.
            creds = Credentials.from_authorized_user_file(str(token_path))
        except Exception as e:
            logger.warning(f"Failed to load existing YouTube token: {e}")

        if creds and kind == "analytics":
            have = set(creds.scopes or [])
            # Full youtube access also authorizes non-monetary Analytics
            # (verified against this channel's live reports). Preserve that
            # existing consent rather than demanding an unnecessary flow.
            if (not set(required).issubset(have)
                    and "https://www.googleapis.com/auth/youtube" not in have):
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
            if os.environ.get("GITHUB_ACTIONS") == "true":
                raise RuntimeError(
                    "YouTube OAuth consent is required; reauthorize locally "
                    "with the requested scopes and update GD_YOUTUBE_TOKEN. "
                    "Interactive consent cannot run on GitHub Actions.")
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
                          companion_is_doc: bool, channel: str | None = None) -> bool:
    """FIX-077: cross-link a shipped video to its story sibling.

    The 28-day analytics showed the bridge is the whole funnel: the Khaled
    short pulled 1.4k views while the doc on the SAME story got 23 — there
    was no path from one to the other. The Data API cannot PIN a comment,
    so this posts it and the operator pins it with one click at publish
    time (checklist item 'pin_bridge_comment').
    """
    try:
        youtube = get_authenticated_service(channel=channel)
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
                               max_wait_s: int = 900,
                               publish_at: str | None = None) -> None:
    """Post-upload guard (2026-09-26): poll until YouTube finishes transcoding,
    then re-assert the requested privacy. Why both halves exist:
    (1) a completed upload sat in `processing` for 33h (transcode wedged —
    a fresh re-upload of the same bytes processed in 60s); the duration
    stayed P0D and the operator could not review the video;
    (2) after the stuck sibling was deleted, the fresh copy briefly showed
    `public` although it was uploaded `unlisted`. Privacy is verified and
    re-asserted on every poll, including scheduled private uploads."""
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
                privacy_drift = cur and cur != privacy_status
                schedule_drift = (publish_at is not None
                                  and st.get("publishAt") != publish_at)
                if privacy_drift or schedule_drift:
                    status_body = {"privacyStatus": privacy_status,
                                   "selfDeclaredMadeForKids": False,
                                   "containsSyntheticMedia": True}
                    if publish_at is not None:
                        status_body["publishAt"] = publish_at
                    youtube.videos().update(part="status", body={
                        "id": video_id, "status": status_body}).execute()
                    logger.warning(
                        f"Post-upload status drift (privacy={cur!r}, "
                        f"publishAt={st.get('publishAt')!r}) — reasserted "
                        f"privacy={privacy_status!r}, publishAt={publish_at!r}.")
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


def _iso8601_seconds(value: str) -> float | None:
    """YouTube's PT#H#M#S duration -> seconds; None when unparseable."""
    import re as _re
    m = _re.fullmatch(
        r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?",
        value or "")
    if not m:
        return None
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def verify_upload_duration(video_id: str, local_seconds: float,
                           channel: str | None = None,
                           tolerance: float = 0.25) -> tuple[bool, str]:
    """FIX-081: confirm the channel video matches the render we just shipped.

    The FIX-054 idempotency guard can return an EXISTING video id, and a
    same-title collision (a short and a doc on one story) once let a doc
    stage exit 0 with NO doc on the channel (run 11, 2026-10-02). Comparing
    the server-side duration against the local file catches both a stale
    collision and a never-uploaded file. Unreadable durations verify as
    True — this must never fail a legitimate retry re-upload.
    """
    youtube = get_authenticated_service(channel=channel)
    items = youtube.videos().list(part="contentDetails", id=video_id
                                  ).execute().get("items", [])
    if not items:
        return False, f"{video_id} not found on the channel"
    have = _iso8601_seconds(items[0].get("contentDetails", {}).get("duration", ""))
    if have is None or not local_seconds:
        return True, f"{video_id}: duration unreadable — not verified"
    if abs(have - local_seconds) / local_seconds > tolerance:
        return False, (f"{video_id} is {have:.0f}s but the render is "
                       f"{local_seconds:.0f}s — a different video was returned")
    return True, f"{video_id}: {have:.0f}s matches the render"


def upload_video(
    project_dir: Path,
    video_path: Path,
    thumbnail_path: Optional[Path] = None,
    privacy_status: str = "public",
    channel: str | None = None,
    title_override: Optional[str] = None,
    publish_at: str | None = None,
) -> str:
    """
    Upload a video to YouTube with metadata from metadata.json.
    Returns the YouTube Video ID.

    title_override: use a different title than metadata.json (e.g. a
    " #shorts" suffix when uploading a project's 9:16 companion — without
    it the FIX-054 idempotency guard sees the long video's title and skips).
    publish_at: optional future ISO-8601 UTC publish time. Scheduled videos
    must upload as private; YouTube makes them public at this time.
    Idempotency reuses an existing upload only when its publish schedule
    matches this request, so retries cannot silently change release timing.
    """
    if (config.PROJECT_ROOT / 'LOCAL_EDITING_ONLY').exists():
        raise RuntimeError('Uploads are disabled: local DaVinci editing and review only.')
    effective_privacy, scheduled_at = _scheduled_upload_status(
        privacy_status, publish_at)
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
            "privacyStatus": effective_privacy,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True
        }
    }
    if scheduled_at:
        body["status"]["publishAt"] = scheduled_at

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
                part="snippet,status,contentDetails", id=",".join(ids)).execute()
            try:
                want_s = float(get_duration(Path(video_path)))
            except Exception:
                want_s = 0.0
            for v in existing.get("items", []):
                if v["snippet"].get("title") != title:
                    continue
                status = v.get("status") or {}
                schedule_matches = _existing_upload_matches_schedule(
                    status, privacy_status, scheduled_at)
                if not schedule_matches:
                    logger.warning(
                        f"Same-title upload {v['id']} has different visibility/schedule; "
                        "continuing with a new upload")
                    continue
                have_s = _iso8601_seconds(
                    (v.get("contentDetails") or {}).get("duration", "")) or 0.0
                looks_dup = (not want_s or not have_s or
                             abs(have_s - want_s) <= max(10.0, 0.25 * want_s))
                if looks_dup:
                    logger.warning(
                        f"Upload skipped — '{title[:50]}' already on channel "
                        f"as {v['id']} ({status.get('privacyStatus')})")
                    return v["id"]
                # FIX-081: same title but a very different length = a DIFFERENT
                # render (a short already carrying the headline must not block
                # the 8-min doc). Upload, retitling the doc so the pair stays
                # distinguishable on the channel.
                if want_s >= 120:
                    title = f"{title} (Full Story)"
                    body["snippet"]["title"] = title
                logger.warning(
                    f"Same-title video {v['id']} is {have_s:.0f}s vs this "
                    f"{want_s:.0f}s render — uploading as '{title[:60]}'")
                break
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
    _reassert_after_processing(youtube, video_id, effective_privacy,
                               publish_at=scheduled_at)

    return video_id
