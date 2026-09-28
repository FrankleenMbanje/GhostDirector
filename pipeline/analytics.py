"""
GhostDirector — Analytics Learning Loop (Phase 2.7 / plan item A3)

The moat: retention data drives the next script. This module closes the loop
between the factory and the channel:

  1. YouTube Analytics API → per-video audienceWatchRatio curve
     (percent of viewers remaining at each elapsed-video-time-ratio bucket).
  2. Join that curve against the rendered `timeline.json` scene starts
     (written by the assembler, FIX-031) → which SCENE loses the most viewers.
  3. Join impressions CTR against the packaging log (FIX-029) → which
     title/thumbnail variant actually earns clicks.
  4. Persist everything to db/analytics.json so the Studio Growth tab (A8)
     and future edit rules can read it without touching the API again.

Pure analytics helpers (percentile buckets, retention curves, scene joins)
are deterministic and unit-tested without network; only `sync_channel()`
talks to Google.

OAuth note: Analytics requires the youtube.readonly + yt-analytics.readonly
scopes on top of the upload scopes. uploader._yt_service_with_scopes() runs
the flow (or reuses the stored token when its scopes already cover it) and
falls back gracefully when client_secrets.json is not configured.

CTR honesty note: the YouTube Analytics API does NOT expose browse
impressions or CTR (that data is Studio-only, verified against the official
metrics list). Views/retention/watch-time/subs come from the API; CTR is
imported from Studio via import_manual_ctr() — ten numbers per sync — which
keeps the packaging join real instead of guessed.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from utils.logger import get_logger

log = get_logger("analytics")

STORE_PATH = config.DB_DIR / "analytics.json"
LOOKBACK_DAYS = 90
MIN_VIEWS_FOR_CURVE = 25   # below this the retention curve is noise
MIN_VIEWS_FOR_CTR = 100    # below this CTR swings wildly


# ──────────────────────────────────────────────
# Store
# ──────────────────────────────────────────────
def load_store(path: Path | None = None) -> dict:
    # Path resolved at CALL time from config so tests can redirect the store
    # (a def-time default would bake the real path in forever).
    path = Path(path) if path else config.ANALYTICS_STORE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"videos": {}}
    except FileNotFoundError:
        return {"videos": {}}
    except Exception as e:
        log.warning(f"analytics store unreadable ({e}); starting fresh")
        return {"videos": {}}


def save_store(store: dict, path: Path | None = None) -> None:
    path = Path(path) if path else config.ANALYTICS_STORE_PATH
    path.write_text(json.dumps(store, indent=1, ensure_ascii=False), encoding="utf-8")


# ──────────────────────────────────────────────
# Retention math (pure, unit-tested)
# ──────────────────────────────────────────────
def eligible_elapsed_percent(length_seconds: float) -> list[float]:
    """The 7 elapsed-video-time-ratio buckets YouTube actually serves.

    The API only returns data for ratios in [0.05 .. 1.00] (plus 0), snapped
    to multiples of 0.05 that exist for the video's length. Asking for other
    buckets yields missing/zero rows that would poison the curve.
    """
    return [0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5,
            0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0]


def relative_retention_curve(elapsed_rows: list[dict]) -> list[dict]:
    """Normalize an audienceWatchRatio response into a clean curve.

    `elapsed_rows` items: {"elapsedVideoTimeRatio": "0.05",
                           "viewPercentRemaining": 0.83}
    Returns [{"ratio": 0.05, "remaining_percent": 83.0}, ...] sorted,
    monotonic-clamped so later points never exceed earlier ones (API noise
    occasionally reports small upticks; clamping keeps the join honest).
    """
    points: list[tuple[float, float]] = []
    for row in elapsed_rows or []:
        try:
            ratio = float(row.get("elapsedVideoTimeRatio", "nan"))
            rem = float(row.get("viewPercentRemaining", "nan"))
        except (TypeError, ValueError):
            continue
        if 0.0 <= ratio <= 1.0 and 0.0 <= rem <= 100.0:
            points.append((ratio, rem * 100.0 if rem <= 1.0 else rem))
    points.sort()

    curve: list[dict] = []
    ceiling = 100.0
    for ratio, rem in points:
        rem = min(rem, ceiling)
        ceiling = rem
        curve.append({"ratio": round(ratio, 4), "remaining_percent": round(rem, 2)})
    return curve


def join_retention_to_scenes(
    curve: list[dict],
    scene_starts: list[float],
    video_length_seconds: float,
) -> dict:
    """Attribute retention loss to scenes.

    Scene i's window = [scene_starts[i], scene_starts[i+1] or video end).
    Drop in remaining-percent over that window is the viewers lost there.
    Returns {"scene_drops": [...], "worst_scene": {...}, "coverage": {...}}.
    """
    if not curve or not scene_starts or video_length_seconds <= 0:
        return {"scene_drops": [], "worst_scene": None, "coverage": {}}

    def remaining_at(seconds: float) -> float | None:
        ratio = seconds / video_length_seconds
        best = None
        for point in curve:
            if point["ratio"] <= ratio + 1e-9:
                best = point["remaining_percent"]
            else:
                break
        return best

    drops = []
    for i, start in enumerate(scene_starts):
        end = scene_starts[i + 1] if i + 1 < len(scene_starts) else video_length_seconds
        r_start = remaining_at(start)
        r_end = remaining_at(min(end, video_length_seconds))
        if r_start is None or r_end is None:
            drops.append({"scene": i + 1, "start_seconds": round(start, 2),
                          "drop_percent": None})
            continue
        drops.append({
            "scene": i + 1,
            "start_seconds": round(start, 2),
            "end_seconds": round(end, 2),
            "start_remaining": round(r_start, 2),
            "end_remaining": round(r_end, 2),
            "drop_percent": round(max(r_start - r_end, 0.0), 2),
        })

    scored = [d for d in drops if d.get("drop_percent") is not None]
    worst = max(scored, key=lambda d: d["drop_percent"]) if scored else None
    coverage = {
        "scene_windows_scored": len(scored),
        "scene_windows_total": len(drops),
        "curve_points": len(curve),
    }
    return {"scene_drops": drops, "worst_scene": worst, "coverage": coverage}


def summarize_video_analytics(metrics: dict) -> dict:
    """Cheap health summary used by the CLI and the Growth tab."""
    avd = metrics.get("average_view_duration_seconds") or 0.0
    length = metrics.get("length_seconds") or 0.0
    return {
        "views": metrics.get("views", 0),
        "average_view_duration_seconds": round(avd, 1),
        "retention_percent": round((avd / length) * 100, 1) if length else None,
        "ctr_percent": metrics.get("ctr_percent"),
        "watch_minutes": metrics.get("estimated_minutes_watched", 0),
    }


# ──────────────────────────────────────────────
# Packaging CTR join (A6 foundation)
# ──────────────────────────────────────────────
def join_ctr_to_packaging(
    analytics_store: dict,
    packaging_log: list[dict],
) -> list[dict]:
    """Fill `ctr` on packaging-log entries from synced analytics rows.

    Matches by youtube URL (youtu.be/<id>) or exact title. Only fills when
    the video has enough impressions for the CTR to mean anything; entries
    below MIN_VIEWS_FOR_CTR keep ctr=None.
    """
    videos = analytics_store.get("videos", {})
    for entry in packaging_log or []:
        if entry.get("ctr") is not None:
            continue
        row = None
        url = entry.get("youtube_url") or ""
        if "youtu" in url:
            vid = url.rstrip("/").split("/")[-1].split("?")[0]
            row = videos.get(vid)
        if row is None:
            title = (entry.get("title_used") or "").strip().lower()
            for v in videos.values():
                if (v.get("title") or "").strip().lower() == title:
                    row = v
                    break
        if not row:
            continue
        views = row.get("views", 0)
        ctr = row.get("ctr_percent")
        if ctr is not None and views >= MIN_VIEWS_FOR_CTR:
            entry["ctr"] = ctr
            entry["ctr_views_at_join"] = views
            entry["ctr_joined_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return packaging_log


def packaging_leaderboard(analytics_store: dict, packaging_log: list[dict]) -> dict:
    """Which thumbnail/title variants actually earn clicks (once data exists)."""
    by_thumb: dict[str, list[float]] = {}
    for entry in packaging_log or []:
        if entry.get("ctr") is None:
            continue
        thumb = entry.get("thumbnail_variant") or "unknown"
        by_thumb.setdefault(thumb, []).append(entry["ctr"])

    def _avg(vals: list[float]) -> float:
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "thumbnail_ctr": {k: {"avg_ctr": _avg(v), "samples": len(v)}
                          for k, v in sorted(by_thumb.items(),
                                             key=lambda kv: -_sum(kv[1]))},
        "logged_entries": len(packaging_log or []),
        "with_ctr": sum(1 for e in (packaging_log or []) if e.get("ctr") is not None),
    }


def _sum(vals: list[float]) -> float:
    return sum(vals)


# ──────────────────────────────────────────────
# Live sync (network; operator-run)
# ──────────────────────────────────────────────
def _video_length_seconds(youtube, video_id: str) -> float | None:
    """contentDetails.duration (ISO-8601) → seconds. 1 API unit."""
    try:
        resp = youtube.videos().list(
            part="contentDetails,snippet", id=video_id
        ).execute()
        items = resp.get("items", [])
        if not items:
            return None
        duration = items[0]["contentDetails"].get("duration", "")
        title = items[0].get("snippet", {}).get("title", "")
        return _iso8601_to_seconds(duration), title
    except Exception as e:
        log.warning(f"length lookup failed for {video_id}: {e}")
        return None


def _iso8601_to_seconds(duration: str) -> float:
    import re
    m = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$",
                 duration or "")
    if not m:
        return 0.0
    d, h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _list_my_uploads(youtube) -> list[dict]:
    """Recent uploads via the uploads playlist (2 API units, no search cost)."""
    try:
        ch = youtube.channels().list(part="contentDetails", mine=True).execute()
        items = ch.get("items", [])
        if not items:
            return []
        uploads_playlist = items[0]["contentDetails"]["relatedPlaylists"].get("uploads")
        if not uploads_playlist:
            return []
        vids = []
        page_token = None
        for _ in range(5):  # up to 250 videos
            resp = youtube.playlistItems().list(
                part="contentDetails,snippet",
                playlistId=uploads_playlist,
                maxResults=50,
                pageToken=page_token,
            ).execute()
            for it in resp.get("items", []):
                vid = it["contentDetails"]["videoId"]
                vids.append({
                    "video_id": vid,
                    "title": it["snippet"].get("title", ""),
                    "published_at": it["contentDetails"].get("videoPublishedAt", ""),
                })
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return vids
    except Exception as e:
        log.warning(f"upload listing failed: {e}")
        return []


def sync_channel(channel: str = "default", days: int = LOOKBACK_DAYS,
                 write: bool = True) -> dict:
    """Pull analytics for every upload in the lookback window and join.

    Returns {"synced": n, "skipped": n, "store": path, "notes": [...]}.
    Requires OAuth (client_secrets.json); raises a clear error otherwise.
    """
    from pipeline.uploader import _yt_service_with_scopes

    youtube = _yt_service_with_scopes(channel=channel)   # Data API (uploads + lengths)
    yta = _yt_analytics_service(channel=channel)         # Analytics API (curves)

    uploads = _list_my_uploads(youtube)
    if not uploads:
        return {"synced": 0, "skipped": 0, "notes": ["No uploads found on this channel."]}

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    store = load_store()
    notes: list[str] = []
    synced = skipped = 0

    for up in uploads:
        vid = up["video_id"]
        try:
            metrics = _video_metrics(yta, vid, start.isoformat(), end.isoformat())
        except Exception as e:
            notes.append(f"{vid}: metrics failed ({e})")
            skipped += 1
            continue
        if not metrics or metrics.get("views", 0) == 0:
            notes.append(f"{vid}: no views in window yet")
            skipped += 1
            continue

        length_result = _video_length_seconds(youtube, vid)
        length_seconds, live_title = (length_result if length_result else (None, up["title"]))
        if not length_seconds:
            skipped += 1
            continue

        curve = []
        if metrics["views"] >= MIN_VIEWS_FOR_CURVE:
            try:
                curve = relative_retention_curve(_watch_ratio_rows(yta, vid,
                                                                   start.isoformat(),
                                                                   end.isoformat()))
            except Exception as e:
                notes.append(f"{vid}: retention curve unavailable ({e})")

        # Join to the rendered timeline when the project dir can be found by title
        scene_starts: list[float] = []
        proj = _find_project_for_title(live_title)
        timeline = _load_timeline(proj) if proj else None
        if timeline:
            scene_starts = [sc["start_seconds"] for sc in timeline.get("scenes", [])]
        join = (join_retention_to_scenes(curve, scene_starts, length_seconds)
                if curve and scene_starts else
                {"scene_drops": [], "worst_scene": None, "coverage": {}})

        store["videos"][vid] = {
            "title": live_title,
            "channel": channel,
            "published_at": up["published_at"],
            "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "length_seconds": length_seconds,
            **metrics,
            "retention_curve": curve,
            "scene_drops": join["scene_drops"],
            "worst_scene": join["worst_scene"],
            "project_dir": str(proj) if proj else None,
        }
        synced += 1

    # CTR → packaging log (per-channel state file)
    try:
        from utils import channel_state
        state_path = channel_state._path_for(channel)
        state = channel_state._load(channel)
        joined = join_ctr_to_packaging(store, state.get("packaging_log", []))
        if joined:
            state["packaging_log"] = joined
            channel_state._save(state, channel)
            notes.append(f"CTR joined into packaging log ({state_path.name})")
    except Exception as e:
        notes.append(f"packaging CTR join failed: {e}")

    store["last_sync"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channel": channel,
        "synced": synced,
        "skipped": skipped,
    }
    if write:
        save_store(store)
    log.info(f"Analytics sync: {synced} synced, {skipped} skipped")
    return {"synced": synced, "skipped": skipped,
            "store": str(config.ANALYTICS_STORE_PATH), "notes": notes}


def _video_metrics(yta, video_id: str, start: str, end: str) -> dict | None:
    """Core metrics for one video: views, AVD, watch minutes, subs gained.

    (CTR is deliberately absent: the API does not serve impressions data.
    It arrives via import_manual_ctr from YouTube Studio numbers.)
    """
    resp = yta.reports().query(
        ids="channel==MINE",
        startDate=start,
        endDate=end,
        metrics="views,estimatedMinutesWatched,averageViewDuration,"
                "averageViewPercentage,subscribersGained",
        dimensions="video",
        filters=f"video=={video_id}",
    ).execute()
    rows = resp.get("rows", [])
    if not rows:
        return None
    cols = resp["columnHeaders"]
    vals = dict(zip((c["name"] for c in cols), rows[0]))
    return {
        "views": int(vals.get("views", 0)),
        "estimated_minutes_watched": int(vals.get("estimatedMinutesWatched", 0)),
        "average_view_duration_seconds": round(float(vals.get("averageViewDuration", 0)), 1),
        "retention_percent_view": round(float(vals.get("averageViewPercentage", 0)), 2),
        "subscribers_gained": int(vals.get("subscribersGained", 0)),
    }


def _watch_ratio_rows(yta, video_id: str, start: str, end: str) -> list[dict]:
    """audienceWatchRatio rows for one video over the window."""
    resp = yta.reports().query(
        ids="channel==MINE",
        startDate=start,
        endDate=end,
        metrics="audienceWatchRatio",
        dimensions="elapsedVideoTimeRatio",
        filters=f"video=={video_id}",
    ).execute()
    cols = [c["name"] for c in resp.get("columnHeaders", [])]
    rows = []
    for r in resp.get("rows", []):
        d = dict(zip(cols, r))
        rows.append({
            "elapsedVideoTimeRatio": d.get("elapsedVideoTimeRatio"),
            "viewPercentRemaining": d.get("audienceWatchRatio"),
        })
    return rows


# ──────────────────────────────────────────────
# Manual CTR import (Studio → packaging join)
# ──────────────────────────────────────────────
def _video_id_from(value: str) -> str:
    """Accept a bare id, a youtu.be URL, or a full watch URL."""
    v = (value or "").strip()
    if "youtu" in v:
        return v.rstrip("/").split("/")[-1].split("?")[0]
    return v


def import_manual_ctr(ctr_map: dict[str, float], channel: str = "default",
                      write: bool = True) -> dict:
    """Import impressions-CTR numbers read from YouTube Studio.

    ctr_map: {"<video id or youtu.be url>": <ctr percent>}
    Updates db/analytics.json rows (creating lightweight rows for unknown
    ids) and re-runs the packaging CTR join. Returns a summary.
    """
    store = load_store()
    updated = 0
    for key, ctr in (ctr_map or {}).items():
        vid = _video_id_from(key)
        if not vid:
            continue
        row = store["videos"].setdefault(vid, {"title": None, "views": 0})
        try:
            row["ctr_percent"] = round(float(ctr), 2)
        except (TypeError, ValueError):
            continue
        row["ctr_source"] = "studio_manual"
        row["ctr_synced_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        updated += 1

    joined = 0
    try:
        from utils import channel_state
        state = channel_state._load(channel)
        packaging = state.get("packaging_log", [])
        join_ctr_to_packaging(store, packaging)
        joined = sum(1 for e in packaging if e.get("ctr") is not None)
        state["packaging_log"] = packaging
        channel_state._save(state, channel)
    except Exception as e:
        log.warning(f"packaging CTR join failed during manual import: {e}")

    if write:
        save_store(store)
    log.info(f"Manual CTR import: {updated} rows updated, {joined} packaging entries carry CTR")
    return {"videos_updated": updated, "packaging_entries_with_ctr": joined}


def _yt_analytics_service(channel: str | None = None):
    """YouTube Analytics API service (read-only)."""
    from pipeline.uploader import _yt_service_with_scopes
    # Analytics uses its own build but the same OAuth creds; re-using the
    # uploader's authenticated service keeps the token file single-source.
    youtube = _yt_service_with_scopes(channel=channel)
    from googleapiclient.discovery import build
    # The credentials object is embedded in the built service; rebuild cheaply.
    creds = youtube._http.credentials if getattr(youtube, "_http", None) else None
    if creds is None:
        raise RuntimeError("Could not derive Analytics credentials from YouTube service")
    return build("youtubeAnalytics", "v2", credentials=creds)


def _find_project_for_title(title: str) -> Path | None:
    """Match an uploaded title back to its output/<project> dir (best effort)."""
    if not title:
        return None
    words = [w.lower().strip(".,:;!?\"'") for w in title.split() if len(w) > 3]
    best: tuple[int, Path] | None = None
    for proj_dir in config.OUTPUT_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        slug_words = set(proj_dir.name.lower().replace("-", "_").split("_"))
        score = sum(1 for w in words if w in slug_words)
        if score and (best is None or score > best[0]):
            best = (score, proj_dir)
    return best[1] if best and best[0] >= 3 else None


def _load_timeline(project_dir: Path | None) -> dict | None:
    if not project_dir:
        return None
    tl = project_dir / "timeline.json"
    if not tl.exists():
        return None
    try:
        return json.loads(tl.read_text(encoding="utf-8"))
    except Exception:
        return None
