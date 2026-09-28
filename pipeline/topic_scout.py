"""
GhostDirector — Topic Scout (plan item A4)

Competitor watch-list → outlier scoring → db/topic_queue.json. The
production-aware answer to VidNinjas' "competitor tracking": we don't just
track what competitors upload — we score which of their topics overperformed
and queue those topics for OUR production engine (which renders them; their
tool stops at the idea).

Per-competitor network calls (yt-dlp flat playlist extraction) are isolated
in _fetch_channel_uploads(); every other function — outlier math, title
cleaning, queue persistence, backfill expansion — is deterministic and
unit-tested offline with fixtures.

Usage:
    python main.py --scout                      # score watch-list, refresh queue
    python main.py --scout --add-competitor @Handle
    python main.py --next-topic                 # pop the best queued topic
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

log = get_logger("topic_scout")

QUEUE_PATH = config.DB_DIR / "topic_queue.json"
WATCHLIST_PATH = config.DB_DIR / "competitors.json"

# Outlier threshold: a video is an outlier when views >= OUTLIER_FACTOR ×
# the channel's median. 4× is the industry's "1of10" style bar for
# "this packaging/topic overperformed, study it".
OUTLIER_FACTOR = 4.0
MAX_PER_COMPETITOR = 30       # recent uploads inspected per channel
QUEUE_CAP = 200               # queue never grows unbounded
DEFAULT_WATCHLIST = [
    "https://www.youtube.com/@MagnatesMedia",
    "https://www.youtube.com/@Coffeezilla",
]


# ──────────────────────────────────────────────
# Watchlist
# ──────────────────────────────────────────────
def load_watchlist(path: Path = WATCHLIST_PATH) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("channels", [])
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"watchlist unreadable ({e}); using defaults")
    return list(DEFAULT_WATCHLIST)


def save_watchlist(channels: list[str], path: Path = WATCHLIST_PATH) -> None:
    path.write_text(json.dumps(channels, indent=1), encoding="utf-8")


def add_competitor(handle_or_url: str, path: Path = WATCHLIST_PATH) -> list[str]:
    """Normalize and add a competitor channel; idempotent."""
    channels = load_watchlist(path)
    value = (handle_or_url or "").strip()
    if value and value not in channels:
        channels.append(value)
        save_watchlist(channels, path)
        log.info(f"Competitor added: {value}")
    return channels


def remove_competitor(handle_or_url: str, path: Path = WATCHLIST_PATH) -> list[str]:
    channels = load_watchlist(path)
    value = (handle_or_url or "").strip()
    if value in channels:
        channels.remove(value)
        save_watchlist(channels, path)
        log.info(f"Competitor removed: {value}")
    return channels


# ──────────────────────────────────────────────
# Title → topic derivation (deterministic)
# ──────────────────────────────────────────────
_CLICKBAIT_FRAGMENTS = (
    "the dark truth", "the rise and fall", "how he", "how she", "how they",
    "why ", "the untold", "the real reason", "what really happened",
    "the downfall", "the secret", "exposed", "the biggest", "the man who",
    "the woman who", "the story of", "inside",
)

_NOISE_WORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "to", "for", "with",
    "how", "why", "what", "who", "this", "that", "his", "her", "their",
    "really", "actually", "insane", "crazy", "shocking", "truth", "story",
    "dark", "secret", "real", "untold", "inside", "full", "documentary",
}


def clean_title_to_topic(title: str, max_words: int = 9) -> str:
    """Competitor title → neutral topic prompt for OUR scriptwriter.

    Strips numbering/brackets/emoji, drops pure-clickbait scaffolding, keeps
    proper nouns and the core subject. Deterministic.
    """
    t = (title or "").strip()
    t = re.sub(r"[\[\(].*?[\]\)]", " ", t)            # [4K], (2024)...
    t = re.sub(r"[|#].*$", " ", t)                    # trailing #tag segments
    t = re.sub(r"[^\w\s'\$&-]", " ", t)               # emoji/punct
    t = re.sub(r"\s+", " ", t).strip().lower()
    if not t:
        return ""

    words = t.split()
    # Drop leading clickbait scaffolding ("why ", "the real reason why"...).
    while words and words[0] in {"why", "how", "what", "the"} and len(words) > 3:
        joined = " ".join(words[:3])
        if any(frag in joined + " " for frag in _CLICKBAIT_FRAGMENTS):
            # remove just the fragment words
            frag_len = len(joined.split())
            words = words[frag_len:]
        else:
            break

    # Rank: capitalized-in-original... (we lowercased) → keep content words,
    # prefer earlier position (titles front-load the subject).
    kept: list[str] = []
    for w in words:
        if w in _NOISE_WORDS and kept:
            continue
        kept.append(w)
        if len(kept) >= max_words:
            break
    topic = " ".join(kept).strip(" -")
    return topic[:80]


def _title_case(topic: str) -> str:
    return " ".join(w.capitalize() if not w[:1].isupper() else w
                    for w in topic.split())


# ──────────────────────────────────────────────
# Outlier scoring (pure)
# ──────────────────────────────────────────────
def _median(values: list[float]) -> float:
    vals = sorted(values)
    n = len(vals)
    if not vals:
        return 0.0
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def score_channel_uploads(uploads: list[dict]) -> list[dict]:
    """Score one competitor's uploads by outlier ratio.

    uploads: [{"title": str, "views": int, "url": str, "published": str}, ...]
    Returns scored rows sorted by outlier_ratio desc (outliers only).
    """
    views = [float(u.get("views") or 0) for u in uploads]
    med = _median([v for v in views if v > 0])
    scored = []
    for u in uploads:
        v = float(u.get("views") or 0)
        ratio = (v / med) if med > 0 else 0.0
        if ratio >= OUTLIER_FACTOR and v > 0:
            scored.append({
                "title": u.get("title", ""),
                "url": u.get("url", ""),
                "views": int(v),
                "outlier_ratio": round(ratio, 2),
                "topic": clean_title_to_topic(u.get("title", "")),
                "source_channel": u.get("channel", ""),
                "published": u.get("published", ""),
            })
    scored.sort(key=lambda r: -r["outlier_ratio"])
    return scored


# ──────────────────────────────────────────────
# Queue persistence
# ──────────────────────────────────────────────
def _load_queue(path: Path = QUEUE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("topics"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"topic queue unreadable ({e}); starting fresh")
    return {"topics": [], "produced": []}


def _save_queue(queue: dict, path: Path = QUEUE_PATH) -> None:
    path.write_text(json.dumps(queue, indent=1, ensure_ascii=False), encoding="utf-8")


def merge_into_queue(scored: list[dict], path: Path = QUEUE_PATH) -> dict:
    """Add scored outlier topics; newest score wins on duplicates; cap size."""
    queue = _load_queue(path)
    by_topic = {t.get("topic", "").lower(): t for t in queue["topics"] if t.get("topic")}
    for row in scored:
        topic = row.get("topic")
        if not topic:
            continue
        key = topic.lower()
        if key in by_topic:
            existing = by_topic[key]
            if row["outlier_ratio"] > existing.get("outlier_ratio", 0):
                existing["outlier_ratio"] = row["outlier_ratio"]
                existing["views"] = row["views"]
            existing["seen_count"] = existing.get("seen_count", 1) + 1
        else:
            entry = dict(row)
            entry["queued_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            entry["seen_count"] = 1
            entry["status"] = "queued"
            queue["topics"].append(entry)
            by_topic[key] = entry
    queue["topics"].sort(key=lambda t: -t.get("outlier_ratio", 0))
    queue["topics"] = queue["topics"][:QUEUE_CAP]
    _save_queue(queue, path)
    return queue


def next_topic(pop: bool = True, path: Path = QUEUE_PATH) -> dict | None:
    """The best queued topic (highest outlier ratio, not already produced)."""
    queue = _load_queue(path)
    produced_urls = {p.get("source_url") for p in queue.get("produced", [])}
    produced_topics = {p.get("topic", "").lower() for p in queue.get("produced", [])}
    for entry in queue["topics"]:
        if entry.get("status") == "produced":
            continue
        if entry.get("url") in produced_urls or entry.get("topic", "").lower() in produced_topics:
            continue
        if pop:
            entry["status"] = "produced"
            entry["produced_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            queue["produced"].append({
                "topic": entry["topic"], "source_url": entry.get("url"),
                "title": entry.get("title"),
            })
            _save_queue(queue, path)
        return entry
    return None


# ──────────────────────────────────────────────
# Network layer (isolated; operator-run)
# ──────────────────────────────────────────────
def _fetch_channel_uploads(channel_url: str, max_items: int = MAX_PER_COMPETITOR) -> list[dict]:
    """Recent uploads + view counts for one channel via yt-dlp flat playlist.

    Runs `[sys.executable, "-m", "yt_dlp"]` (Windows-PATH-safe, per blueprint
    fix #5). Raises RuntimeError on failure so run_scout can report per-channel.
    """
    import subprocess

    if not channel_url.startswith("http"):
        channel_url = f"https://www.youtube.com/{channel_url if channel_url.startswith('@') else '@' + channel_url}"

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--flat-playlist", "--print", "%(id)s|%(title)s|%(view_count)s|%(channel)s",
        "--playlist-items", f"1:{max_items}",
        f"{channel_url}/videos",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"yt-dlp timed out for {channel_url}") from e
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed for {channel_url}: {proc.stderr.strip()[:200]}")

    uploads = []
    for line in proc.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        vid, title, views = parts[0], parts[1], parts[2]
        chan = parts[3] if len(parts) > 3 else channel_url
        try:
            view_count = int(views) if views not in ("", "NA", "None") else 0
        except ValueError:
            view_count = 0
        uploads.append({
            "title": title,
            "views": view_count,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "channel": chan,
        })
    return uploads


def run_scout(watchlist: list[str] | None = None,
              path: Path = QUEUE_PATH) -> dict:
    """Score every watch-list channel and merge outliers into the queue.

    Returns a report; per-channel failures are collected, never fatal.
    """
    channels = watchlist if watchlist is not None else load_watchlist()
    report = {"channels_checked": 0, "channels_failed": [], "outliers": 0,
              "queue_size": 0, "per_channel": {}}
    all_scored: list[dict] = []
    for ch in channels:
        report["channels_checked"] += 1
        try:
            uploads = _fetch_channel_uploads(ch)
        except Exception as e:
            report["channels_failed"].append(f"{ch}: {e}")
            log.warning(f"scout skipped {ch}: {e}")
            continue
        scored = score_channel_uploads(uploads)
        for s in scored:
            s["source_channel"] = s.get("source_channel") or ch
        report["per_channel"][ch] = {"uploads": len(uploads), "outliers": len(scored)}
        all_scored.extend(scored)
        report["outliers"] += len(scored)
    queue = merge_into_queue(all_scored, path)
    report["queue_size"] = len(queue["topics"])
    log.info(f"Scout: {report['outliers']} outliers from {report['channels_checked']} "
             f"channels → queue size {report['queue_size']}")
    return report
