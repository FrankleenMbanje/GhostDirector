"""
GhostDirector — Channel Audit & Competitor Discovery (plan item A10)

The vidIQ/Ninjas layer, production-aware:
  1. audit()              — MY channel: real public stats resolved from the first
                            live upload (videos.list → channelId → channels.list),
                            joined with the operator profile (db/channel_profile.json:
                            niche, cadence, goal) and db/analytics.json when synced.
  2. suggest_channels()   — competitor discovery: search YouTube for the niche's
                            competitive keywords, return a ranked candidate list
                            (subscribers, views, video counts) ready for --add-competitor.
  3. compare_channels()   — my stats vs each watch-list competitor: subs, uploads,
                            views/video and the views/subs efficiency ratio.
  4. recommendations()    — the "do this next" list: data-driven rows from the audit
                            + the standing playbooks (video length, cadence, chapters,
                            pinned comment, Shorts funnel, paid-tier unlock).

Network calls are isolated in two small functions (_resolve_my_channel via the
Data API when OAuth exists, _search_channels via yt-dlp which needs NO quota);
everything else is deterministic and unit-testable offline with fixtures.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from utils.logger import get_logger

log = get_logger("channel_audit")

PROFILE_PATH = config.DB_DIR / "channel_profile.json"

# Benchmarks used by recommendations() (public vidIQ/vidninja-style heuristics,
# tuned for documentary/true-crime niches)
REC_BENCHMARKS = {
    "min_video_length_for_watch_pages": 480,   # 8:00 — unlocks mid-rolls + 2 ad slots
    "weekly_upload_cadence": 1,                # ≥1/week keeps the algorithm fed
    "max_days_since_upload": 14,               # gap that triggers the cadence alarm
    "ctr_target_percent": 4.0,
    "avd_target_ratio": 0.42,                  # avg view duration / length
}


# ──────────────────────────────────────────────
# Operator profile (the only user-maintained input)
# ──────────────────────────────────────────────
def load_profile() -> dict:
    """db/channel_profile.json — niche, cadence, goal, channel handle/URL."""
    try:
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"channel profile unreadable ({e})")
    return {}


def save_profile(profile: dict) -> Path:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, indent=1, ensure_ascii=False), encoding="utf-8")
    return PROFILE_PATH


def ensure_profile(niche: str | None = None, channel_url: str | None = None) -> dict:
    """Create the profile on first use (interactive defaults), update if given."""
    profile = load_profile()
    if niche:
        profile["niche"] = niche
    if channel_url:
        profile["channel_url"] = channel_url
    if not profile.get("niche"):
        profile["niche"] = "documentary true crime business"
    profile.setdefault("goal", "monetization + growth")
    profile.setdefault("uploads_per_week", 1)
    profile.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    save_profile(profile)
    return profile


# ──────────────────────────────────────────────
# 1) My-channel audit
# ──────────────────────────────────────────────
def _resolve_my_channel(channel: str | None = None) -> dict | None:
    """Resolve my channel via the first live upload (videos.list → channels.list).

    Returns {"channel_id", "title", "subscribers", "total_views", "video_count",
             "uploads_playlist", "published_at", "handle"} or None when no
    OAuth/upload exists yet — never raises.
    """
    try:
        from pipeline.uploader import _yt_service_with_scopes
        youtube = _yt_service_with_scopes(channel=channel)
        from pipeline.analytics import _list_my_uploads
        uploads = _list_my_uploads(youtube)
        if not uploads:
            return None
        first_id = uploads[0]["video_id"]
        vid = youtube.videos().list(part="snippet", id=first_id).execute()
        cid = vid["items"][0]["snippet"]["channelId"]
        ch = youtube.channels().list(part="snippet,statistics", id=cid).execute()
        item = ch["items"][0]
        sn, st = item["snippet"], item["statistics"]
        return {
            "channel_id": cid,
            "title": sn.get("title"),
            "handle": sn.get("customUrl"),
            "subscribers": int(st.get("subscriberCount", 0)),
            "total_views": int(st.get("viewCount", 0)),
            "video_count": int(st.get("videoCount", 0)),
            "uploads_playlist": item.get("contentDetails", {}).get(
                "relatedPlaylists", {}).get("uploads"),
            "published_at": sn.get("publishedAt"),
        }
    except Exception as e:
        log.info(f"channel resolve skipped ({type(e).__name__}: {e})")
        return None


def audit() -> dict:
    """Full channel audit: public stats + profile + analytics + health rows.

    Works in three honest tiers:
      - OAuth + uploads  → real public stats joined in
      - analytics synced → retention/CTR health rows
      - neither          → profile-only audit with the standing playbooks
    """
    profile = ensure_profile()
    live = _resolve_my_channel()
    analytics = _load_analytics_summary()

    audit_rows: list[dict] = []
    if live:
        audit_rows.append({
            "check": "Channel identity",
            "status": "ok",
            "detail": f"{live['title']} ({live['subscribers']:,} subs, {live['video_count']} videos)",
        })
    else:
        audit_rows.append({
            "check": "Channel identity",
            "status": "action",
            "detail": "No OAuth/upload visible — set up client_secrets.json and publish video #1",
        })

    # Cadence health from the local packaging log (works without OAuth)
    from utils import channel_state
    state = channel_state._load("default")
    pkg = state.get("packaging_log", [])
    dates = sorted({e["date"][:10] for e in pkg if e.get("date")})
    if len(dates) >= 2:
        gap_days = (datetime.fromisoformat(dates[-1]) -
                    datetime.fromisoformat(dates[-2])).days
        if gap_days > REC_BENCHMARKS["max_days_since_upload"]:
            audit_rows.append({
                "check": "Upload cadence",
                "status": "warn",
                "detail": f"{gap_days} days between last two productions "
                          f"(target ≤{REC_BENCHMARKS['max_days_since_upload']})",
            })
        else:
            audit_rows.append({
                "check": "Upload cadence",
                "status": "ok",
                "detail": f"{gap_days} days between last two productions",
            })
    else:
        audit_rows.append({
            "check": "Upload cadence",
            "status": "action",
            "detail": "Not enough production history yet — ship video #2",
        })

    # Length check: current template tops out at ~3:30 (no mid-roll ads)
    audit_rows.append({
        "check": "Video length",
        "status": "warn",
        "detail": f"Celebrity template renders ~3:30 (<{REC_BENCHMARKS['min_video_length_for_watch_pages'] // 60}:00) "
                  f"— no mid-roll ad slots; build the 8-min template when daily quota allows",
    })

    if analytics.get("videos"):
        avg_ctr = _mean([v.get("ctr_percent") for v in analytics["videos"].values()
                         if v.get("ctr_percent") is not None])
        if avg_ctr is not None:
            status = "ok" if avg_ctr >= REC_BENCHMARKS["ctr_target_percent"] else "warn"
            audit_rows.append({
                "check": "Impressions CTR",
                "status": status,
                "detail": f"{avg_ctr}% average (target ≥{REC_BENCHMARKS['ctr_target_percent']}%) — "
                          f"import Studio numbers via --import-ctr",
            })
        worst = [(v.get("worst_scene"), v["title"]) for v in analytics["videos"].values()
                 if v.get("worst_scene")]
        if worst:
            audit_rows.append({
                "check": "Retention",
                "status": "info",
                "detail": f"Worst scene identified on {len(worst)} video(s) — "
                          f"see the Growth tab for scene-level fixes",
            })

    # Standing playbooks (always relevant, data-independent)
    audit_rows.append({
        "check": "SEO",
        "status": "info",
        "detail": "Autocomplete tags are injected into every render; "
                  "verify the top tag actually has search volume in Studio → Research",
    })
    audit_rows.append({
        "check": "Shorts funnel",
        "status": "info",
        "detail": "Post the Short 24h after the master with a pinned-comment link",
    })

    return {
        "profile": profile,
        "channel": live,
        "analytics_summary": analytics,
        "audit_rows": audit_rows,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _load_analytics_summary() -> dict:
    """Read db/analytics.json (empty store is fine — the audit degrades gracefully)."""
    try:
        data = json.loads(config.ANALYTICS_STORE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("videos"), dict):
            return {"videos": data["videos"], "last_sync": data.get("last_sync")}
    except Exception:
        pass
    return {"videos": {}, "last_sync": None}


def _mean(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


# ──────────────────────────────────────────────
# 2) Competitor discovery (vidIQ-style "find my competitors")
# ──────────────────────────────────────────────
def _search_channels(query: str, max_results: int = 10) -> list[dict]:
    """Search YouTube for channels matching a niche query (yt-dlp, zero quota).

    Uses yt-dlp's ytsearch filter so no API key is needed. Returns ranked
    rows: channel name, url, subs, total views, video count.
    """
    import subprocess

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--flat-playlist", "--print",
        "%(channel_id)s|%(channel)s|%(channel_follower_count)s|%(view_count)s",
        "--playlist-items", f"1:{max_results * 2}",   # extra rows — dedupe channels
        f"ytsearch{max_results * 2}:{query}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"channel search timed out for '{query}'") from e
    if proc.returncode != 0:
        raise RuntimeError(f"channel search failed: {proc.stderr.strip()[:200]}")

    seen: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        cid, name, subs, views = parts[0], parts[1], parts[2], parts[3]

        def _int(v: str) -> int:
            try:
                return int(float(v or 0))
            except (TypeError, ValueError):
                return 0

        if cid in seen:
            continue
        seen[cid] = {
            "channel_id": cid,
            "name": name,
            "url": f"https://www.youtube.com/channel/{cid}",
            "subscribers": _int(subs),
            "views_seen": _int(views),   # views of the video that surfaced them
        }
    rows = sorted(seen.values(), key=lambda r: -r["subscribers"])
    return rows[:max_results]


def suggest_channels(niche: str | None = None, max_results: int = 8) -> dict:
    """Ranked candidate competitors for the niche → ready for --add-competitor.

    Queries multiple competitive keywords derived from the niche string.
    Never raises; per-query failures are collected.
    """
    profile = ensure_profile()
    niche = niche or profile.get("niche", "documentary true crime business")

    # Competitive keywords: niche words + the genre's proven search phrases
    words = [w for w in niche.split() if len(w) > 2]
    queries = [
        niche,
        f"{words[0] if words else niche} documentary",
        f"rise and fall {words[-1] if words else ''}".strip(),
    ]
    seen: dict[str, dict] = {}
    errors: list[str] = []
    for q in queries:
        try:
            for row in _search_channels(q, max_results=max_results):
                seen[row["channel_id"]] = row
        except Exception as e:
            errors.append(f"'{q}': {e}")

    rows = sorted(seen.values(), key=lambda r: -r["subscribers"])[:max_results]
    # Suggested handle format for --add-competitor
    for r in rows:
        r["add_command"] = f'python main.py --add-competitor "{r["url"]}"'
    return {"niche": niche, "candidates": rows, "errors": errors}


# ──────────────────────────────────────────────
# 3) My channel vs competitors
# ──────────────────────────────────────────────
def _fetch_channel_stats(channel_url: str) -> dict:
    """Public stats for one channel via yt-dlp (subs, video count)."""
    import subprocess

    if not channel_url.startswith("http"):
        channel_url = f"https://www.youtube.com/{channel_url if channel_url.startswith('@') else '@' + channel_url}"
    cmd = [sys.executable, "-m", "yt_dlp", "--playlist-items", "1",
           "--print", "%(channel_follower_count)s|%(channel)s",
           "--skip-download", f"{channel_url}/videos"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"stats failed for {channel_url}: {proc.stderr.strip()[:150]}")
    subs = 0
    name = channel_url
    for line in proc.stdout.splitlines():
        parts = line.split("|")
        if len(parts) >= 2:
            try:
                subs = int(float(parts[0] or 0))
            except (TypeError, ValueError):
                subs = 0
            name = parts[1]
            break
    return {"url": channel_url, "name": name, "subscribers": subs}


def compare_channels(watchlist: list[str] | None = None,
                     include_me: bool = True) -> dict:
    """My stats vs every watch-list competitor (views/subs efficiency ratio)."""
    from pipeline.topic_scout import load_watchlist
    channels = watchlist if watchlist is not None else load_watchlist()
    me = _resolve_my_channel() if include_me else None
    rows: list[dict] = []
    if me:
        rows.append({
            "name": f"{me['title']} (ME)",
            "subscribers": me["subscribers"],
            "video_count": me["video_count"],
            "total_views": me["total_views"],
            "views_per_video": round(me["total_views"] / max(me["video_count"], 1)),
            "views_per_sub": round(me["total_views"] / max(me["subscribers"], 1), 2),
            "is_me": True,
        })
    for ch in channels:
        try:
            stats = _fetch_channel_stats(ch)
            rows.append({
                "name": stats["name"],
                "subscribers": stats["subscribers"],
                "is_me": False,
                "url": stats["url"],
            })
        except Exception as e:
            rows.append({"name": ch, "error": str(e), "is_me": False})
    rows.sort(key=lambda r: -r.get("subscribers", 0))
    return {"compared": len(rows), "rows": rows}


# ──────────────────────────────────────────────
# 4) Recommendations ("tell me what to do")
# ──────────────────────────────────────────────
def recommendations(audit_result: dict | None = None) -> list[str]:
    """Prioritized do-next list: standing playbooks + data-driven actions."""
    a = audit_result or audit()
    actions: list[str] = []

    has_live = a.get("channel") is not None
    if not has_live:
        actions.insert(0,
            "Create Google OAuth credentials (client_secrets.json) — unlocks upload, "
            "analytics sync, A/B swaps, and the audit's live data"
        )
    analytics = a.get("analytics_summary") or {}
    if not analytics.get("videos"):
        # Slot after the OAuth action (index 0) when that exists — OAuth gates
        # everything, so it always leads the list.
        actions.insert(1 if not has_live else 0,
            "After the first 48h of video #1: import CTR from Studio (--import-ctr) "
            "and run --sync-analytics to feed the retention loop"
        )
    else:
        actions.append(
            "Growth tab → review the worst-scene report and re-render that scene's "
            "shot; rotate packaging only when CTR < 4%"
        )

    actions.append(
        "Ship video #2 within 7 days — cadence is the strongest lever at 0 subs; "
        "use --next-topic to pull the highest-scoring competitor outlier"
    )
    actions.append(
        "Run the thumbnail style matrix on your next render (user templates win "
        "A/Bs first) and log which style you shipped — the leaderboard learns your niche"
    )
    actions.append(
        "At 4+ uploads: build the 8-minute template (mid-roll ads, deeper chapters) "
        "— documentary RPM lives in the 8–12 min band"
    )
    actions.append(
        "Daily Gemini quota is 1 video/day on free tier — upgrade when cadence "
        "demands more (paid tier ≈ cents per video)"
    )
    return actions


# ──────────────────────────────────────────────
# Rich CLI rendering (click.echo stays testable)
# ──────────────────────────────────────────────
def render_audit_report(a: dict) -> str:
    lines: list[str] = []
    ch = a.get("channel")
    lines.append("CHANNEL AUDIT")
    lines.append("=" * 60)
    if ch:
        lines.append(f"Channel   : {ch['title']}  ({ch.get('handle') or 'no handle'})")
        lines.append(f"Subs      : {ch['subscribers']:,}")
        lines.append(f"Videos    : {ch['video_count']}")
        lines.append(f"Views     : {ch['total_views']:,}")
    else:
        lines.append("Channel   : (not resolvable — OAuth/upload missing)")
    lines.append(f"Niche     : {a['profile'].get('niche')}")
    lines.append("")
    lines.append("HEALTH CHECKS")
    for row in a["audit_rows"]:
        icon = {"ok": "[OK]", "warn": "[!!]", "action": "[→]", "info": "[i]"}[row["status"]]
        lines.append(f"  {icon} {row['check']:<18} {row['detail']}")
    lines.append("")
    lines.append("WHAT TO DO NEXT")
    for i, action in enumerate(recommendations(a), 1):
        lines.append(f"  {i}. {action}")
    return "\n".join(lines)
