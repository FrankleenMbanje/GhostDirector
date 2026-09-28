"""
GhostDirector — Channel State (Phase 3.12 persona rotation + Phase 2.9 packaging log)

Small JSON state in db/channel_state.json with two jobs:

1. Persona rotation: every render draws a deterministic variation seed,
   voice id, palette accent and transition seed so consecutive uploads never
   share a fingerprint. Randomness is seeded from (counter, topic) so a
   re-render of the same video reproduces its persona.
2. Packaging log (Phase 2.9 data layer): which title_variant and thumbnail
   variant went live per video, ready to join with CTR data later.

Plan item A7 (multi-channel): every function takes a `channel` profile name.
The default profile keeps the legacy `channel_state.json` path; any other
profile gets `channel_state_<name>.json`, so a second channel can run on the
same factory without fingerprint or packaging-log bleed.
"""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path

import config
from utils.logger import get_logger

log = get_logger("channel_state")

_PATH = config.DB_DIR / "channel_state.json"

# Voice pool for rotation. Order matters for readability in logs; the draw is
# seeded, not random, so re-renders match.
VOICE_POOL = [
    "en-US-GuyNeural",
    "en-US-ChristopherNeural",
    "en-US-EricNeural",
    "en-US-AndrewNeural",
    "en-US-BrianNeural",
]

# Accent/emphasis palettes for captions + thumbnails (hex, no #)
PALETTE_POOL = [
    {"highlight": "#FFD700", "accent": "#FF0000"},   # gold on red (classic)
    {"highlight": "#00E5FF", "accent": "#CC0000"},   # electric cyan
    {"highlight": "#7CFC00", "accent": "#008000"},   # acid green
    {"highlight": "#FF6EC7", "accent": "#4B0082"},   # hot pink
    {"highlight": "#FFA500", "accent": "#000000"},   # amber on black
]


def _path_for(channel: str) -> Path:
    """One state file per channel profile; 'default' keeps the legacy path."""
    if not channel or channel == "default":
        return _PATH
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in channel.lower())[:40]
    return config.DB_DIR / f"channel_state_{safe}.json"


def list_channels() -> list[str]:
    """All channel profiles that have state on disk (default first)."""
    found = sorted(
        p.stem.replace("channel_state_", "")
        for p in config.DB_DIR.glob("channel_state_*.json")
    )
    return ["default"] + found


def _load(channel: str = "default") -> dict:
    path = _path_for(channel)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f"Could not read {path.name} ({e}); starting fresh")
        return {}


def _save(state: dict, channel: str = "default") -> None:
    path = _path_for(channel)
    try:
        path.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning(f"Could not save channel state ({channel}): {e}")


def draw_persona(topic: str, channel: str = "default") -> dict:
    """Pick this video's persona variation, advancing the rotation counter.

    Deterministic in (counter, topic): a crash + re-render reproduces the
    same persona instead of silently changing the video's voice.
    """
    state = _load(channel)
    counter = int(state.get("persona_counter", 0))
    seed = f"persona:{channel}:{counter}:{topic}"

    rng = random.Random(seed)
    persona = {
        "counter": counter,
        "seed": counter,                      # feeds voice/palette/caption jitters
        "voice_id": rng.choice(VOICE_POOL),
        "palette": rng.choice(PALETTE_POOL),
        "transition_seed": rng.randint(0, 999_999),
    }

    state["persona_counter"] = counter + 1
    state["last_persona"] = persona
    _save(state, channel)
    log.info(
        f"[{channel}] Persona #{counter}: voice={persona['voice_id']} "
        f"palette={persona['palette']['highlight']} tseed={persona['transition_seed']}"
    )
    return persona


def apply_persona_to_template(template: dict, persona: dict) -> dict:
    """Apply the drawn persona to a template dict (in place, returns it).

    Only overrides values the operator has not explicitly customized: the
    base Edge-TTS voice rotates; an explicitly set template voice wins.
    """
    voice_cfg = template.setdefault("voice", {})
    if voice_cfg.get("provider", "edge-tts") == "edge-tts":
        voice_cfg["voice_id"] = persona["voice_id"]

    pal = persona.get("palette", {})
    template.setdefault("captions", {})["highlight_color"] = pal.get(
        "highlight", template.get("captions", {}).get("highlight_color", "#FFD700")
    )
    template.setdefault("thumbnail", {})["color_overlay"] = pal.get(
        "accent", template.get("thumbnail", {}).get("color_overlay", "#CC0000")
    )
    return template


def log_packaging(
    topic: str,
    title_used: str,
    title_variants: list[str],
    thumbnail_variant: str,
    youtube_url: str | None = None,
    channel: str = "default",
) -> None:
    """Record the packaging that went live for this video (Phase 2.9 data layer).

    The CTR field is filled in by pipeline/analytics.py (A3) once the video
    has impression data on YouTube.
    """
    state = _load(channel)
    entries = state.setdefault("packaging_log", [])
    entries.append({
        "date": datetime.now().isoformat(timespec="seconds"),
        "topic": topic,
        "channel": channel,
        "title_used": title_used,
        "title_variants": title_variants,
        "thumbnail_variant": thumbnail_variant,
        "youtube_url": youtube_url,
        "ctr": None,
    })
    state["packaging_log"] = entries[-500:]
    _save(state, channel)
    log.info(f"[{channel}] Packaging logged: title='{title_used[:40]}' thumb={thumbnail_variant}")
