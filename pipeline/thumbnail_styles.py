"""
GhostDirector — Thumbnail Style Library (plan item A9a)

A curated library of 10 thumbnail styles modeled on the layouts that dominate
documentary / true-crime / business channels — plus a loader for USER templates
(assets/thumbnail_templates/*.json), because the operator knows their niche's
winning looks best. Every style is deterministic: same inputs → same PNG, so
A/B results teach us which STYLE wins, not which dice-roll won.

Style entries are specs consumed by pipeline/thumbnail.py's renderer:
    {"id", "name", "text_layout", "accent", "bg_treatment", "palette",
     "pair_with" (title archetype), "best_for" (niches), "source"}

Curated sources (public knowledge, distilled from tool roundups and
top-channel conventions):
  1. focal-yellow   — subject left/right + 1 yellow word (MrBeast school)
  2. split-boxes    — black pills w/ white text (MagnatesMedia / Newsthink)
  3. red-circle     — subject + bright circle/arrow callout (reaction niche)
  4. before-after   — split-screen then/now (fall-from-grace docs)
  5. minimal-dark   — near-black, small glowing bar (ColdFusion school)
  6. money-green    — cash-green accent on dark bg (finance exposés)
  7. price-tag      — giant $ number as hero (business collapse)
  8. vs-duel        — A vs B confrontation frame (rivalries)
  9. evidence-board — document/arrow overlay on photo (investigation)
 10. emoji-burst    — one emoji-style icon + short 2-word text (younger demos)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from utils.logger import get_logger

log = get_logger("thumbnail_styles")

USER_TEMPLATE_DIR = config.ASSETS_DIR / "thumbnail_templates"

# ── The curated library (rendered by thumbnail.py concept renderers) ──
STYLES: list[dict] = [
    {
        "id": "focal-yellow", "name": "Focal + Yellow Word",
        "text_layout": "left_stack", "accent": "yellow", "bg_treatment": "gradient",
        "palette": {"accent": "#FFE600", "text": "#FFFFFF"},
        "pair_with": "curiosity_gap", "best_for": ["documentary", "true_crime", "biography"],
        "source": "curated",
    },
    {
        "id": "split-boxes", "name": "Black Pill Boxes",
        "text_layout": "pill_stack", "accent": "red", "bg_treatment": "tint_left",
        "palette": {"box": "#E60028", "text": "#FFFFFF"},
        "pair_with": "direct_benefit", "best_for": ["documentary", "education"],
        "source": "curated",
    },
    {
        "id": "red-circle", "name": "Red Circle Callout",
        "text_layout": "corner_short", "accent": "red", "bg_treatment": "none",
        "palette": {"accent": "#FF1D25", "text": "#FFFFFF"},
        "pair_with": "mystery_object", "best_for": ["true_crime", "reaction"],
        "source": "curated",
    },
    {
        "id": "before-after", "name": "Then / Now Split",
        "text_layout": "split_labels", "accent": "white", "bg_treatment": "split",
        "palette": {"left_label": "THEN", "right_label": "NOW"},
        "pair_with": "rise_and_fall", "best_for": ["documentary", "biography", "celebrity"],
        "source": "curated",
    },
    {
        "id": "minimal-dark", "name": "Minimal Dark Statement",
        "text_layout": "left_stack", "accent": "glow_bar", "bg_treatment": "heavy_vignette",
        "palette": {"accent": "#FFE600", "text": "#FFFFFF"},
        "pair_with": "contrarian", "best_for": ["business", "technology"],
        "source": "curated",
    },
    {
        "id": "money-green", "name": "Money Green Exposé",
        "text_layout": "pill_stack", "accent": "green", "bg_treatment": "tint_left",
        "palette": {"box": "#0BA84A", "text": "#FFFFFF"},
        "pair_with": "direct_benefit", "best_for": ["finance", "business", "scam"],
        "source": "curated",
    },
    {
        "id": "price-tag", "name": "Giant $ Number",
        "text_layout": "hero_number", "accent": "green", "bg_treatment": "gradient",
        "palette": {"accent": "#7CFC00", "text": "#FFFFFF"},
        "pair_with": "number_hook", "best_for": ["finance", "business"],
        "source": "curated",
    },
    {
        "id": "vs-duel", "name": "Versus Duel Frame",
        "text_layout": "vs_center", "accent": "vs_badge", "bg_treatment": "split",
        "palette": {"badge": "#FF3B30"},
        "pair_with": "rivalry", "best_for": ["sports", "business", "celebrity"],
        "source": "curated",
    },
    {
        "id": "evidence-board", "name": "Evidence Board",
        "text_layout": "stamp", "accent": "manila", "bg_treatment": "desaturate",
        "palette": {"stamp": "#C8A24B", "text": "#FFFFFF"},
        "pair_with": "investigation", "best_for": ["true_crime", "mystery"],
        "source": "curated",
    },
    {
        "id": "emoji-burst", "name": "Emoji Burst",
        "text_layout": "corner_short", "accent": "emoji", "bg_treatment": "none",
        "palette": {"text": "#FFFFFF"},
        "pair_with": "reaction", "best_for": ["entertainment", "celebrity"],
        "source": "curated",
    },
    {
        "id": "exclusive-split", "name": "EXCLUSIVE News Split",
        "text_layout": "exclusive_news", "accent": "red_bar", "bg_treatment": "split_faces",
        "palette": {"badge": "#E60028", "bar": "#E60028", "headline_bg": "#FFFFFF",
                    "headline_text": "#111111", "divider": "#FFFFFF"},
        "pair_with": "news_bomb", "best_for": ["celebrity", "entertainment", "trending", "news"],
        "source": "operator-kit (2Pac EXCLUSIVE reference, 2026-09-22)",
    },
    {
        "id": "exclusive-circle", "name": "EXCLUSIVE News Split — Red Circle",
        "text_layout": "exclusive_news", "accent": "red_circle", "bg_treatment": "split_faces",
        "palette": {"badge": "#E60028", "bar": "#E60028", "headline_bg": "#FFFFFF",
                    "headline_text": "#111111", "divider": "#FFFFFF"},
        "pair_with": "news_bomb", "best_for": ["celebrity", "entertainment", "trending", "news"],
        "source": "operator-kit (2Pac EXCLUSIVE reference, 2026-09-22)",
    },
    {
        "id": "exclusive-arrow", "name": "EXCLUSIVE News Split — Curved Arrow",
        "text_layout": "exclusive_news", "accent": "red_arrow", "bg_treatment": "split_faces",
        "palette": {"badge": "#E60028", "bar": "#E60028", "headline_bg": "#FFFFFF",
                    "headline_text": "#111111", "divider": "#FFFFFF"},
        "pair_with": "news_bomb", "best_for": ["celebrity", "entertainment", "trending", "news"],
        "source": "operator-kit (2Pac EXCLUSIVE reference, 2026-09-22)",
    },
]

CURATED_IDS = [s["id"] for s in STYLES]


def load_user_templates(directory: Path | None = None) -> list[dict]:
    """User JSON templates from assets/thumbnail_templates/*.json.

    Schema (documented in the seeded example file):
        {"id": "my-style", "name": "...", "text_layout": "left_stack",
         "accent": "yellow", "bg_treatment": "gradient", "palette": {...},
         "pair_with": "...", "best_for": ["..."], "font": {"size": 96,
         "file": "Montserrat-Bold.ttf"}, "overlay": {"color": "#000000",
         "max_alpha": 230, "exponent": 1.4}, "padding": 6}

    Unknown text_layout values are coerced to "left_stack" (still renders —
    never fails a production run because of a hand-edited JSON).
    """
    directory = directory or USER_TEMPLATE_DIR
    templates: list[dict] = []
    if not directory.exists():
        return templates
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"user thumbnail template unreadable ({path.name}): {e}")
            continue
        if not isinstance(data, dict) or not data.get("id"):
            continue
        data["source"] = "user"
        if data.get("text_layout") not in _KNOWN_LAYOUTS:
            log.warning(f"template '{data['id']}': unknown layout "
                        f"'{data.get('text_layout')}' → left_stack")
            data["text_layout"] = "left_stack"
        templates.append(data)
    return templates


_KNOWN_LAYOUTS = {
    "left_stack",      # one word per line, left-anchored (focal / minimal)
    "pill_stack",      # rounded boxes behind each line (split-boxes / money)
    "corner_short",    # 1-2 words bottom-left corner (red-circle / emoji)
    "split_labels",    # THEN | NOW labels on a split bg
    "hero_number",     # giant $ figure dominating the left half
    "vs_center",       # VS badge between two subjects
    "stamp",           # rotated stamp text (evidence board)
    "exclusive_news",  # EXCLUSIVE bar + white headline bar on a face split
}


def recommend_styles(niche: str, title: str | None = None,
                     user_first: bool = True) -> list[dict]:
    """Best styles for a niche, most-proven first.

    User templates always lead (operator taste beats our priors — and their
    A/B data accrues to their ids), then curated matches by niche tag, then
    the remaining curated defaults so the grid is never empty.
    """
    niche_n = (niche or "").lower()
    niche_hits = [n for n in niche_n.replace("-", " ").split() if len(n) > 2]

    def niche_score(style: dict) -> int:
        tags = " ".join(style.get("best_for", [])).lower()
        return sum(1 for t in niche_hits if t in tags)

    user = load_user_templates() if user_first else []
    curated_by_score = sorted(STYLES, key=niche_score, reverse=True)
    ranked = user + curated_by_score
    # Stable dedupe by id
    seen: set[str] = set()
    out: list[dict] = []
    for s in ranked:
        sid = s.get("id")
        if sid and sid not in seen:
            seen.add(sid)
            out.append(s)
    return out


def seed_example_template() -> Path | None:
    """Drop one commented-schema example template for the operator to copy."""
    USER_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    example = USER_TEMPLATE_DIR / "_example_template.json"
    if not example.exists():
        example.write_text(json.dumps({
            "id": "my-gold-frame",
            "name": "My Gold Frame",
            "text_layout": "left_stack",
            "accent": "yellow",
            "bg_treatment": "gradient",
            "palette": {"accent": "#FFD700", "text": "#FFFFFF"},
            "pair_with": "curiosity_gap",
            "best_for": ["documentary"],
            "font": {"size": 110, "file": "Montserrat-Bold.ttf"},
            "overlay": {"color": "#000000", "max_alpha": 230, "exponent": 1.4},
            "padding": 8,
        }, indent=2), encoding="utf-8")
        log.info(f"Seeded example user template: {example}")
    return example
