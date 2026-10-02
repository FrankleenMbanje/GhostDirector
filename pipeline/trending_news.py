"""
GhostDirector — Trending Celebrity News Discovery (The Fame Files)

The Fame Files is the trending-news lane: fresh celebrity stories, same day,
as shorts. This module finds candidates, scores them, and remembers what we
already covered (db/trending_news.json) so every daily run picks something new.

Sources (no API keys needed):
  - Google News RSS:  "celebrity news" + per-celebrity boost queries
  - DuckDuckGo News:  broad celebrity sweep

Separation from Rise and Ruin is enforced two ways:
  1. lane guard — stories that are rise-and-fall-of-X material are skipped;
  2. the daily ledger — nothing runs twice on either channel (seen/used
     entries and the channel_state packaging ledger are both checked).
"""

import asyncio
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import config
from utils.logger import get_logger

log = get_logger("trending_news")

# Story shapes that belong on Rise and Ruin, not The Fame Files. Matching a
# headline here doesn't guarantee it's a rise-and-fall story, but it's enough
# to keep the Fame Files feed from drifting into the old lane.
_LANE_PATTERNS = [
    r"\brise (and|&|n) fall\b",
    r"\brise and ruin\b",
    r"\bdownfall of\b",
    r"\buntold story\b",
    r"\bdocumentary\b",
    r"\bbiography\b",
    r"\bbankruptc(y|ies)\b",
    r"\bnet worth ranking\b",
    r"\bhow [a-z]+ built (his|her|their) empire\b",
]

# Celebrities worth an extra targeted query (kept short — RSS is a bonus
# signal, DDG News is the broad sweep).
_BOOST_NAMES = [
    "Taylor Swift", "Kanye West", "Diddy", "Blake Lively", "Justin Baldoni",
    "Sabrina Carpenter", "Timothee Chalamet", "Drake", "Kendrick Lamar",
    "MrBeast", "Elon Musk", "Brad Pitt", "Tom Cruise", "Beyonce",
    "Kim Kardashian", "Travis Kelce", "Sydney Sweeney", "Elton John",
]

_TITLE_CLEAN_RE = re.compile(r"\s*-\s+[^-]{3,60}$")   # "Headline - Source" tail
_TAG_RE = re.compile(r"<[^>]+>")                       # RSS strips HTML in titles
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")             # headline specificity signal


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_when(text: str) -> datetime | None:
    """Parse RSS pubDate / DDG date strings into aware datetimes."""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _clean_title(raw: str) -> str:
    t = _TAG_RE.sub("", raw or "").strip()
    t = _TITLE_CLEAN_RE.sub("", t).strip()
    return re.sub(r"\s+", " ", t)


def _slug(text: str, max_len: int = 70) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len].rstrip("-")


# ──────────────────────────────────────────────
# Ledger (db/trending_news.json)
# ──────────────────────────────────────────────
def _load_ledger() -> dict:
    p = config.TRENDING_NEWS_DB
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"trending ledger unreadable ({e}); starting fresh")
    return {"stories": [], "runs": []}


def _save_ledger(data: dict) -> None:
    p = config.TRENDING_NEWS_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _packaging_titles(channel: str | None = None) -> set[str]:
    """Lowercased topics already shipped via the packaging ledger (any channel)."""
    titles: set[str] = set()
    try:
        from utils import channel_state
        channels = [channel] if channel else ["default"] + channel_state.list_channels()
        for ch in channels:
            for entry in channel_state._load(ch).get("packaging_log", []):
                for t in (entry.get("topic"), entry.get("title_used")):
                    if t:
                        titles.add(t.lower())
    except Exception as e:
        log.warning(f"packaging ledger unavailable ({e})")
    return titles


def _story_key(title: str) -> str:
    return _slug(title, 90)


# ──────────────────────────────────────────────
# Source fetchers
# ──────────────────────────────────────────────
async def _google_news_rss(query: str, limit: int = 12) -> list[dict]:
    """Google News RSS → [{title, url, source, published}]"""

    def _sync() -> list[dict]:
        import urllib.request
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            root = ET.fromstring(resp.read())
        out: list[dict] = []
        for item in root.iter("item"):
            title = _clean_title(item.findtext("title") or "")
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            out.append({
                "title": title,
                "url": link,
                "source": (item.findtext("source") or "Google News").strip(),
                "published": (item.findtext("pubDate") or "").strip(),
            })
            if len(out) >= limit:
                break
        return out

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _sync)
    except Exception as e:
        log.warning(f"Google News RSS failed for '{query}': {e}")
        return []


async def _ddg_news(query: str, limit: int = 12) -> list[dict]:
    """DuckDuckGo News → [{title, url, source, published}]"""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        log.warning("duckduckgo_search not installed — DDG news sweep skipped")
        return []

    def _sync() -> list[dict]:
        with DDGS() as ddgs:
            rows = list(ddgs.news(query, max_results=limit, timelimit="w"))
        out: list[dict] = []
        for r in rows:
            title = _clean_title(r.get("title") or "")
            url = (r.get("url") or r.get("href") or "").strip()
            if not title or not url:
                continue
            out.append({
                "title": title,
                "url": url,
                "source": r.get("source") or "DDG",
                "published": str(r.get("date") or r.get("published") or ""),
            })
        return out

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _sync)
    except Exception as e:
        log.warning(f"DDG news failed for '{query}': {e}")
        return []


# ──────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────
def _is_fresh(item: dict, now: datetime | None = None) -> bool:
    """Inside config.TRENDING_MAX_AGE_HOURS (undated items count as fresh —
    DDG timelimit=w and RSS default sort already bias recent)."""
    now = now or _now_utc()
    dt = _parse_when(item.get("published") or "")
    if dt is None:
        return True
    return (now - dt) <= timedelta(hours=config.TRENDING_MAX_AGE_HOURS)


def _in_lane_guard(title: str) -> bool:
    low = title.lower()
    return any(re.search(p, low) for p in _LANE_PATTERNS)


def _score(item: dict, seen_keys: set[str]) -> float | None:
    """Higher = better Fame Files candidate. None = rejected."""
    title = item.get("title") or ""
    if not title or len(title) < 25:
        return None
    if _story_key(title) in seen_keys:
        return None
    if _in_lane_guard(title):
        return None

    low = title.lower()
    score = 0.0

    # FIX-079: lane bonus — in-lane stories rank ahead of everything else
    lane = _lane_hits(title)
    score += min(lane, 2) * 0.8

    # Celebrity signal: a known name or strong celeb nouns in the headline
    for name in _BOOST_NAMES:
        if name.lower() in low:
            score += 2.0
            break
    for kw in ("star", "actor", "singer", "rapper", "pop ", "celebrity",
               "hollywood", "album", "movie", "series", "tour", "show",
               "award", "wedding", "divorce", "arrest", "feud", "viral",
               "announcement", "comeback", "premiere", "grammy", "oscar"):
        if kw in low:
            score += 0.6

    # Urgency / recency
    score += 1.5 if _is_fresh(item) else -2.5

    # Headline quality: specific numbers and quoted speech read as "news"
    if _YEAR_RE.search(title):
        score += 0.4
    if '"' in title or "“" in title:
        score += 0.3
    if low.count("!") > 1 or low.isupper():
        score -= 1.0  # clickbait farm smell

    # Prefer mainstream outlets
    src = (item.get("source") or "").lower()
    for good in ("variety", "hollywood reporter", "deadline", "people",
                 "tmz", "et online", "entertainment tonight", "us weekly",
                 "page six", "billboard", "rolling stone", "e! news",
                 "buzzfeed", "yahoo", "bbc", "cnn", "guardian"):
        if good in src:
            score += 1.2
            break

    # Deduping near-identical headlines from the same sweep (keep the first)
    return score


def _dedupe_candidates(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        k = _story_key(it.get("title") or "")
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────
async def discover_trending(limit: int | None = None) -> list[dict]:
    """Pull + score today's trending celebrity stories.

    Returns the top-N candidates: [{title, url, source, published, score,
    when_hours_ago}] — fresh, lane-clean, not seen in the ledger.
    """
    limit = limit or config.TRENDING_TOP_N
    queries = ["celebrity news", "celebrity breakup OR feud OR arrest",
               "music star news", "movie star news"] + \
              [f"{n} news" for n in _BOOST_NAMES[:8]]

    results: list[dict] = []
    rss_tasks = [_google_news_rss(q) for q in queries]
    ddg_tasks = [_ddg_news(q, 10) for q in queries[:3]]
    for batch in await asyncio.gather(*rss_tasks, *ddg_tasks):
        results.extend(batch)

    results = _dedupe_candidates(results)
    log.info(f"Trending sweep: {len(results)} unique candidates")

    ledger = _load_ledger()
    seen = {_story_key(s.get("title") or "") for s in ledger.get("stories", [])}
    seen |= {_story_key(t) for t in _packaging_titles()}

    now = _now_utc()
    lane_lock = os.environ.get("GD_LANE_LOCK", "1") not in ("0", "false", "no")
    scored: list[dict] = []
    for it in results:
        s = _score(it, seen)
        if s is None:
            continue
        dt = _parse_when(it.get("published") or "")
        scored.append({
            **it,
            "score": round(s, 2),
            "when_hours_ago": round((now - dt).total_seconds() / 3600, 1) if dt else None,
        })

    scored = _lane_gate(scored, lane_lock)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


# FIX-079 lane lock (2026-10-02): the four winners (1.3k-1.6k views) are all
# Taylor Swift / rapper-drama; out-of-lane essays (Pattinson, Holmes) die at
# 10-51 views. Items that hit the lane pass at any score; items that miss it
# need an exceptional headline (score >= 4.0) or they are rejected. Set
# GD_LANE_LOCK=0 to disable (operator override).
_LANE_LOCK_RE = [
    r"taylor swift", r"travis kelce", r"drake", r"kendrick", r"kanye\b|\bye\b",
    r"diddy|combs", r"50 cent", r"cardi b", r"nicki minaj", r"karol g",
    r"feud|beef|diss track|callout|call out", r"lawsuit|sues|sued|lawyers",
    r"arrest|arrested|court|trial|exiled|banned", r"scandal|exposed|drama",
]
_LANE_LOCK_THRESHOLD = 4.0


def _lane_hits(title: str) -> int:
    low = (title or "").lower()
    return sum(1 for p in _LANE_LOCK_RE if re.search(p, low))


def _lane_gate(items: list[dict], enabled: bool) -> list[dict]:
    """FIX-079: drop out-of-lane stories unless the headline is exceptional
    (score >= _LANE_LOCK_THRESHOLD). In-lane items always pass."""
    if not enabled:
        return items
    return [it for it in items
            if it["score"] >= _LANE_LOCK_THRESHOLD
            or _lane_hits(it.get("title") or "") > 0]


def record_story(story: dict, status: str = "seen",
                 channel: str | None = None, video_id: str | None = None,
                 project_dir: str | None = None, fmt: str | None = None) -> None:
    """Upsert a story in the ledger (one entry per key; status updated).
    status: 'seen' | 'produced' | 'used' | 'skipped'.
    fmt: 'short' | 'longform' — lets the FIX-077 bridge find the sibling."""
    ledger = _load_ledger()
    entry = {
        "key": _story_key(story.get("title") or ""),
        "title": story.get("title"),
        "url": story.get("url"),
        "source": story.get("source"),
        "published": story.get("published"),
        "status": status,
        "channel": channel,
        "video_id": video_id,
        "project_dir": project_dir,
        "fmt": fmt,
        "recorded_at": _now_utc().isoformat(),
    }
    stories = ledger.setdefault("stories", [])
    for i, s in enumerate(stories):
        # FIX-077: upsert per (key, fmt) so the short and the doc of one
        # story coexist in the ledger — the bridge needs to find BOTH ids.
        # Key-only dedupe (story freshness) still works: any entry with the
        # key marks the story as seen.
        if s.get("key") == entry["key"] and s.get("fmt") == entry["fmt"]:
            stories[i] = {**s, **entry}   # update in place, keep original recorded_at as first_seen below
            stories[i].setdefault("first_seen", s.get("recorded_at"))
            break
    else:
        entry["first_seen"] = entry["recorded_at"]
        stories.append(entry)
    ledger["stories"] = stories[-400:]  # cap the file size
    _save_ledger(ledger)


def find_companion_video_id(story_title: str, want_fmt: str) -> str | None:
    """FIX-077: the already-shipped sibling video for this story
    (short↔doc bridge). Returns its YouTube video id or None."""
    key = _story_key(story_title or "")
    if not key:
        return None
    for s in _load_ledger().get("stories", []):
        if (s.get("key") == key and s.get("video_id")
                and s.get("fmt") == want_fmt):
            return s["video_id"]
    return None


_QUEUE_PATH = Path("output") / "PUBLISH_QUEUE.md"


def append_publish_queue(story_title: str, fmt: str, video_id: str,
                         packaged_title: str | None = None) -> None:
    """FIX-078/FIX-080: the verify-and-pin list. Videos now upload PUBLIC
    (operator order 2026-10-02), so the old "publish within 6h" race is
    gone — what remains is the same-day pass: check playback, confirm the
    packaging, and pin the bridge comment (the Data API cannot pin). Each
    upload appends a row with a Studio link and a check-by deadline
    (upload + 6h)."""
    try:
        now = _now_utc()
        deadline = now + timedelta(hours=6)
        fmt_label = "8-min doc" if fmt == "longform" else "Short"
        pin_note = ("pin the bridge comment (link the Short)"
                    if fmt == "longform"
                    else "pin the bridge comment once the doc is live (same day)")
        row = (f"| {deadline.strftime('%b %d %H:%M UTC')} (+6h) "
               f"| {fmt_label} | {(packaged_title or story_title or '')[:60]} "
               f"| [Studio](https://studio.youtube.com/video/{video_id}/edit) "
               f"| [watch](https://youtu.be/{video_id}) | {pin_note} |\n")
        _QUEUE_PATH.parent.mkdir(exist_ok=True)
        if not _QUEUE_PATH.exists():
            _QUEUE_PATH.write_text(
                "# PUBLISH QUEUE — verify & pin within 6h of upload\n\n"
                "Videos upload PUBLIC automatically (operator order 2026-10-02). "
                "Check playback + pin the bridge comment; pull anything broken "
                "back to unlisted from Studio.\n\n"
                "| publish-by | format | title | review | watch | pin |\n"
                "|---|---|---|---|---|---|\n",
                encoding="utf-8")
        with open(_QUEUE_PATH, "a", encoding="utf-8") as f:
            f.write(row)
    except Exception:
        pass   # the queue must never break a production run


def record_run(story: dict, project_dir: str | None, video_id: str | None,
               channel: str) -> None:
    """One line per Fame Files production run (operator-facing history)."""
    ledger = _load_ledger()
    ledger.setdefault("runs", []).append({
        "at": _now_utc().isoformat(),
        "title": story.get("title"),
        "channel": channel,
        "project_dir": project_dir,
        "video_id": video_id,
    })
    ledger["runs"] = ledger["runs"][-100:]
    _save_ledger(ledger)


def already_covered(title: str) -> bool:
    """True if this story was ever recorded in the ledger."""
    ledger = _load_ledger()
    k = _story_key(title)
    return any(s.get("key") == k for s in ledger.get("stories", []))
