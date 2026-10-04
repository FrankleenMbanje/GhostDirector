"""
GhostDirector — Channel Memory (FIX-083)

The learning layer the channel never had: EVERY upload — including the
operator's MANUAL videos (the 2Pac doc and friends) — becomes a labeled
example: packaging features (format, title pattern, lane hits, origin)
joined to outcomes (views, likes, comments, engagement). Persisted to
db/channel_memory.json.

Why this is separate from pipeline/analytics.py: analytics.py syncs
retention for RECENT videos and joins it to PIPELINE packaging logs. The
manual uploads never enter that loop, and nothing fed observed outcomes
back into production decisions. This module ingests the whole catalogue.

Honesty rails (small channel, uneven exposure):
  * The report separates FACTS (medians per format, n stated) from HINTS
    (title-pattern buckets). With <50 videos — and shorts drowning
    long-form in exposure — most cross-format comparisons are noise.
  * style_priors (operator taste: "make docs like the 2Pac video") and
    performance_priors (audience outcome) stay SEPARATE. Taste is an
    instruction; outcomes are statistics, and statistics need volume
    before they may steer production.
"""

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

import config
from utils.logger import get_logger
from pipeline.uploader import _iso8601_seconds, get_authenticated_service

log = get_logger("channel_memory")

MEMORY_PATH = Path("db") / "channel_memory.json"
SHORT_MAX_S = 65.0
MAX_VIDEOS = 200


# ──────────────────────────────────────────────
# Pure helpers (unit-tested)
# ──────────────────────────────────────────────
def fmt_from_seconds(seconds: float | None) -> str:
    if not seconds:
        return "unknown"
    return "short" if seconds <= SHORT_MAX_S else "long"


def origin_of(title: str, description: str) -> str:
    """automated = shipped by this pipeline. The MacLeod CC-BY credit line is
    appended to every automated upload (FIX-055); the long-form's 9:16
    companion also carries a ' #shorts' suffix. Everything else is a manual
    operator upload — exactly the training examples that were invisible."""
    if "incompetech.com" in (description or ""):
        return "automated"
    if (title or "").rstrip().endswith("#shorts"):
        return "automated"
    return "manual"


def title_features(title: str) -> dict:
    words = [w for w in (title or "").replace("—", " ").split() if w]
    caps = [w for w in words if len(w) >= 2 and w.isupper()]
    return {
        "words": len(words),
        "chars": len(title or ""),
        "has_question": "?" in (title or ""),
        "has_bang": "!" in (title or ""),
        "has_number": any(c.isdigit() for c in (title or "")),
        "has_colon": ":" in (title or ""),
        "caps_words": len(caps),
    }


def _lane_hits(title: str) -> int:
    try:
        from pipeline.trending_news import _lane_hits as _hits
        return _hits(title or "")
    except Exception:
        return 0


def _median(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(vals), 1) if vals else None


def aggregate(rows: list[dict]) -> dict:
    """Pure: rows -> the report structure (unit-testable without network)."""
    sh = [r for r in rows if r["format"] == "short"]
    lg = [r for r in rows if r["format"] == "long"]

    def _bucket(group, key):
        out: dict[str, list] = {}
        for r in group:
            out.setdefault(str(r["features"].get(key)), []).append(r["views"])
        return {k: {"n": len(v), "median_views": _median(v)}
                for k, v in sorted(out.items())}

    lane_buckets: dict[str, list] = {}
    for r in sh:
        lane_buckets.setdefault(str(min(r.get("lane_hits", 0), 2)), []).append(r["views"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_videos": len(rows),
        "n_automated": sum(1 for r in rows if r["origin"] == "automated"),
        "n_manual": sum(1 for r in rows if r["origin"] == "manual"),
        "shorts": {
            "n": len(sh),
            "median_views": _median([r["views"] for r in sh]),
            "median_engagement": _median([r["engagement"] for r in sh]),
        },
        "longs": {
            "n": len(lg),
            "median_views": _median([r["views"] for r in lg]),
            "median_engagement": _median([r["engagement"] for r in lg]),
        },
        "title_buckets_shorts": {
            k: _bucket(sh, k) for k in
            ("has_question", "has_number", "has_colon", "caps_words")
        },
        "lane_buckets_shorts": {
            k: {"n": len(v), "median_views": _median(v)}
            for k, v in sorted(lane_buckets.items())
        },
        "rows": rows,
    }


# ──────────────────────────────────────────────
# Ingest
# ──────────────────────────────────────────────
def fetch_rows(channel: str | None = None) -> list[dict]:
    """Every upload on the channel with features + outcomes (paginated)."""
    youtube = get_authenticated_service(channel=channel)
    ch = youtube.channels().list(part="contentDetails", mine=True).execute()
    up = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    ids: list[str] = []
    token = None
    while len(ids) < MAX_VIDEOS:
        resp = youtube.playlistItems().list(
            part="contentDetails", playlistId=up, maxResults=50,
            pageToken=token).execute()
        ids += [i["contentDetails"]["videoId"] for i in resp.get("items", [])]
        token = resp.get("nextPageToken")
        if not token:
            break

    rows: list[dict] = []
    for i in range(0, len(ids), 50):
        vids = youtube.videos().list(
            part="snippet,status,statistics,contentDetails",
            id=",".join(ids[i:i + 50])).execute()
        for v in vids.get("items", []):
            sn = v["snippet"]
            st = v.get("status", {})
            stats = v.get("statistics", {})
            secs = _iso8601_seconds(v.get("contentDetails", {}).get("duration", "")) or 0.0
            views = int(stats.get("viewCount", 0) or 0)
            likes = int(stats.get("likeCount", 0) or 0)
            comments = int(stats.get("commentCount", 0) or 0)
            title = sn.get("title", "")
            rows.append({
                "id": v["id"],
                "title": title,
                "published": sn.get("publishedAt"),
                "format": fmt_from_seconds(secs),
                "seconds": round(secs, 1),
                "origin": origin_of(title, sn.get("description", "")),
                "privacy": st.get("privacyStatus"),
                "views": views,
                "likes": likes,
                "comments": comments,
                "engagement": round((likes + comments) / views, 4) if views else 0.0,
                "lane_hits": _lane_hits(title),
                "features": title_features(title),
            })
    return rows


def delivery_audit(rows: list[dict], hours: float = 72,
                   now: datetime | None = None) -> list[str]:
    """FIX-089: which recent AUTOMATED uploads are not PUBLIC right now?

    The 2026-10-02 Strictly doc passed its post-upload guard
    ("privacy=public confirmed") and read `unlisted` the next morning —
    either a platform demotion or a Studio flip; the log alone could not
    tell. The catalogue refresh captures live privacyStatus, so this audit
    reports drift the upload-time guard cannot see. Manual uploads are the
    operator's own — never flagged. Nothing is auto-flipped: re-publishing
    a small rubbish doc may be exactly what the operator did NOT want.
    """
    ref = now or datetime.now(timezone.utc)
    flagged: list[str] = []
    checked = 0
    for r in rows or []:
        try:
            if (r.get("origin") or "") != "automated":
                continue
            dt = datetime.fromisoformat(
                (r.get("published") or "").replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_h = (ref - dt).total_seconds() / 3600
            if age_h < 0 or age_h > hours:
                continue
            checked += 1
            priv = (r.get("privacy") or "unknown").lower()
            if priv != "public":
                flagged.append(
                    f"  NOT PUBLIC — {r.get('id')} [{priv}] "
                    f"{r.get('published')} — {r.get('title')}")
        except Exception:
            continue
    if flagged:
        return ([f"DELIVERY AUDIT — {len(flagged)}/{checked} automated "
                 f"uploads from the last {int(hours)}h are not public:"]
                + flagged
                + ["  → republish from Studio if unintended; the pipeline "
                   "re-asserts PUBLIC only during its own upload window."])
    return [f"DELIVERY AUDIT — all {checked} automated uploads from the "
            f"last {int(hours)}h are public. OK"]


def fetch_channel_stats(channel: str | None = None) -> dict:
    """Subscriber + lifetime-view counts — the monetization gap's numerator."""
    try:
        youtube = get_authenticated_service(channel=channel)
        ch = youtube.channels().list(part="statistics", mine=True).execute()
        stats = ch["items"][0].get("statistics", {})
        return {
            "subscribers": int(stats.get("subscriberCount", 0) or 0),
            "total_views": int(stats.get("viewCount", 0) or 0),
            "video_count": int(stats.get("videoCount", 0) or 0),
        }
    except Exception as e:
        log.warning(f"Channel stats unavailable: {e}")
        return {"subscribers": None, "total_views": None, "video_count": None}


def build_memory(channel: str | None = None, save: bool = True) -> dict:
    """Ingest the full catalogue, aggregate, persist db/channel_memory.json."""
    channel = (channel if channel in (config.CHANNEL_FAMEFILES,
                                      config.CHANNEL_RISEANDRUIN)
               else config.CHANNEL_FAMEFILES)
    rows = fetch_rows(channel)
    memory = aggregate(rows)
    memory["channel"] = channel
    memory["channel_stats"] = fetch_channel_stats(channel)
    if save:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")
    return memory


def render_report(memory: dict) -> str:
    stats = memory.get("channel_stats") or {}
    lines = [f"Channel memory — {memory.get('channel')} @ {memory.get('generated_at')}",
             f"channel: {stats.get('subscribers')} subs | "
             f"{stats.get('total_views')} lifetime views | "
             f"{stats.get('video_count')} videos",
             f"videos: {memory['n_videos']} "
             f"(automated {memory['n_automated']}, manual {memory['n_manual']})"]
    for key, label in (("shorts", "shorts"), ("longs", "long-form docs")):
        b = memory[key]
        lines.append(f"  {label}: n={b['n']} | median views {b['median_views']} "
                     f"| median engagement {b['median_engagement']}")
    rows = memory.get("rows", [])
    lines.append("top by views:")
    for r in sorted(rows, key=lambda r: r["views"], reverse=True)[:5]:
        lines.append(f"  {r['views']:>7} {r['format']:<5} {r['origin']:<9} "
                     f"{(r.get('title') or '')[:58]}")
    manual = [r for r in rows if r["origin"] == "manual"]
    if manual:
        lines.append("manual uploads (operator taste + outcome):")
        for r in sorted(manual, key=lambda r: r["views"], reverse=True)[:8]:
            lines.append(f"  {r['views']:>7} {r['format']:<5} "
                         f"{float(r.get('seconds') or 0):>6.0f}s "
                         f"{(r.get('title') or '')[:58]}")
    lines.append("shorts title buckets (median views):")
    for k, buckets in memory.get("title_buckets_shorts", {}).items():
        parts = [f"{bk}: n={bv['n']} med={bv['median_views']}"
                 for bk, bv in buckets.items()]
        lines.append(f"  {k}: " + "; ".join(parts))
    lines.append("shorts lane buckets (median views):")
    for k, bv in memory.get("lane_buckets_shorts", {}).items():
        lines.append(f"  lane-hits<={k}: n={bv['n']} med={bv['median_views']}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(render_report(build_memory()))
