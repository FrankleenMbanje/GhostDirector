"""
GhostDirector — Keyword/SEO Layer (plan item A5)

YouTube autocomplete scrape → clustered long-tails → tag/description SEO.
Parity with VidNinjas' "descriptions & SEO" tools, wired directly into the
metadata generator so every render ships with suggestion-backed keywords —
zero API quota (suggestqueries is the public endpoint browsers use).

Deterministic functions (clustering, tag selection, description placement)
are unit-tested offline; only `suggest_keywords()` touches the network.

Usage (standalone):
    python -m pipeline.keywords "the rise and fall of elizabeth holmes"
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import get_logger

log = get_logger("keywords")

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
MAX_TAGS = 30              # YouTube tag limit is ~500 chars / ~30 tags
DESCRIPTION_KEYWORD_LIMIT = 8


# ──────────────────────────────────────────────
# Fetching
# ──────────────────────────────────────────────
def _fetch_suggestions(seed: str, timeout: float = 8.0) -> list[str]:
    """One autocomplete query → list of suggestion strings (JSONP, not JSON)."""
    import httpx

    params = {
        "client": "firefox",           # returns clean JSON array
        "hl": "en", "gl": "us",
        "ds": "yt",                    # YouTube data source
        "q": seed,
    }
    url = f"{SUGGEST_URL}?{urllib.parse.urlencode(params)}"
    resp = httpx.get(url, timeout=timeout, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    })
    resp.raise_for_status()
    data = resp.json()
    # client=firefox shape: [query, [s1, s2, ...]]
    if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
        return [str(s) for s in data[1]]
    return []


def suggest_keywords(topic: str) -> list[str]:
    """Topic → suggestion set: the topic itself plus letter expansions.

    Deterministic expansion order; network errors degrade to empty (the
    metadata generator falls back to script tags).
    """
    base = (topic or "").strip()
    if not base:
        return []
    seeds = [base]
    # Expand: append letters a/b/h/w to harvest different long-tail clusters
    # (standard YouTube keyword-research pattern; cheap and effective).
    for suffix in ("a", "h", "w"):
        seeds.append(f"{base} {suffix}")
    results: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        try:
            for s in _fetch_suggestions(seed):
                norm = s.strip().lower()
                if norm and norm not in seen:
                    seen.add(norm)
                    results.append(s.strip())
        except Exception as e:
            log.warning(f"suggest failed for '{seed}': {e}")
    return results


# ──────────────────────────────────────────────
# Clustering & selection (pure, offline-tested)
# ──────────────────────────────────────────────
def cluster_suggestions(suggestions: list[str], topic: str) -> dict[str, list[str]]:
    """Group suggestions into clusters by their relationship to the topic.

    Clusters:
      core     — contains the full topic (or most of its content words)
      person   — contains a capitalized proper noun not in the topic
      question — how/why/what/who... (question-intent long-tails)
      related  — everything else
    Returns {"core": [...], "person": [...], "question": [...], "related": [...]}.
    """
    topic_lower = (topic or "").lower()
    topic_words = {w for w in re.findall(r"[a-z']+", topic_lower) if len(w) > 2}
    clusters: dict[str, list[str]] = {"core": [], "person": [], "question": [], "related": []}

    for s in suggestions:
        sl = s.lower()
        # Proper noun = word starting uppercase, not sentence-start, not in topic.
        words = s.split()
        proper = [w for i, w in enumerate(words)
                  if i > 0 and w[:1].isupper() and w.lower() not in topic_words]
        if topic_words and topic_words.issubset(set(re.findall(r"[a-z']+", sl))):
            clusters["core"].append(s)
        elif re.match(r"^(how|why|what|who|when|where|is|does|did)\b", sl):
            clusters["question"].append(s)
        elif proper:
            clusters["person"].append(s)
        else:
            clusters["related"].append(s)
    return clusters


def select_tags(suggestions: list[str], topic: str, base_tags: list[str],
                max_tags: int = MAX_TAGS) -> list[str]:
    """Build the final tag list: script tags first, then suggestion long-tails.

    Deduped case-insensitively; keeps YouTube's ~500-char budget in mind by
    stopping when adding the next tag would blow past it.
    """
    char_budget = 500
    used_chars = sum(len(t) + 1 for t in base_tags)
    tags: list[str] = list(base_tags)
    seen = {t.lower() for t in base_tags}

    def _fits(tag: str) -> bool:
        return used_chars + len(tag) + 1 <= char_budget

    for s in suggestions:
        norm = s.strip().lower()
        if norm in seen or len(s) > 70:      # very long tails are junk tags
            continue
        if len(tags) >= max_tags or not _fits(s):
            break
        tags.append(s)
        seen.add(norm)
    return tags


def keyword_footer(suggestions: list[str], topic: str) -> str | None:
    """A natural-sounding 'Explore more about…' description block.

    Only for clusters with real substance; never keyword-stuffed links.
    Returns None when there is nothing worth surfacing.
    """
    clusters = cluster_suggestions(suggestions, topic)
    picks = clusters["core"][:3] or clusters["question"][:3]
    if not picks:
        return None
    items = "\n".join(f"• {p}" for p in picks[:DESCRIPTION_KEYWORD_LIMIT // 2])
    return f"\n\nExplore more about {topic}:\n{items}"


# ──────────────────────────────────────────────
# Metadata hook
# ──────────────────────────────────────────────
def enrich_metadata(metadata: dict, topic: str,
                    fetch: bool = True) -> dict:
    """In-place enrichment of a generated metadata dict (plan A5).

    Fetches autocomplete suggestions for the topic, merges them into `tags`
    and appends a keyword footer to `description`. Network-safe: any failure
    leaves the metadata untouched (logged only).
    """
    if not topic:
        return metadata
    suggestions: list[str] = []
    if fetch:
        try:
            suggestions = suggest_keywords(topic)
        except Exception as e:
            log.warning(f"keyword fetch failed: {e}")
    if not suggestions:
        return metadata

    metadata["tags"] = select_tags(suggestions, topic, metadata.get("tags", []))
    footer = keyword_footer(suggestions, topic)
    if footer and footer.strip() not in (metadata.get("description") or ""):
        metadata["description"] = (metadata.get("description") or "") + footer
    metadata["keywords_from_suggestions"] = len(suggestions)
    log.info(f"SEO: +{len(metadata['tags'])} tags, footer added ({len(suggestions)} suggestions)")
    return metadata


if __name__ == "__main__":
    topic = " ".join(sys.argv[1:]) or "the rise and fall of elizabeth holmes"
    print(json.dumps(cluster_suggestions(suggest_keywords(topic), topic),
                     indent=2, ensure_ascii=False))
