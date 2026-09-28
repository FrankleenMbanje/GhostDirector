"""
GhostDirector — Metadata Generator

Generates YouTube-ready title, description, tags, and chapter markers.
Includes AI content disclosure for YouTube compliance.
"""

import sys
import re
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Script
import config
from utils.logger import get_logger

log = get_logger("metadata")


def _format_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS or H:MM:SS for YouTube chapters."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# Words that never carry chapter-worthy meaning on their own.
_CHAPTER_STOPWORDS = {
    "the", "a", "an", "and", "but", "or", "of", "in", "on", "at", "to",
    "for", "with", "that", "this", "these", "those", "he", "she", "it",
    "they", "was", "were", "is", "are", "his", "her", "their", "its",
    "from", "by", "as", "so", "then", "than", "had", "has", "have",
    "what", "when", "where", "why", "how", "not", "no", "yes", "you",
    "your", "we", "our", "us", "i", "me", "my",
}


def _chapter_title_from_narration(narration: str, fallback: str) -> str:
    """Build a ~6-word chapter title from the scene's narration topic (A27).

    Chapters are public text — the old source, scene.visual_prompt, is an
    internal IMAGE SEARCH QUERY ("Aerial Shot Of Manhattan At Night…") and
    leaked that straight into the description. The narration sentence tells
    viewers what they're about to hear; key capitalized names survive.
    """
    text = (narration or "").strip()
    if not text:
        return fallback

    first_sentence = re.split(r"(?<=[.!?])\s+", text)[0]
    words = re.findall(r"[\w'\$]+", first_sentence)
    if not words:
        return fallback

    # Score: capitalized words (names, places) first, then content words.
    def _score(w: str, idx: int) -> tuple:
        lower = w.lower()
        stop = lower in _CHAPTER_STOPWORDS
        cap = w[0].isupper() and idx > 0
        return (cap and not stop, not stop, -idx)

    ranked = sorted(range(len(words)), key=lambda i: _score(words[i], i), reverse=True)
    keep = sorted(ranked[:6])
    title = " ".join(words[i] for i in keep)
    return (title[:60].rstrip(" ,.;:") + "…") if len(title) > 60 else title


def generate_metadata(
    script: Script,
    template: dict,
    output_dir: Path,
) -> dict:
    """
    Generate complete YouTube metadata from the script.

    Returns a dict with: title, description, tags, category, chapters.
    Also saves to metadata.json.
    """
    log.info("[bold blue]Generating metadata[/bold blue]")

    # ── Chapters (YouTube strictly requires 00:00 as the first timestamp) ──
    # FIX-004: prefer the assembler's transition-aware timeline.json when it
    # exists — the old cumulative math drifts 0.4s per xfade cut.
    scene_starts: list[float] | None = None
    timeline_path = output_dir / "timeline.json"
    if timeline_path.exists():
        try:
            tl = json.loads(timeline_path.read_text(encoding="utf-8"))
            scene_starts = [sc["start_seconds"] for sc in tl.get("scenes", [])]
        except Exception as e:
            log.warning(f"timeline.json unreadable ({e}); falling back to estimates")

    chapters = [{"timestamp": "00:00", "title": "The Secret / Hook"}]
    cumulative = 3.0  # Legacy fallback estimate (accounts for intro hook)

    for i, scene in enumerate(script.scenes):
        duration = scene.audio_duration_seconds or scene.duration_target_seconds or 5.0
        # Create a chapter every ~3-4 scenes
        if i > 0 and (i % 3 == 0):
            start = (
                scene_starts[i]
                if scene_starts and i < len(scene_starts)
                else cumulative
            )
            # A27: title from what the narration SAYS, not the image search
            # query. Keep the scene number as a last-resort label.
            clean_title = _chapter_title_from_narration(
                scene.narration, fallback=f"Part {len(chapters) + 1}"
            )
            chapters.append({
                "timestamp": _format_timestamp(max(start, 0.1)),
                "title": clean_title,
            })
        cumulative += duration

    # ── High-Engagement Pinned Comment ──
    pinned = getattr(script, "pinned_comment", None)
    if not pinned:
        first_chap = chapters[1]["timestamp"] if len(chapters) > 1 else "00:15"
        pinned = f"Watch what happens at {first_chap} — Did you know this? Let me know in the comments below! 👇"

    # ── Title Variants (A/B testing for CTR from script or generated) ──
    raw_variants = getattr(script, "title_variants", None) or []
    if raw_variants and isinstance(raw_variants, list) and isinstance(raw_variants[0], dict):
        title_variants = [v.get("title", "") for v in raw_variants if v.get("title")]
    else:
        base_title = script.title
        clean_name = base_title.replace("The Rise of ", "").replace("The Story of ", "")
        title_variants = [
            f"Why Nobody Talks About {clean_name}"[:70],
            f"How {clean_name} Changed Everything In Secret"[:70],
            f"The Untold Truth Behind {clean_name}"[:70],
        ]
    if script.title not in title_variants:
        title_variants.insert(0, script.title)

    # ── Mid-Roll Ad Placement Timestamps (for 8+ min monetization) ──
    midroll_markers = getattr(script, "midroll_markers", None) or []
    if not midroll_markers and cumulative >= 480:  # 8 minutes
        # Optimal midroll placement at natural chapter transitions around 3:30 and 7:00
        for ch in chapters:
            ts = ch["timestamp"]
            parts = ts.split(":")
            sec = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else 0
            if (190 <= sec <= 230 or 400 <= sec <= 440) and ts not in midroll_markers:
                midroll_markers.append(ts)

    midroll_text = (
        f"Optimal Mid-Roll Ad Placements: {', '.join(midroll_markers)}"
        if midroll_markers else ""
    )  # Operator-facing only — NEVER render into the public description.

    # ── Description ──
    chapter_text = "\n".join(
        f"{ch['timestamp']} {ch['title']}" for ch in chapters
    )

    script_tags = list(script.tags or [])

    # ── Source credit ledger (FIX-008) ──
    # Every scraped photo/clip recorded provenance on the Scene. Rendering it
    # here supports the fair-use good-faith posture (commentary + credit) and
    # gives evidence for Content ID disputes. Never fabricate entries.
    credits: list[str] = []
    seen_sources: set[str] = set()
    for scene in script.scenes:
        url = (getattr(scene, "source_url", None) or "").strip()
        if not url or url in seen_sources:
            continue
        seen_sources.add(url)
        title = (getattr(scene, "source_title", None) or "").strip()
        channel = (getattr(scene, "source_channel", None) or "").strip()
        label = title if title else url
        if channel and channel.lower() not in label.lower():
            label = f"{label} — {channel}"
        # FIX-048: when no source title exists the label IS the url — printing
        # it again on the next line doubled every credit (~1.9k chars of the
        # 6.6k-char description that blew the 5000-char YouTube limit).
        credits.append(f"• {url}" if label == url else f"• {label}\n  {url}")

    credits_text = ""
    if credits:
        credits_text = (
            "\n🎬 FOOTAGE & IMAGE CREDITS\n"
            "Short excerpts of third-party material are used for commentary and\n"
            "criticism under fair use. Sources:\n"
            + "\n".join(credits[:20])
            + ("\n" if len(credits) > 20 else "")
        )
        if len(credits) > 20:
            credits_text += f"(+{len(credits) - 20} more)\n"

    # FIX-048: plain section labels instead of ━ divider bars — the 26-char
    # heavy-line runs tripped the repeated-character spam check on every video.
    description = f"""
{script.description}

⏱️ CHAPTERS
{chapter_text}

💬 PINNED DISCUSSION
{pinned}
{credits_text}
ℹ️ ABOUT
Content is for educational and entertainment purposes only.
Events involving legal allegations are presented as reported by
public sources; nothing here is a statement of guilt.

Stock footage provided by Pexels (pexels.com).
Background music is royalty-free.
"""

    tags = script_tags[:30]  # YouTube allows up to 30 tags
    tag_string = ",".join(tags)
    while len(tag_string) > 500 and tags:
        tags.pop()
        tag_string = ",".join(tags)

    # ── Build metadata ──
    metadata = {
        "title": title_variants[0] if title_variants else script.title,
        "title_variants": title_variants,
        "description": description.strip(),
        "tags": tags,
        "category": "24",  # 24 = Entertainment (celebrity-news lane; operator call 2026-09-26)
        "privacy_status": "unlisted",
        "made_for_kids": False,
        "language": "en",
        "ai_generated": True,
        "chapters": chapters,
        "pinned_comment": pinned,
        "midroll_markers": midroll_markers,
        "midroll_ad_cues": midroll_text,  # operator-facing; not rendered publicly
        "hook_overlay_text": getattr(script, "hook_overlay_text", None),
        # Phase 3.11 operator checklist — Studio tasks that have no API field
        # (synthetic-media disclosure is a manual toggle; mid-rolls are set
        # per video in Studio). The uploader surfaces this as a reminder.
        "checklist": {
            "synthetic_media_disclosed": False,
            "thumbnail_variant_used": "thumbnail.png",
            "midroll_ad_cues_set": bool(midroll_markers),
            "pinned_comment_posted": False,
        },
    }

    # NOTE (plan A5): autocomplete SEO enrichment happens in main.run_pipeline
    # AFTER this function — only there is the operator's original topic
    # (search-shaped, e.g. "The Rise and Fall of Elizabeth Holmes") available;
    # script.title is creative copy ("How Silicon Valley's Golden Girl Lost
    # Everything") and returns zero autocomplete suggestions.

    # ── Save ──
    metadata_path = output_dir / "metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info(f"[bold green]Metadata saved:[/bold green] {metadata_path.name}")
    log.info(f"  Title: {metadata['title']}")
    log.info(f"  Tags: {len(tags)} tags")
    log.info(f"  Chapters: {len(chapters)} markers")
    log.info(f"  Pinned Comment: {pinned[:50]}...")

    return metadata


# ──────────────────────────────────────────────
if __name__ == "__main__":
    print("Metadata module loaded. Run via main.py.")
