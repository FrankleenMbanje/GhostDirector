"""
GhostDirector — Configuration

All settings, paths, API keys, and defaults in one place.
Modify this file to configure the pipeline.
"""

from pathlib import Path
import json
import os


# ──────────────────────────────────────────────
# PATHS (machine-specific locations are configurable — Phase 16)
# ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.resolve()
TEMPLATES_DIR = PROJECT_ROOT / "templates"
ASSETS_DIR = PROJECT_ROOT / "assets"
OUTPUT_DIR = Path(os.environ.get("GD_OUTPUT_DIR") or (PROJECT_ROOT / "output"))
DB_DIR = PROJECT_ROOT / "db"
FONTS_DIR = ASSETS_DIR / "fonts"
MUSIC_DIR = ASSETS_DIR / "music"
SFX_DIR = ASSETS_DIR / "sfx"
OVERLAYS_DIR = ASSETS_DIR / "overlays"

# Ensure output dirs exist
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
DB_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────
# API KEYS
# ──────────────────────────────────────────────
import os
from dotenv import load_dotenv

# Load variables from .env file into the environment
load_dotenv(PROJECT_ROOT / ".env")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")


# ──────────────────────────────────────────────
# LLM SETTINGS
# ──────────────────────────────────────────────
GEMINI_MODEL = "gemini-3.6-flash"  # Free tier model; verify at startup (see verify_gemini_models)
GEMINI_PRO_FALLBACK = "gemini-flash-latest"  # Used only if the primary model 404s at runtime (pro ids are quota-0 on free tier)
GEMINI_TEMPERATURE = 0.8           # Creative but not wild
GEMINI_MAX_OUTPUT_TOKENS = 16384   # 48-scene scripts + humanizer pass need headroom

# FIX-058: image-GENERATION ids (contextual fallback illustrations). Separate
# chain from text models — image ids answer response_modalities=["IMAGE"] on
# generateContent; a pinned id can 404/quota-out independently (same reality
# as the text chain). _gemini_illustration walks these until one returns
# bytes; preview ids that reject IMAGE-only shape get a TEXT+IMAGE retry.
GEMINI_IMAGE_CANDIDATES: list[str] = [
    "gemini-2.5-flash-image",                # nano banana, stable id
    "gemini-2.5-flash-image-preview",
    "gemini-2.0-flash-preview-image-generation",
    "gemini-2.0-flash-exp-image-generation",
]

# Verified-live model chain (populated by verify_gemini_models): every
# candidate that answered a real generation on THIS key, in preference
# order. Call sites walk the whole chain at runtime — a pinned id can 404,
# exhaust its daily bucket, or 503 under load independently of the others
# (all three observed live on 2026-09-18).
GEMINI_LIVE_CANDIDATES: list[str] = []

# Known-current flash ids that must ALWAYS be reachable in the runtime chain,
# even if verify_gemini_models exits early (probe quota, network hiccup).
# The chain walker + daily-quota quarantine in scriptwriter handle dead hops.
# 2026-09-30: the 3 primaries + 4 probes below are all verified answering
# JSON structured output on THIS key — free-tier quota is PER MODEL, so a
# burned day on the primaries still leaves ~4×20 fresh calls for same-day
# recovery runs (FIX-070 note: the Pacific reset refills everything at
# 07:00/08:00 UTC anyway).
_GEMINI_KNOWN_FLASH = [
    "gemini-3.6-flash", "gemini-flash-latest", "gemini-flash-lite-latest",
    "gemini-3.5-flash", "gemini-3.5-flash-lite",
    "gemini-3-flash-preview", "gemini-3.1-flash-lite",
]


def gemini_candidates() -> list[str]:
    """Runtime model chain: primary, pro fallback, known flashes, verified ids."""
    chain = [GEMINI_MODEL, GEMINI_PRO_FALLBACK, *_GEMINI_KNOWN_FLASH]
    chain += [c for c in GEMINI_LIVE_CANDIDATES if c not in chain]
    seen: set[str] = set()
    out: list[str] = []
    for c in chain:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ──────────────────────────────────────────────
# GEMINI MODEL AVAILABILITY GUARD
# ──────────────────────────────────────────────
def verify_gemini_models() -> None:
    """
    Verify the configured Gemini models can actually GENERATE on this API key.

    History:
      - A21: a stale model id silently burned 15 API calls per run.
      - FIX-024: catalog-based auto-upgrade (models.list) — until Google
        started LISTING legacy ids (gemini-2.5-*) that new API keys may not
        call; the catalog check passed and every real call 404'd.
      - FIX-036 (this): the only honest check is a real 1-token generation.
        If the configured model is rejected, walk a candidate list — seeded
        with the exact ids Google's own 404 message recommends — plus the
        newest catalog flash/pro, until one answers. Every candidate failing
        is the only fatal outcome.
    """
    if not GEMINI_API_KEY:
        return  # Offline mode; voice/timestamps/assembly still work
    try:
        from google import genai
        from google.genai import types as genai_types
        client = genai.Client(api_key=GEMINI_API_KEY)

        def _can_generate(model_id: str) -> bool:
            """True when the key can run a real (tiny) generation on this model.

            Fail-fast policy (live-verified 2026-09-18): 429 (quota exhausted)
            and 404 (unknown to this key) return False immediately — probing
            them further just burns wall-clock inside the SDK's internal
            retry loop. Only *config-shape* errors (e.g. thinking_config
            rejected on lite models) retry with a plain config.
            """
            attempts = (
                {"config": genai_types.GenerateContentConfig(
                    max_output_tokens=8,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                )},
                {"config": genai_types.GenerateContentConfig(max_output_tokens=8)},
            )
            for kwargs in attempts:
                try:
                    client.models.generate_content(
                        model=model_id, contents="ping", **kwargs
                    )
                    return True
                except Exception as e:
                    s = str(e)
                    if ("429" in s or "RESOURCE_EXHAUSTED" in s.upper()
                            or "404" in s or "NOT_FOUND" in s.upper()):
                        if "429" in s or "RESOURCE_EXHAUSTED" in s.upper():
                            print(f"[config] '{model_id}' is quota-exhausted right "
                                  f"now — trying next candidate")
                        return False
                    continue
            return False

        # Preference order. The 3.6-flash / 3.1-pro ids come straight from
        # Google's 404 messages on real new keys. The -latest aliases sit on
        # SEPARATE daily quota buckets (found live 2026-09-18: an exhausted
        # 3.6-flash daily limit left the alias buckets untouched), so they
        # are the resilience path when a pinned id is quota-dead for the day.
        flash_candidates = [
            "gemini-3.6-flash", "gemini-flash-latest",
            "gemini-flash-lite-latest",
        ]
        # Free tier reality (found live 2026-09-23): pro-preview ids LIST fine
        # but CALL with quota limit: 0 — they burn attempts and crash the run.
        # Free tier is flash-only; the -latest aliases carry separate buckets.
        pro_candidates = [
            "gemini-flash-latest", "gemini-flash-lite-latest",
        ]
        # Append the newest catalog ids as extra fallbacks (harmless if listing fails)
        try:
            import re as _re
            available = [
                m.name.lstrip("models/") for m in client.models.list()
                if "generateContent" in (m.supported_actions or [])
            ]

            def _newest(prefix: str) -> str | None:
                best = None
                for name in available:
                    m = _re.match(rf"gemini-(\d+)\.(\d+)-{prefix}", name)
                    if m:
                        key = (int(m.group(1)), int(m.group(2)))
                        if best is None or key > best[0]:
                            best = (key, name)
                return best[1] if best else None

            for newest, bucket in ((_newest("flash"), flash_candidates), (_newest("pro"), pro_candidates)):
                if newest and newest not in bucket:
                    bucket.append(newest)
        except Exception:
            pass

        global GEMINI_MODEL, GEMINI_PRO_FALLBACK

        if _can_generate(GEMINI_MODEL):
            # Primary works; make sure the pro fallback does too (degrade quietly)
            if GEMINI_PRO_FALLBACK != GEMINI_MODEL and not _can_generate(GEMINI_PRO_FALLBACK):
                alt = next(
                    (c for c in pro_candidates if c != GEMINI_MODEL and _can_generate(c)),
                    GEMINI_MODEL,
                )
                print(
                    f"[config] GEMINI_PRO_FALLBACK '{GEMINI_PRO_FALLBACK}' rejected by this "
                    f"key — verified working: '{alt}'"
                )
                GEMINI_PRO_FALLBACK = alt
            _record_live_chain(flash_candidates + pro_candidates)
            return

        # Primary rejected: probe candidates until one truly answers
        working_flash = next((c for c in flash_candidates if _can_generate(c)), None)
        if not working_flash:
            raise RuntimeError(
                f"GEMINI_MODEL '{GEMINI_MODEL}' and every known flash candidate "
                f"failed on this API key. Check quota/billing or model availability."
            )
        print(
            f"[config] GEMINI_MODEL '{GEMINI_MODEL}' rejected by this API key — "
            f"verified working: '{working_flash}'"
        )
        GEMINI_MODEL = working_flash

        if GEMINI_PRO_FALLBACK == GEMINI_MODEL or not _can_generate(GEMINI_PRO_FALLBACK):
            working_pro = next(
                (c for c in pro_candidates if c != GEMINI_MODEL and _can_generate(c)),
                GEMINI_MODEL,
            )
            if working_pro != GEMINI_PRO_FALLBACK:
                print(
                    f"[config] GEMINI_PRO_FALLBACK '{GEMINI_PRO_FALLBACK}' rejected — "
                    f"verified working: '{working_pro}'"
                )
            GEMINI_PRO_FALLBACK = working_pro
        _record_live_chain(flash_candidates + pro_candidates)
    except RuntimeError:
        raise
    except Exception as e:
        # Network hiccup or SDK drift should not block an offline-capable run
        print(f"[config] Gemini model check skipped: {e}")


def _record_live_chain(candidates: list[str]) -> None:
    """Remember which candidate ids this key can actually use (order kept).

    Called at the end of verify_gemini_models; only ids are listed here —
    liveness was already established by the probes above (a 503-under-load
    id still answers probes, so it stays in the chain as a later hop).
    """
    global GEMINI_LIVE_CANDIDATES
    seen: list[str] = []
    for c in [GEMINI_MODEL, GEMINI_PRO_FALLBACK, *candidates]:
        if c and c not in seen:
            seen.append(c)
    GEMINI_LIVE_CANDIDATES = seen


# ──────────────────────────────────────────────
# VOICE DEFAULTS
# ──────────────────────────────────────────────
DEFAULT_TTS_PROVIDER = "edge-tts"

# Edge-TTS settings
EDGE_TTS_VOICE = "en-US-GuyNeural"
EDGE_TTS_RATE = "+0%"
EDGE_TTS_PITCH = "+0Hz"

# ElevenLabs settings (optional premium)
ELEVENLABS_VOICE_ID = ""
ELEVENLABS_MODEL = "eleven_multilingual_v2"   # v2: better prosody/emotion than monolingual_v1
ELEVENLABS_STABILITY = 0.45
ELEVENLABS_SIMILARITY_BOOST = 0.8


# ──────────────────────────────────────────────
# WHISPER SETTINGS
# ──────────────────────────────────────────────
WHISPER_MODEL = "base"        # "base" fits 2GB VRAM (MX450)
WHISPER_DEVICE = "cuda"       # "cuda" for GPU, "cpu" for fallback
WHISPER_LANGUAGE = "en"


# ──────────────────────────────────────────────
# VIDEO DEFAULTS
# ──────────────────────────────────────────────
DEFAULT_RENDER_MODE = "ffmpeg"    # "ffmpeg" or "resolve"
DEFAULT_TEMPLATE = "celebrity"

# FFmpeg
FFMPEG_BIN = "ffmpeg"             # Must be on PATH
FFPROBE_BIN = "ffprobe"           # Must be on PATH

# Long-form video
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
VIDEO_FPS = 30
VIDEO_CODEC = "libx264"
VIDEO_CRF = 18                    # Quality (lower = better, 18 = visually lossless)
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"

# Shorts
SHORTS_WIDTH = 1080
SHORTS_HEIGHT = 1920
SHORTS_MAX_DURATION = 59          # YouTube Shorts limit


# ──────────────────────────────────────────────
# AUDIO MASTERING
# ──────────────────────────────────────────────
VOICE_VOLUME_DB = -16
MUSIC_VOLUME_DB = -24
MASTER_LUFS = -14
DUCKING_REDUCTION_DB = -12


# ──────────────────────────────────────────────
# STOCK FOOTAGE
# ──────────────────────────────────────────────
PEXELS_BASE_URL = "https://api.pexels.com"
PIXABAY_BASE_URL = "https://pixabay.com/api"
PEXELS_VIDEO_URL = f"{PEXELS_BASE_URL}/videos/search"
PEXELS_PHOTO_URL = f"{PEXELS_BASE_URL}/v1/search"
PIXABAY_VIDEO_URL = f"{PIXABAY_BASE_URL}/videos/"
PIXABAY_PHOTO_URL = f"{PIXABAY_BASE_URL}/"
STOCK_MIN_WIDTH = 1280            # Minimum acceptable resolution
STOCK_PREFERRED_ORIENTATION = "landscape"


# ──────────────────────────────────────────────
# THUMBNAIL
# ──────────────────────────────────────────────
THUMBNAIL_WIDTH = 1280
THUMBNAIL_HEIGHT = 720


# ──────────────────────────────────────────────
# YOUTUBE (Phase 4)
# ──────────────────────────────────────────────
YOUTUBE_CLIENT_SECRETS_FILE = PROJECT_ROOT / "client_secrets.json"
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# ──────────────────────────────────────────────
# TWO-CHANNEL ERA (2026-09-21): Rise and Ruin owns the rise-and-fall lane;
# The Fame Files is the trending celebrity news channel (shorts-first).
# ──────────────────────────────────────────────
CHANNEL_RISEANDRUIN = "riseandruin"
CHANNEL_FAMEFILES = "famefiles"


def youtube_token_path(channel: str | None = None) -> Path:
    """Per-channel OAuth token file ('default' keeps the legacy path).

    One browser consent per channel; tokens live side-by-side in the project
    root (youtube_token.riseandruin.json / youtube_token.famefiles.json).
    """
    if not channel or channel == "default":
        return PROJECT_ROOT / "youtube_token.json"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in channel.lower())[:40]
    return PROJECT_ROOT / f"youtube_token.{safe}.json"


# ──────────────────────────────────────────────
# TRENDING NEWS (Fame Files daily shorts)
# ──────────────────────────────────────────────
TRENDING_NEWS_DB = DB_DIR / "trending_news.json"
TRENDING_MAX_AGE_HOURS = 36        # news older than this is not "trending"
TRENDING_CANDIDATE_POOL = 24       # stories pulled before scoring
TRENDING_TOP_N = 5                 # finalists shown/considered per run

# ──────────────────────────────────────────────
# GROWTH LAYER (2026-09-18 — beat-VidNinjas plan)
# ──────────────────────────────────────────────
# A5: YouTube-autocomplete SEO enrichment on every render (set 0 to disable
# on offline machines — the metadata generator degrades to script tags).
SEO_AUTOCOMPLETE = os.environ.get("SEO_AUTOCOMPLETE", "1").lower() not in ("0", "false", "no")

# A10: channel niche — powers thumbnail style recommendations and competitor
# discovery (db/channel_profile.json overrides this at runtime).
CHANNEL_NICHE = os.environ.get("CHANNEL_NICHE", "documentary true crime business")

# A3: analytics store (retention joins + imported CTR) and A4 scout outputs
ANALYTICS_STORE_PATH = DB_DIR / "analytics.json"
TOPIC_QUEUE_PATH = DB_DIR / "topic_queue.json"
COMPETITORS_PATH = DB_DIR / "competitors.json"


# ──────────────────────────────────────────────
# TEMPLATE LOADER
# ──────────────────────────────────────────────
def load_template(name: str) -> dict:
    """Load a template by name, with _base.json inheritance."""
    base_path = TEMPLATES_DIR / "_base.json"
    template_path = TEMPLATES_DIR / f"{name}.json"

    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    # Load base defaults
    base = {}
    if base_path.exists():
        base = json.loads(base_path.read_text(encoding="utf-8"))

    # Load specific template
    template = json.loads(template_path.read_text(encoding="utf-8"))

    # Deep merge: template overrides base
    merged = _deep_merge(base, template)
    return merged


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
