"""
GhostDirector — Title/Thumbnail A-B Mechanics (plan item A6)

Rotate title/thumbnail variants on live uploads and record every swap, so the
packaging leaderboard (analytics.join_ctr_to_packaging) can rank variants by
real CTR. Mechanics only — winner selection needs CTR data, which arrives via
analytics.import_manual_ctr() (Studio import) or future API availability.

All state mutations are unit-testable: functions take/return plain dicts and
only touch network through update_title/update_thumbnail, which reuse the
uploader's authenticated service.

Usage (operator, after a video has accumulated impressions):
    python main.py --abset <videoId> --title-index 1
    python main.py --abset <videoId> --thumb thumbnail_v2_split.png
    python main.py --ableaderboard
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from utils.logger import get_logger

log = get_logger("abtest")


# ──────────────────────────────────────────────
# Variant lookup from a project's metadata.json
# ──────────────────────────────────────────────
def title_variant(metadata: dict, index: int) -> str | None:
    """The title variant at `index` (0 = live title itself)."""
    variants = [v for v in (metadata.get("title_variants") or []) if v]
    if 0 <= index < len(variants):
        return variants[index].strip()
    return None


def thumbnail_variant(project_dir: Path, filename: str) -> Path | None:
    """Resolve a thumbnail variant filename inside the project dir."""
    if not filename:
        return None
    candidate = project_dir / filename
    return candidate if candidate.exists() else None


# ──────────────────────────────────────────────
# Swap recording (state; testable)
# ──────────────────────────────────────────────
def record_swap(
    state: dict,
    video_url: str,
    kind: str,                     # "title" | "thumbnail"
    from_value: str,
    to_value: str,
    note: str | None = None,
) -> dict:
    """Append an A/B swap to the packaging log (in place; caller persists).

    The packaging entry's `title_used`/`thumbnail_variant` is updated to the
    new value so the CTR join reflects what actually went live.
    """
    entry = state.setdefault("ab_swaps", [])
    entry.append({
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "video_url": video_url,
        "kind": kind,
        "from": from_value,
        "to": to_value,
        "note": note or "",
    })
    state["ab_swaps"] = entry[-300:]

    # Keep packaging_log in sync with reality for the CTR join.
    for p in state.get("packaging_log", []):
        if p.get("youtube_url") == video_url:
            if kind == "title":
                p["title_used"] = to_value
                p["title_swapped_from"] = from_value
            else:
                p["thumbnail_variant"] = to_value
                p["thumbnail_swapped_from"] = from_value
    return state


# ──────────────────────────────────────────────
# Live updates (network; operator-run)
# ──────────────────────────────────────────────
def update_title(video_id: str, new_title: str, channel: str | None = None) -> bool:
    """videos.update on the live title (40 quota units).

    FIX-053: videos.update REPLACES the whole snippet, so the existing
    description/tags/category are fetched first and re-sent with the new
    title — otherwise a title swap silently wipes the metadata.
    """
    try:
        from pipeline.uploader import _yt_service_with_scopes
        youtube = _yt_service_with_scopes(channel=channel)
        current = youtube.videos().list(
            part="snippet", id=video_id
        ).execute()
        items = current.get("items", [])
        if not items:
            log.error(f"Title update failed: video {video_id} not found")
            return False
        snippet = items[0]["snippet"]
        snippet["title"] = new_title
        youtube.videos().update(
            part="snippet",
            body={"id": video_id, "snippet": snippet},
        ).execute()
        log.info(f"Title updated on {video_id}: '{new_title[:50]}'")
        return True
    except Exception as e:
        log.error(f"Title update failed: {e}")
        return False


def update_thumbnail(video_id: str, thumb_path: Path, channel: str | None = None) -> bool:
    """thumbnails.set on the live thumbnail (~50 quota units)."""
    try:
        from pipeline.uploader import _yt_service_with_scopes
        from googleapiclient.http import MediaFileUpload
        youtube = _yt_service_with_scopes(channel=channel)
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumb_path)),
        ).execute()
        log.info(f"Thumbnail updated on {video_id}: {thumb_path.name}")
        return True
    except Exception as e:
        log.error(f"Thumbnail update failed: {e}")
        return False


def ab_set(video_id: str, project_dir: Path,
           title_index: int | None = None,
           thumb_file: str | None = None,
           channel: str = "default") -> dict:
    """Apply an A/B swap to a live video and record it in channel state.

    Returns {"ok": bool, "changes": [...]}.
    """
    from utils import channel_state

    metadata_path = project_dir / "metadata.json"
    metadata = (json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata_path.exists() else {})
    video_url = f"https://youtu.be/{video_id}"
    changes: list[str] = []

    if title_index is not None:
        new_title = title_variant(metadata, title_index)
        if not new_title:
            return {"ok": False, "changes": [f"title variant #{title_index} not found"]}
        if update_title(video_id, new_title):
            state = channel_state._load(channel)
            record_swap(state, video_url, "title",
                        metadata.get("title", ""), new_title,
                        note=f"variant #{title_index}")
            channel_state._save(state, channel)
            changes.append(f"title → variant #{title_index}")

    if thumb_file:
        thumb = thumbnail_variant(project_dir, thumb_file)
        if not thumb:
            return {"ok": False, "changes": [f"thumbnail '{thumb_file}' not found"]}
        if update_thumbnail(video_id, thumb):
            state = channel_state._load(channel)
            record_swap(state, video_url, "thumbnail",
                        metadata.get("thumbnail_used", "thumbnail.png"), thumb.name)
            channel_state._save(state, channel)
            changes.append(f"thumbnail → {thumb.name}")

    return {"ok": bool(changes), "changes": changes}


# ──────────────────────────────────────────────
# Leaderboard basis (pure; needs CTR data)
# ──────────────────────────────────────────────
def ab_leaderboard(state: dict, packaging_entries: list[dict]) -> dict:
    """Summarize A/B activity + which variants have enough CTR data to rank."""
    swaps = state.get("ab_swaps", [])
    ranked = []
    for p in packaging_entries:
        if p.get("ctr") is not None:
            ranked.append({
                "video": p.get("youtube_url"),
                "title": p.get("title_used", "")[:60],
                "thumbnail": p.get("thumbnail_variant"),
                "ctr": p["ctr"],
                "swapped": any(s.get("video_url") == p.get("youtube_url")
                               for s in swaps),
            })
    ranked.sort(key=lambda r: -r["ctr"])
    return {
        "swaps_logged": len(swaps),
        "videos_with_ctr": len(ranked),
        "ranking": ranked[:10],
        "note": ("Import CTR via analytics.import_manual_ctr (Studio) — "
                 "browse impressions are not served by the Analytics API.")
                if not ranked else None,
    }
