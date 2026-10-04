"""
GhostDirector — FFmpeg Video Assembler

Headless video assembly using FFmpeg. Takes all generated assets
(scene videos, audio, timestamps) and produces the final MP4.
"""

import sys
import os
import json
import re
import shutil
import random
import math
import statistics
import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Script
import config
from utils.logger import get_logger
from utils.ffmpeg_cmd import (
    run_ffmpeg, scale_and_crop, photo_to_video, concat_videos,
    add_audio_to_video, mix_audio_with_music, normalize_audio,
    burn_subtitles, get_duration, cut_video_segment,
)
from utils.caption_renderer import generate_ass_subtitles

log = get_logger("assembler_ffmpeg")

# Cache-buster for per-scene prepared clips (FIX-044): bump when the visual
# preparation pipeline changes (easing, motion, grading) so --resume never
# reuses stale cached renders from an older edit engine. Prepared clips and
# SFX stems embed this tag in their filenames.
PREPARED_V = "v4"  # v4: FIX-051 b-roll cutaway alternation (main shot ↔ real footage)
# v3: FIX-045 taste layer (mood-driven cuts/zoom) + VidRush captions


# ─────────────────────────────────────────────────────────────
# FIX-045 — EDIT-TASTE MODEL (the VidRush layer)
#
# A human editor doesn't apply the same energy everywhere. They read the
# emotional beat and choose: hype gets rapid cuts and punchy SFX; grief and
# revelations get long, still, silent takes; neutral connective tissue gets
# a steady mid pace. This table encodes that judgment so the assembler can
# VARY intensity per scene instead of stamping out uniform cuts.
#
#   cut_sec    → seconds of scene per punch-cut (lower = faster cutting)
#   max_cuts   → hard cap on punch-cuts within one scene
#   min_shot   → the shortest sub-shot allowed (fast editing uses quicker shots)
#   zoom_speed → Ken Burns speed multiplier (calm scenes drift slower)
#   sfx        → whether cut accents get a whoosh/impact at all (silence is
#                a choice a human editor makes — restraint IS taste)
#
# Unknown moods fall back to "neutral". The hook/opening scene is always
# treated as "hook" (max energy) regardless of its labeled mood.
# ─────────────────────────────────────────────────────────────
EDIT_TASTE: dict[str, dict] = {
    "dramatic":    {"cut_sec": 3.2, "max_cuts": 3, "min_shot": 1.5, "zoom_speed": 1.0,  "sfx": True},
    "dark":        {"cut_sec": 4.0, "max_cuts": 2, "min_shot": 1.8, "zoom_speed": 0.85, "sfx": False},
    "suspenseful": {"cut_sec": 3.6, "max_cuts": 3, "min_shot": 1.6, "zoom_speed": 1.05, "sfx": True},
    "emotional":   {"cut_sec": 6.0, "max_cuts": 2, "min_shot": 3.0, "zoom_speed": 0.7,  "sfx": False},
    "upbeat":      {"cut_sec": 2.6, "max_cuts": 3, "min_shot": 1.3, "zoom_speed": 1.2,  "sfx": True},
    "triumphant":  {"cut_sec": 2.8, "max_cuts": 3, "min_shot": 1.4, "zoom_speed": 1.15, "sfx": True},
    "neutral":     {"cut_sec": 4.2, "max_cuts": 2, "min_shot": 1.8, "zoom_speed": 0.95, "sfx": True},
}


def _edit_taste(scene, idx: int) -> dict:
    """Resolve the per-scene edit-taste profile (FIX-045)."""
    if idx == 0:
        return EDIT_TASTE["upbeat"]  # opening scene: max energy (the hook)
    mood = (getattr(scene, "mood", None) or "neutral").strip().lower()
    return EDIT_TASTE.get(mood, EDIT_TASTE["neutral"])


# Mood-aware transition mapping — the outgoing scene's mood picks the cut.
MOOD_TRANSITIONS = {
    "dramatic": "fadeblack", "dark": "fadeblack", "suspenseful": "dissolve",
    "emotional": "dissolve", "upbeat": "smoothright", "triumphant": "circleopen",
    "neutral": "fade",
}

# Subtle teal-orange style grade applied before subtitle burn.
CINEMATIC_GRADE = "eq=contrast=1.06:saturation=1.1"

# ─────────────────────────────────────────────────────────────
# FIX-043 (2026-09-18): edit-feel upgrade, four parts.
#
# 1. HARD-CUT PACING. Documentary-grade editors cut on the action — a
#    straight cut feels confident; a cross-dissolve on every scene boundary
#    reads as slideshow. Mood dissolves (fadeblack/dissolve/circleopen) are
#    now ACT accents, not defaults; hard cut is the baseline.
# 2. SMOOTHSTEP KEN BURNS. zoompan animates a LINEAR ramp between keyframes,
#    so motion starts and stops abruptly (slideshow drift). The 
#    zoom/pan expression now eases with smoothstep on `on` — slow-in,
#    slow-out — and default zoom range is tightened 1.0→1.12 to a subtle
#    1.04→1.10 push that keeps life without the "PowerPoint zoom".
# 3. KINETIC HOOK CARD. A 2.2s title card on the very first narration beat:
#    dark bg, Anton-style bold type (two lines), scale 0.92→1.08 pop + fade,
#    cut to black → hard cut into scene 1 with its narration. Retention
#    battle won in the first 3 seconds, silent-autoplay safe.
# 4. TWO-ACT MUSIC. One looped bed for 8 minutes is monotony; the mix now
#    switches to a contrasting mood track at the midpoint scene (drawn from
#    the script's own mood palette) with a 1.5s crossfade.
# ─────────────────────────────────────────────────────────────

# Act-2 contrast partner per scene mood (FIX-043.4): the midpoint music
# switch picks the contrasting act so the video has musical structure.
ACT_MOOD_PAIR = {
    "dark": "dramatic",
    "dramatic": "dark",
    "suspenseful": "dramatic",
    "emotional": "dramatic",
    "upbeat": "dramatic",
    "triumphant": "dramatic",
    "neutral": "dramatic",
}


def _transition_for_cut(
    mood: str | None, trans_cfg: dict, cut_index: int, index_offset: int = 0
) -> tuple[str, float]:
    """Resolve (transition type, duration multiplier) for one cut (FIX-043.1).

    Hard cuts are the baseline (multiplier 0.0 → concat path). Mood-act
    accents fire on ~1 in 3 cuts:
      - fadeblack after a dramatic/dark scene (act punctuation, ~1× per act)
      - smoothright after upbeat
      - one dissolve per act, max (beat-group separator)
    """
    style = trans_cfg.get("style", "mood")
    if style in ("none", "hard"):
        return ("none", 0.0)

    rhythm = trans_cfg.get("accent_rhythm", 3)   # accent every Nth cut baseline
    # FIX-068: in bounded-pass rendering `cut_index` is pass-local; the offset
    # restores the GLOBAL cut index so accent rhythm / act punctuation match
    # the single-pass edit plan exactly.
    gidx = cut_index + index_offset
    is_accent_slot = (gidx % max(int(rhythm), 2)) == 0
    act_boundary = gidx in (6, 12, 18)            # fadeblack punctuation slots

    mood_t = MOOD_TRANSITIONS.get(mood or "", "fade")
    if mood == "dramatic" or mood == "dark":
        if act_boundary:
            return ("fadeblack", 1.0)
        return ("none", 0.0)
    if mood == "upbeat":
        return ("smoothright", 1.0) if is_accent_slot else ("none", 0.0)
    if mood in ("suspenseful", "emotional"):
        return ("dissolve", 1.0) if is_accent_slot and cut_index % 6 == 3 else ("none", 0.0)
    if mood == "triumphant":
        return ("circleopen", 1.0) if is_accent_slot else ("none", 0.0)
    return ((mood_t, 1.0) if is_accent_slot else ("none", 0.0)) if mood_t != "fade" else ("none", 0.0)


# ─────────────────────────────────────────────────────────────
# FIX-059 — HUMAN EDIT LANGUAGE: pre-laps (J-cuts) + motivated hard cuts.
#
# A human editor lets the next scene's first words breathe over the tail of
# the previous shot (the J-cut / "audio pre-lap") — it kills the slideshow
# feel of cut-then-talk-then-cut and is the single strongest tell of human
# editing. Mechanically: every scene's narration audio file carries ~0.8s
# of trailing TTS silence (live-measured), so starting scene i's audio
# PRE_LAP_S before its video does NOT overlap any real speech of scene i-1.
# Pre-laps are applied at MOTIVATED HARD CUTS only (xfade cuts keep the
# current audio-at-video-start behavior). Because scene clips are back to
# back on the video timeline, shifting audio i earlier by L(i) shifts the
# effective audio starts of every later scene by the same amount — the
# whole J-cut chain stays contiguous with NO holes (no audio starts later
# than its video). Caption and SFX timing are compensated by the same
# amounts so words stay pinned to speech.
# ─────────────────────────────────────────────────────────────
PRE_LAP_MIN_S = 0.35
PRE_LAP_MAX_S = 0.55
# Objective probe of the narration tail: the silence run must extend to
# EOF and leave at least PRE_LAP_MIN_S + margin of room, or the pre-lap
# would overlap real words.
PRE_LAP_PROBE_FLOOR_DB = -38
# Question→answer + contrast are the two motivated hard-cut relationships a
# news editor actually uses; others stay on the default (xfade) plan.
_PRE_LAP_QUESTION_RE = re.compile(r"\?+\s*$")
_PRE_LAP_CONTRAST_RE = re.compile(
    r"\b(but|however|instead|until|then|suddenly|yet)\b", re.IGNORECASE
)


def _audio_leads_by(audio_path: str | Path | None) -> float:
    """Safe pre-lap size for this scene's narration, or 0.0.

    The pre-lap OVERLAPS this file's trailing silence with the incoming
    scene's first words, so the usable lead is the length of the silence
    run that extends to EOF (not merely any detected silence). Bounded to
    PRE_LAP_MAX_S with a safety margin; any error → 0.0 (a pre-lap is an
    enhancement, never a risk to speech).
    """
    try:
        if not audio_path or not Path(audio_path).exists():
            return 0.0
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(audio_path),
             "-af", f"silencedetect=noise={PRE_LAP_PROBE_FLOOR_DB}dB:d=0.2",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        err = r.stderr or ""
        starts = [float(m.group(1)) for m in re.finditer(r"silence_start: ([0-9.]+)", err)]
        ends = [float(m.group(1)) for m in re.finditer(r"silence_end: ([0-9.]+)", err)]
        if not starts:
            return 0.0
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(audio_path)],
            capture_output=True, text=True, timeout=30,
        )
        dur = float((p.stdout or "").strip().splitlines()[0])
        # The LAST silence run must extend to EOF: either it never ended
        # (few ends than starts) or it ends at the file's own duration.
        # Silence that ENDS mid-file is not a pre-lap buffer — real speech
        # follows it.
        if len(ends) >= len(starts) and dur - ends[-1] > 0.05:
            return 0.0
        tail = starts[-1]
        trailing = dur - tail
        lead = min(PRE_LAP_MAX_S, trailing - 0.12)
        return round(lead, 3) if lead >= PRE_LAP_MIN_S else 0.0
    except Exception:
        return 0.0


def _cut_styles(
    scenes: list,
    lead_by: list[float],
) -> list[str]:
    """One style label per scene boundary (len = N-1): 'prelap_hard_cut',
    'hard_cut' or 'xfade'. Restrained by design: pre-laps fire on at most
    every third boundary, motivated by a question→answer or a contrast
    beat, and never back to back. Everything else keeps the FIX-043 plan."""
    n = max(len(scenes) - 1, 0)
    styles: list[str] = []
    last_pre = -9
    for j in range(n):
        out_narr = (getattr(scenes[j], "narration", "") or "").strip()
        motivated = bool(_PRE_LAP_QUESTION_RE.search(out_narr[-120:])
                         or _PRE_LAP_CONTRAST_RE.search(out_narr[-120:]))
        if lead_by[j] > 0.0 and motivated and j - last_pre >= 3:
            styles.append("prelap_hard_cut")
            last_pre = j
        elif j % 2 == 1:
            # Alternate the rest so hard cuts and dissolves interleave —
            # one technique on every cut is its own template fingerprint.
            styles.append("hard_cut")
        else:
            styles.append("xfade")
    return styles


def _transition_for_mood(mood: str | None, trans_cfg: dict) -> str:
    """Resolve the xfade transition type for a cut following a scene of this mood.

    Kept for shorts.py; long-form now routes through _transition_for_cut
    (FIX-043.1 hard-cut pacing with mood accents).
    """
    style = trans_cfg.get("style", "mood")
    if style in ("none", "mood"):
        return MOOD_TRANSITIONS.get(mood or "", "fade")
    return style


# FIX-058: subject-aware music. A template's music_mood is a BLANKET —
# "upbeat" made a serious news report sound like kids' entertainment.
# With "auto", the narration's subject decides: tragedy → restrained,
# tension → suspenseful, business/success → dramatic, entertainment → chill.
_SENSITIVE_SUBJECT = re.compile(
    r"\b(died|death|dead|killed|murder|murdered|tragic|tragedy|suicide|"
    "overdose|funeral|cancer|illness|hospital|scandal|trial|guilty|"
    "convicted|sentenced|arrested|lawsuit|allegations|abuse|assault|"
    "divorce|breakup|feud|addiction|relapse|crisis|controversy|"
    "downfall|collapse|bankrupt|fraud|scam|victim)\\b",
    re.I,
)
_TENSE_SUBJECT = re.compile(
    r"\b(drama|exposed|shocking|secret(s)?|reveal(ed)?|collapse(d|s)?|"
    "feud|warn(ed|ing)|threat|danger|risk|betray|investigation)\\b", re.I)
_SUCCESS_SUBJECT = re.compile(
    r"\b(won|wins|record|billion|million|deal|empire|success|successf)", re.I)


# FIX-061 — story-STRUCTURE signals (Phase 2 music intelligence): the same
# "suspenseful" label can hide a party or a lawsuit, so the resolution also
# reads the story's shape — a major announcement wants confident cinematic
# energy, a celebration wants modern polish (never bounce), a reflective
# career look wants restrained emotion.
_ANNOUNCE_SUBJECT = re.compile(
    r"\b(announc\w+|reveal\w*|unveil\w*|debuts?|debut\w*|premiere[sd]?|"
    r"launch\w*|confirms?|confirmed|drop(?:s|ped)?|anniversary|"
    r"returns?|comeback|first look|teaser|trailer)\b", re.IGNORECASE)
_CELEBRATION_SUBJECT = re.compile(
    r"\b(celebrat\w+|wins?|won|award[s]?|trophy|record(?:-| )?breaking|"
    r"milestone|number one|no\. ?1|tops? the (chart|box office)|"
    r"smashes?|sells? out|engag\w+|wedding|marri\w+|birthday)\b", re.IGNORECASE)
_REFLECTIVE_SUBJECT = re.compile(
    r"\b(legacy|remember\w*|look(?:s)? back|career|decades?|years ago|"
    r"nostalg\w+|tribute|retir\w+|farewell|through the years|journey)\b",
    re.IGNORECASE)


_DEATH_SUBJECT = re.compile(
    r"\b(died|death|dead|killed|murder|murdered|tragic|tragedy|suicide|"
    r"overdose|funeral|cancer|illness|hospital|mourn\w*)\b", re.I)
_LEGAL_SUBJECT = re.compile(
    r"\b(lawsuit|trial|guilty|convicted|sentenced|arrested|allegations|"
    r"fraud|scam|bankrupt|controversy|investigation|court|sue[ds]?|"
    r"legal fight|settlement)\b", re.I)


def resolve_music_mood(template_cfg_mood: str, narration_text: str) -> str:
    """Map (template music_mood, narration subject+structure) → music mood.

    "auto" (or a mismatched blanket like "upbeat" on a somber story) is
    resolved by the words being spoken. Serious celebrity news gets
    restrained cinematic/documentary beds — never playful beds. Priority:
    sensitive > tense > structure (celebration / announcement / reflection)
    > default. An explicit non-auto template mood still WINS when no
    sensitive/tense/structure signal fires (operator choice is respected).
    """
    text = narration_text or ""
    sensitive = bool(_SENSITIVE_SUBJECT.search(text))
    tense = bool(_TENSE_SUBJECT.search(text))
    if sensitive:
        # Legal/scandal hard news wants subtle tension; death/tragedy wants
        # somber restraint. Both stay mature — never playful.
        if _LEGAL_SUBJECT.search(text) and not _DEATH_SUBJECT.search(text):
            return "dark" if tense else "suspenseful"
        return "dark" if tense else "chill"      # restrained, somber, mature
    if tense:
        return "suspenseful"                      # tension before a reveal
    explicit = template_cfg_mood not in ("auto", "", None)
    if _CELEBRATION_SUBJECT.search(text):
        return "dramatic" if explicit else "upbeat"   # modern polish, not bounce
    if _ANNOUNCE_SUBJECT.search(text):
        return "dramatic"                          # confident cinematic energy
    if _REFLECTIVE_SUBJECT.search(text):
        return "dark" if explicit else "emotional"    # restrained, mature
    if template_cfg_mood == "auto":
        if _SUCCESS_SUBJECT.search(text):
            return "dramatic"                     # cinematic momentum, not bouncy
        return "dramatic"                         # neutral-news default
    return template_cfg_mood


def _find_music_track(mood: str) -> Path | None:
    """Find a background music track matching the mood."""
    mood_dir = config.MUSIC_DIR / mood
    if not mood_dir.exists():
        # Try any available mood
        for fallback_dir in config.MUSIC_DIR.iterdir():
            if fallback_dir.is_dir() and list(fallback_dir.glob("*.mp3")):
                mood_dir = fallback_dir
                break
        else:
            return None

    tracks = list(mood_dir.glob("*.mp3")) + list(mood_dir.glob("*.wav"))
    if not tracks:
        return None
    return random.choice(tracks)


def _build_hook_card(script, template: dict, temp_dir: Path) -> Path | None:
    """Kinetic title card for the first seconds (FIX-043.3).

    A 2.2s dark card with the hook text in bold two-line type, easing
    scale-pop 0.92→1.08 (smoothstep — same easing as Ken Burns) + 0.25s
    fade-in / 0.3s fade-out, then a HARD cut into scene 1 with narration
    already running. This wins the silent-autoplay first frame and the
    first-3-seconds retention battle. Text goes through textfile= so no
    drawtext escaping is needed.
    """
    visuals_cfg = template.get("visuals", {})
    width = int(visuals_cfg.get("resolution", "1920x1080").split("x")[0])
    height = int(visuals_cfg.get("resolution", "1920x1080").split("x")[1])
    # FIX-071: card_dur was never defined after the FIX-044 zoompan rework —
    # every build silently skipped the hook card with "name 'card_dur' is
    # not defined". 2.2 s per the design below (0.25 s fade-in handled by
    # the card's own fade-out timing; narration starts under the card).
    card_dur = 2.2
    font = config.FONTS_DIR / "Montserrat-Bold.ttf"
    if not font.exists():
        return None

    raw = (getattr(script, "hook_overlay_text", None) or script.title or "").strip()
    if not raw:
        return None
    words = raw.split()
    if len(words) <= 3:
        lines = [raw]
    else:  # balance two lines
        mid = (len(words) + 1) // 2
        lines = [" ".join(words[:mid]), " ".join(words[mid:])]
    lines = lines[:2]

    line_files: list[Path] = []
    for i, line in enumerate(lines):
        tf = temp_dir / f"hook_line_{i}.txt"
        tf.write_text(line[:42], encoding="utf-8")
        line_files.append(tf)
    fps = 30
    total_frames = int(card_dur * fps)
    # Render at 2× then zoom down — zoompan raster-scale gives the type real pop.
    big_w, big_h = width * 2, height * 2
    eased = f"min(max(on/{max(total_frames - 1, 1)},0),1)"
    p = f"({eased}*{eased}*(3-2*{eased}))"
    font_path = font.as_posix().replace(":", "\\:")
    # Windows drive colons break the filter-graph option parser — every
    # absolute path inside the filter string needs the same escape.
    def _fp(p: Path) -> str:
        return p.as_posix().replace(":", "\\:")

    draws = []
    y_center = big_h // 2
    offsets = [-190, 190] if len(lines) == 2 else [0]
    colors = ["white", "0xE2A33C"]
    for i, tf in enumerate(line_files):
        draws.append(
            f"drawtext=fontfile='{font_path}':textfile='{_fp(tf)}':"
            f"fontsize={'150' if len(lines) == 2 else '175'}:fontcolor={colors[min(i, 1)]}:"
            f"x=(w-text_w)/2:y={y_center}+{offsets[min(i, 1)]}-(text_h/2)"
        )

    card_path = temp_dir / "hook_card.mp4"
    # FIX-061 (Phase 1.4/1.5): the hook must establish WHAT IS HAPPENING on
    # the first frame — scene 1's own story-picked asset (already saliency-
    # framed) becomes the card background with restrained type on top; the
    # flat dark plate is only a last resort. Photos render at 2× for the
    # zoompan type pop; videos run through the same trim as before.
    scene1_photo = getattr(script.scenes[0], "photo_path", None) if script.scenes else None
    scene1_video = getattr(script.scenes[0], "video_path", None) if script.scenes else None
    # FIX-093: the "kinetic" card rendered COMPLETELY STATIC — measured 0.00
    # mean frame motion across its full 2.2s (frame-diff probe, 2026-10-04).
    # Root cause: zoompan's `on` counts frames produced from the CURRENT
    # input image; `-loop 1` + `d=1` yields exactly one frame per input
    # image, so `on` stayed 0 and the scale-pop never advanced. Every short
    # and every doc opened on a 2.2-second freeze frame — half the test
    # audience swipes in that window (engaged views ~50%). Photo path: ONE
    # image input with d=total_frames so `on` counts 0..N-1; video/color
    # paths keep d=1 (their own frame-to-frame motion carries).
    def _zoom(d: int) -> str:
        return (f"zoompan=z='1+0.16*{p}':d={d}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"s={width}x{height}:fps={fps}")

    fade_tail = (f"fade=t=out:st={card_dur - 0.3:.2f}:d=0.3,"
                 f"format=yuv420p")

    def _type_chain(d: int) -> str:
        return ",".join(draws) + f",{_zoom(d)},{fade_tail}"
    if scene1_photo and Path(scene1_photo).exists():
        bg_chain = (
            f"[0:v]scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
            f"crop={big_w}:{big_h},eq=brightness=-0.22:saturation=1.05,setsar=1"
        )
        # FIX-093b: this chain was COMPUTED BUT NEVER PASSED to ffmpeg on the
        # photo/video paths (only the lavfi color path consumed it) — the
        # "card" shipped as the raw photo: no darkening, no headline text,
        # no zoom. Wire it through filter_complex and map its output.
        filter_str = bg_chain + "," + _type_chain(total_frames) + "[vout]"
        args = [
            "-i", str(scene1_photo),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex", filter_str,
            "-map", "[vout]", "-map", "1:a",
            "-t", f"{card_dur}",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-shortest", str(card_path),
        ]
    elif scene1_video and Path(scene1_video).exists():
        bg_chain = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},eq=brightness=-0.22:saturation=1.05,"
            f"fps={fps},setsar=1,trim=duration={card_dur},setpts=PTS-STARTPTS"
        )
        filter_str = bg_chain + "," + _type_chain(1) + "[vout]"
        args = [
            "-t", "2.5", "-i", str(scene1_video),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex", filter_str,
            "-map", "[vout]", "-map", "1:a",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-shortest", str(card_path),
        ]
    else:
        filter_str = (
            f"color=c=0x101110:s={big_w}x{big_h}:d={card_dur}:r={fps},"
            + _type_chain(1)
        )
        args = [
            "-f", "lavfi", "-i", filter_str,
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-shortest", str(card_path),
        ]
    # FIX-093: all three paths now produce exactly card_dur seconds at fps —
    # photo: one image + zoompan d=total_frames; video: input trimmed to
    # card_dur with d=1 (its own motion carries); color: lavfi source with
    # d=1. The photo path's zoom is real motion now (was a still loop).
    run_ffmpeg(args, "Kinetic hook card")
    return card_path if card_path.exists() else None


def _build_music_bed(act1: Path, act2: Path | None, switch_s: float,
                     total_s: float, temp_dir: Path) -> Path | None:
    """Two-act music structure (FIX-043.4).

    Act 1 plays to the midpoint switch, fades 1.5s; act 2 (contrasting mood,
    from the script's own palette) fades in 1.5s and carries the tail with
    a 2.5s fade-out baked in. One ffmpeg pass, butt-jointed via concat —
    the crossfades make the joint inaudible without overlap math.
    """
    if not act1.exists() or total_s < 75 or switch_s < 20:
        return None

    tail_fade = 2.5
    act2_len = max(total_s - switch_s + 0.2, 4.0)
    parts = [
        f"[0:a]atrim=0:{switch_s:.2f},asetpts=PTS-STARTPTS,"
        f"aformat=sample_rates=44100:channel_layouts=stereo,"
        f"afade=t=out:st={max(switch_s - 1.5, 0):.2f}:d=1.5[a1]"
    ]
    if act2 and act2.exists():
        parts.append(
            f"[1:a]atrim=0:{act2_len:.2f},asetpts=PTS-STARTPTS,"
            f"aformat=sample_rates=44100:channel_layouts=stereo,"
            f"afade=t=in:st=0:d=1.5,"
            f"afade=t=out:st={max(act2_len - tail_fade, 0):.2f}:d={tail_fade}[a2]"
        )
    else:
        # No act-2 track available: act 1 carries the tail (still gets the
        # baked fade-out, so the caller can skip its own).
        parts[0] = parts[0].replace(
            f"afade=t=out:st={max(switch_s - 1.5, 0):.2f}:d=1.5",
            f"afade=t=out:st={max(total_s - tail_fade, 0):.2f}:d={tail_fade}",
        )
        parts[0] += f",atrim=0:{total_s:.2f},asetpts=PTS-STARTPTS"

    bed = temp_dir / "music_bed.aac"
    n_parts = 2 if (act2 and act2.exists()) else 1
    filter_str = ";".join(parts) + f";[a1][{'a2' if n_parts == 2 else 'x'}]"
    if n_parts == 2:
        filter_str += "concat=n=2:v=0:a=1[bed]"
    else:
        filter_str = ";".join(parts) + ";[a1]anull[bed]"

    args = ["-stream_loop", "-1", "-i", str(act1)]
    if n_parts == 2:
        args += ["-stream_loop", "-1", "-i", str(act2)]
    args += ["-filter_complex", filter_str, "-map", "[bed]",
             "-c:a", "aac", "-b:a", "192k", str(bed)]
    run_ffmpeg(args, f"Two-act music bed (switch @ {switch_s:.0f}s)")
    return bed if bed.exists() else None


def _prepare_scene_clip(
    scene, template: dict, output_dir: Path, idx: int, motion: str = "zoom_in"
) -> Path | None:
    """
    Prepare a single scene's video clip — either trim stock footage
    or apply Ken Burns effect to a photo.
    """
    visuals_cfg = template.get("visuals", {})
    width = int(visuals_cfg.get("resolution", "1920x1080").split("x")[0])
    height = int(visuals_cfg.get("resolution", "1920x1080").split("x")[1])
    zoom_range = visuals_cfg.get("ken_burns_zoom_range", [1.0, 1.12])

    duration = scene.audio_duration_seconds or scene.duration_target_seconds
    prepared_path = output_dir / f"scene_{idx:02d}_prepared.{PREPARED_V}.mp4"

    if prepared_path.exists():
        return prepared_path

    # Determine source: video or photo
    source_video = Path(scene.video_path) if scene.video_path else None
    source_photo = Path(scene.photo_path) if scene.photo_path else None

    # Apply Fair Use transformations for scraped content
    is_scraped = scene.visual_type in ("youtube_clip", "web_photo", "photo_person")

    if source_video and source_video.exists():
        # Scale/crop stock footage to target resolution and trim to duration
        scale_and_crop(source_video, prepared_path, width, height, duration,
                       apply_fair_use=is_scraped, seed=idx)
        return prepared_path
    elif source_photo and source_photo.exists():
        # Convert photo to video with Ken Burns effect
        photo_to_video(
            source_photo, prepared_path, duration,
            width, height,
            zoom_start=zoom_range[0], zoom_end=zoom_range[1],
            apply_fair_use=is_scraped,
            seed=idx,
            motion=motion,
        )
        return prepared_path
    else:
        log.warning(f"Scene {idx}: no video or photo found, creating black frame")
        # Create a black video for the duration
        args = [
            "-f", "lavfi",
            "-i", f"color=c=black:s={width}x{height}:d={duration}:r=30",
            "-c:v", "libx264", "-crf", "18",
            "-pix_fmt", "yuv420p",
            str(prepared_path),
        ]
        run_ffmpeg(args, f"Black frame for scene {idx}")
        return prepared_path


# Motion palette for multi-cut scenes: every cut in a scene gets a different
# camera move, so no two adjacent sub-cuts look alike.
_CUT_MOTIONS = ("zoom_in", "zoom_out", "pan_left", "pan_right")


# ──────────────────────────────────────────────
# Parallel scene preparation (Phase 3.10 / FIX-026)
# ──────────────────────────────────────────────
# Punch-cuts tripled the per-scene FFmpeg work. Scenes are independent
# (isolated ffmpeg runs, separate outputs), so they prepare in a process
# pool. Worker function must be module-level for pickling.

def _prepare_scene_entry(args: tuple) -> tuple:
    """Worker: prepare one scene's concat-ready clip. Returns (idx, path|None, cuts_used)."""
    (
        scene_dict, template, temp_dir_str, idx, motion,
        parallel_cuts,
    ) = args

    # Reconstruct the minimal Scene surface the helpers need. Scene is a
    # plain dataclass, so from_dict round-trips everything they touch.
    from models import Scene
    from dataclasses import fields as dc_fields
    valid = {f.name for f in dc_fields(Scene)}
    scene = Scene(**{k: v for k, v in scene_dict.items() if k in valid})

    template = dict(template)
    temp_dir = Path(temp_dir_str)

    try:
        cuts = _prepare_photo_cuts(scene, template, temp_dir, idx)
        if cuts is None:
            cuts = _prepare_video_cuts(scene, template, temp_dir, idx)

        if cuts:
            concat_list = temp_dir / f"scene_{idx:02d}_cuts.txt"
            with open(concat_list, "w", encoding="utf-8") as f:
                for cut_path, _, _ in cuts:
                    f.write(
                        f"file '{str(cut_path.resolve()).replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n"
                    )
            prepared_path = temp_dir / f"scene_{idx:02d}_prepared.{PREPARED_V}.mp4"
            if not prepared_path.exists():
                run_ffmpeg(
                    [
                        "-f", "concat", "-safe", "0",
                        "-i", str(concat_list),
                        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                        "-pix_fmt", "yuv420p",
                        str(prepared_path),
                    ],
                    f"Concat punch-cuts for scene {idx}",
                )
            # (start, len) per sub-cut — consumed by the Resolve FCPXML export
            cuts_meta = [(round(cs, 3), round(cl, 3)) for (_, cs, cl) in cuts]
            return idx, str(prepared_path), True, cuts_meta

        clip_path = _prepare_scene_clip(scene, template, temp_dir, idx, motion=motion)
        return idx, (str(clip_path) if clip_path else None), False, []
    except Exception as e:
        log.warning(f"Scene {idx} preparation failed in worker: {e}")
        return idx, None, False, []


def _prepare_all_scenes_parallel(
    script, template: dict, temp_dir: Path,
) -> dict[int, tuple[Path, bool]]:
    """Prepare every scene in a process pool; graceful fallback to serial.

    Returns {scene_number: (prepared_clip_path, was_punch_cut)}. Falls back
    to the original serial loop when pool startup fails (e.g. restricted
    environments), so assembly never breaks because of parallelism.
    """
    scene_fields = (
        "scene_number", "narration", "visual_prompt", "alternative_visual_prompts",
        "visual_type", "mood", "people_to_show", "duration_target_seconds",
        "sfx_cue", "broll_keywords", "audio_path", "audio_duration_seconds",
        "video_path", "photo_path", "timestamps", "source_url", "source_title",
        "source_channel",
    )
    temp_dir_str = str(temp_dir)
    jobs = []
    for scene in script.scenes:
        idx = scene.scene_number
        motion = random.Random(f"motion:{idx}").choice(_CUT_MOTIONS)
        jobs.append((
            {k: getattr(scene, k) for k in scene_fields},
            dict(template), temp_dir_str, idx, motion,
            True,
        ))

    results: dict[int, tuple[Path, bool, list[tuple[float, float]]]] = {}
    workers = max(1, min(4, int(math.floor(math.log2(os.cpu_count() or 2)))))
    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for idx, path_str, cuts_used, cuts_meta in pool.map(_prepare_scene_entry, jobs):
                if path_str:
                    results[idx] = (Path(path_str), cuts_used, cuts_meta)
    except Exception as e:
        log.warning(f"[yellow]Parallel scene prep unavailable ({e}); running serial[/yellow]")
        results = {}
        for job in jobs:
            idx, path_str, cuts_used, cuts_meta = _prepare_scene_entry(job)
            if path_str:
                results[idx] = (Path(path_str), cuts_used, cuts_meta)
    return results


def _sentence_groups(stamps: list[dict], n_cuts: int) -> list[list[dict]]:
    """Partition word timestamps into n_cuts sentence-aligned groups.

    Sentences are merged largest-first into the currently smallest group
    (greedy balance), so sub-cuts get roughly equal narration time without
    ever splitting a sentence mid-way.
    """
    sentences: list[list[dict]] = []
    current: list[dict] = []
    for w in stamps:
        current.append(w)
        if w["word"].rstrip().endswith((".", "!", "?")):
            sentences.append(current)
            current = []
    if current:
        if len(current) <= 2 and sentences:
            sentences[-1].extend(current)  # fold tiny orphan tail into last sentence
        else:
            sentences.append(current)

    groups: list[list[list[dict]]] = [[] for _ in range(n_cuts)]
    sizes = [0] * n_cuts
    for sent in sorted(sentences, key=len, reverse=True):
        i = sizes.index(min(sizes))
        groups[i].append(sent)
        sizes[i] += len(sent)

    # Restore chronological sentence order inside each group
    out = []
    for g in groups:
        if g:
            out.append([w for sent in sorted(g, key=lambda s: s[0]["start"]) for w in sent])
    out.sort(key=lambda ws: ws[0]["start"])
    return out


def _prepare_photo_cuts(
    scene, template: dict, output_dir: Path, idx: int
) -> list[tuple[Path, float, float]] | None:
    """
    Punch-cut editing (A23): slice a photo scene into 2-3 sub-cuts at
    sentence boundaries, each with its own camera move and crop framing.

    This is the single biggest anti-slideshow lever: an 8-12s photo scene
    becomes 2-3 distinct shots (wide -> tight -> pan) exactly where the
    narration changes sentence, mimicking how a human editor cuts.

    Returns [(clip_path, seg_start, seg_len), ...] or None when the scene
    doesn't qualify (video asset, short scene, missing timestamps).
    """
    visuals_cfg = template.get("visuals", {})
    if not visuals_cfg.get("multi_cut_photos", True):
        return None

    source_photo = Path(scene.photo_path) if scene.photo_path else None
    if not (source_photo and source_photo.exists()):
        return None
    if scene.visual_type not in ("web_photo", "photo_person", "stock_photo"):
        return None

    return _build_scene_cuts(
        scene, template, output_dir, idx,
        source_kind="photo",
    )


def _compute_cut_points(
    stamps: list[dict], duration: float,
    taste: dict | None = None,
) -> tuple[list[float], int] | None:
    """Shared cut-point math for photo and video punch-cuts.

    `taste` (FIX-045) shapes cutting rhythm per scene mood: cut_sec sets
    scene-seconds per cut, max_cuts caps density, min_shot merges an
    over-short trailing shot. Defaults reproduce the old uniform rhythm.

    Returns (cuts, n_cuts) where cuts are scene-local seconds, or None when
    the scene is too short / has too few sentences to cut.
    """
    if duration < 6.0 or len(stamps) < 8:
        return None

    # Need at least 2 sentences to have somewhere to cut
    probe = _sentence_groups(stamps, 2)
    if len(probe) < 2:
        return None

    t = taste or {}
    cut_sec = float(t.get("cut_sec", 4.0))
    max_cuts = int(t.get("max_cuts", 3))
    min_shot = float(t.get("min_shot", 1.5))

    n_cuts = int(max(2, min(max_cuts, duration // cut_sec)))
    groups = _sentence_groups(stamps, n_cuts)
    if len(groups) < 2:
        return None
    n_cuts = len(groups)

    # Cut points at the temporal midpoint between adjacent groups' words —
    # never clips the tail of one word or the head of the next.
    starts = [g[0]["start"] for g in groups]
    ends = [g[-1]["end"] for g in groups]
    cuts = [0.0]
    for i in range(1, n_cuts):
        cuts.append(round(min(max((ends[i - 1] + starts[i]) / 2.0, cuts[-1]), duration), 3))
    cuts.append(round(duration, 3))
    # Restraint check (FIX-045): never leave a sub-shot shorter than
    # min_shot — a human editor merges it instead of flashing a frame.
    while len(cuts) > 2 and (cuts[-1] - cuts[-2]) < min_shot:
        del cuts[-2]
    return cuts, len(cuts) - 1


def _build_scene_cuts(
    scene, template: dict, output_dir: Path, idx: int,
    source_kind: str,
) -> list[tuple[Path, float, float]] | None:
    """Shared punch-cut builder for photo and video source scenes.

    source_kind "photo" renders each sub-cut as an independent Ken Burns
    shot from the still; source_kind "video" re-encodes consecutive
    [start, len) slices of the source clip with per-cut fair-use framing
    (Phase 1.1: youtube_clip / stock_video scenes get cuts too).
    """
    visuals_cfg = template.get("visuals", {})
    duration = scene.audio_duration_seconds or scene.duration_target_seconds or 5.0
    stamps = scene.timestamps or []

    taste = _edit_taste(scene, idx)  # FIX-045: mood-driven intensity
    computed = _compute_cut_points(stamps, duration, taste=taste)
    if computed is None:
        return None
    cuts, n_cuts = computed

    width = int(visuals_cfg.get("resolution", "1920x1080").split("x")[0])
    height = int(visuals_cfg.get("resolution", "1920x1080").split("x")[1])
    zoom_lo, zoom_hi = visuals_cfg.get("ken_burns_zoom_range", [1.0, 1.12])
    # zoom_speed scales the push distance, not the floor (keeps ≥1)
    zoom_range = [zoom_lo, zoom_lo + (zoom_hi - zoom_lo) * taste["zoom_speed"]]
    is_scraped = scene.visual_type in ("web_photo", "photo_person", "youtube_clip")
    motion_order = random.Random(f"cutmotions:{idx}").sample(list(_CUT_MOTIONS), len(_CUT_MOTIONS))

    source_photo = Path(scene.photo_path) if scene.photo_path else None
    source_video = Path(scene.video_path) if scene.video_path else None

    # FIX-051: paired cutaway. A photo scene with a b-roll clip (public-domain
    # archive footage or stock video) alternates MAIN SHOT (Ken Burns still)
    # and CUTAWAY (real footage) across its sub-cuts — the multi-source
    # rhythm a human editor cuts with. Even cuts keep the still, odd cuts
    # take the footage.
    cutaway_video: Path | None = None
    if source_kind == "photo":
        broll = getattr(scene, "broll_video_path", None)
        if broll:
            bp = Path(broll)
            if bp.exists():
                try:
                    if get_duration(bp) >= 2.0:
                        cutaway_video = bp
                except Exception:
                    cutaway_video = None

    def _render_cut(cut_path: Path, k: int, seg_len: float) -> bool:
        """Render one sub-cut file. Returns True on success."""
        if source_kind == "photo" and source_photo and source_photo.exists():
            # FIX-051: odd cuts use the paired footage cutaway when one is
            # available — even cuts stay on the still.
            if cutaway_video is not None and k % 2 == 1:
                cw_probe = get_duration(cutaway_video)
                cw_max = max(0.0, cw_probe - seg_len)
                cw_start = random.Random(f"cutsrc:{idx}:{k}").uniform(0.0, cw_max) if cw_max > 0.05 else 0.0
                cut_video_segment(
                    cutaway_video, cut_path, cw_start, seg_len,
                    width, height,
                    apply_fair_use=False,
                    seed=idx * 10 + k,
                )
                return True
            photo_to_video(
                source_photo, cut_path, seg_len,
                width, height,
                # Adjacent stills cuts must be READABLE as different shots:
                # cut 0 wide, cut 2 tight, alternating zoom direction and a
                # different motion per cut (the subtle ~5% drift made two
                # cuts of one photo read as the same frame on a phone).
                zoom_start=zoom_range[0] + (0.14 if k >= 2 else 0.0),
                zoom_end=min(
                    zoom_range[1] + 0.16,
                    zoom_range[0] + (0.14 if k >= 2 else 0.0)
                    + (0.13 if k % 2 == 0 else 0.03),
                ),
                apply_fair_use=is_scraped,
                seed=idx * 10 + k,          # different crop framing per cut
                motion=(motion_order[k % len(motion_order)]
                        if k < 2 else
                        _CUT_MOTIONS[(k + 2) % len(_CUT_MOTIONS)]),
            )
            return True
        if source_kind == "video" and source_video and source_video.exists():
            # Vary WHERE inside the source clip each cut samples so adjacent
            # sub-cuts don't reuse the same footage when the source is short
            # stock; scraped clips also get a fresh fair-use transform per cut.
            src_probe = get_duration(source_video)
            max_off = max(0.0, src_probe - seg_len)
            src_start = random.Random(f"cutsrc:{idx}:{k}").uniform(0.0, max_off) if max_off > 0.05 else 0.0
            cut_video_segment(
                source_video, cut_path, src_start, seg_len,
                width, height,
                apply_fair_use=is_scraped,
                seed=idx * 10 + k,
            )
            return True
        return False

    out: list[tuple[Path, float, float]] = []
    for k in range(n_cuts):
        seg_start, seg_end = cuts[k], cuts[k + 1]
        seg_len = round(seg_end - seg_start, 3)
        if seg_len < 1.2:  # too short to read as a shot; fold into neighbor
            if out:
                prev_path, prev_start, prev_len = out[-1]
                out[-1] = (prev_path, prev_start, round(prev_len + seg_len, 3))
                continue
            # (first segment can only be short if cuts degenerated; keep it)
        cut_path = output_dir / f"scene_{idx:02d}_cut{k}.{PREPARED_V}.mp4"
        if not cut_path.exists():
            if not _render_cut(cut_path, k, seg_len):
                return None
        out.append((cut_path, seg_start, seg_len))

    if len(out) < 2:
        return None

    # Last cut absorbs any leftover so the concat length == narration length
    # exactly (P4 spec). Folds and rounding would otherwise leave the video
    # shorter than the audio, and the assembler's -shortest would clip the
    # final words of the scene.
    rendered_total = round(sum(seg_len for _, _, seg_len in out), 3)
    leftover = round(duration - rendered_total, 3)
    if leftover > 0.05:
        last_path, last_start, last_len = out[-1]
        last_path.unlink(missing_ok=True)
        if not _render_cut(last_path, n_cuts - 1, round(last_len + leftover, 3)):
            return None
        out[-1] = (last_path, last_start, round(last_len + leftover, 3))

    log.info(f"  [cyan]Punch-cut[/cyan] scene {idx}: {len(out)} sub-cuts at sentence boundaries ({source_kind})")
    return out


def _prepare_video_cuts(
    scene, template: dict, output_dir: Path, idx: int
) -> list[tuple[Path, float, float]] | None:
    """Punch-cut a VIDEO scene (youtube_clip / stock_video) into sub-cuts.

    Phase 1.1: extends FIX-011 beyond photos. The narration-aligned cut
    points are shared with the photo path via _build_scene_cuts; each
    sub-cut re-encodes a different slice of the source clip so the visual
    changes with the sentence even when only one video asset exists.
    youtube_clip sources re-encode with fair-use transforms per cut.
    """
    visuals_cfg = template.get("visuals", {})
    if not visuals_cfg.get("multi_cut_photos", True):
        return None
    source_video = Path(scene.video_path) if scene.video_path else None
    if not (source_video and source_video.exists()):
        return None
    if scene.visual_type not in ("youtube_clip", "stock_video"):
        return None
    if (scene.photo_path and Path(scene.photo_path).exists()):
        return None  # photo takes priority (handled by the photo path)

    try:
        src_dur = get_duration(source_video)
    except Exception:
        return None
    duration = scene.audio_duration_seconds or scene.duration_target_seconds or 5.0
    # A source that can't cover one sub-cut (>= ~2s) can't produce varied
    # slices; fall through to the loop-clip path instead.
    if src_dur < 2.0:
        return None

    return _build_scene_cuts(
        scene, template, output_dir, idx,
        source_kind="video",
    )


# FIX-068: bounded-pass assembly threshold. One filter_complex keeps every
# input's decoder, scaler and frame buffers alive for the whole render, so
# memory grows linearly with clip count — 44 simultaneous inputs OOM'd
# ("Cannot allocate memory") on the 44-scene celebrity_8min doc. Chains
# longer than this render in bounded passes joined by stream-copy concat.
# FIX-071: 16 still OOM'd the GitHub runner (7 GB total, ~4.5 GB after the
# torch-whisper import) — pass 2 of the Rundgren doc died silently and the
# runner was SIGTERM'd. xfade graphs hold ~200-300 MB per 1080p input, so
# 8 inputs keeps pass renders comfortably under the ceiling.
_MAX_FILTER_INPUTS = 8


def _narration_must_have_audio(scene) -> bool:
    """FIX-094: true when the scene's narration was supposed to become audio.

    A narrated scene arriving at the assembler with scene.audio_path unset is
    the silent-tail defect; only genuinely narration-free visual beats may be
    padded with anullsrc. Kept as a one-line predicate so the rule is unit
    testable without rendering a video.
    """
    return bool((getattr(scene, "narration", "") or "").strip())


def _assemble_with_transitions(
    clips: list[Path],
    moods: list[str | None],
    template: dict,
    output_path: Path,
    open_card: Path | None = None,
    leads: list[float] | None = None,
    hard_cuts: list[str] | None = None,
    cut_index_offset: int = 0,
) -> tuple[Path, list[float]]:
    """
    Concatenate clips with mood-aware xfade transitions in a single FFmpeg pass.

    Video: chained xfade — each cut overlaps the previous clip's tail, so the
    total timeline shrinks by the transition duration at every cut.
    Audio: narration is never cut — each clip's audio stream is delayed to the
    clip's post-transition start time and mixed with amix.

    Returns (output_path, input_starts) where input_starts[i] is the timeline
    position of clips[i]'s audio — slice by structure (hook/scenes/outro) for
    subtitle or chapter offsets.
    """
    # FIX-068: long chains delegate to bounded-pass rendering (see
    # _MAX_FILTER_INPUTS) — short chains keep the exact single-pass behavior.
    if len(clips) > _MAX_FILTER_INPUTS:
        return _assemble_chunked(
            clips, moods, template, output_path,
            open_card=open_card, leads=leads, hard_cuts=hard_cuts,
        )
    visuals_cfg = template.get("visuals", {})
    trans_cfg = visuals_cfg.get("transitions", {})
    width = int(visuals_cfg.get("resolution", "1920x1080").split("x")[0])
    height = int(visuals_cfg.get("resolution", "1920x1080").split("x")[1])
    base_t = float(trans_cfg.get("duration", 0.4))

    durations = [get_duration(p) for p in clips]
    leads = leads or [0.0] * max(len(clips) - 1, 0)
    hard_cuts = hard_cuts or []

    # Per-cut transition duration: never longer than 40% of either neighbor,
    # so short clips (e.g. the 3s hook) can't have overlapping transitions.
    trans_durs = []
    for j in range(len(clips) - 1):
        t = min(base_t, durations[j] * 0.4, durations[j + 1] * 0.4)
        trans_durs.append(max(round(t, 2), 0.0))

    # xfade chain offsets: cut j starts (into the accumulated timeline) when
    # clip j has  T_j  seconds remaining.
    offsets = []
    for j in range(len(clips) - 1):
        prev = offsets[j - 1] if j > 0 else 0.0
        offsets.append(prev + durations[j] - trans_durs[j])

    inputs: list[str] = []
    fc: list[str] = []

    for i, clip in enumerate(clips):
        inputs += ["-i", str(clip)]
        # Normalize every stream so xfade inputs match in fps/timebase/format.
        fc.append(
            f"[{i}:v]fps=30,settb=AVTB,scale={width}:{height}:"
            f"force_original_aspect_ratio=increase,crop={width}:{height},"
            f"setsar=1,format=yuv420p[v{i}]"
        )
        # FIX-059 J-cuts: scene i's narration may start PRE_LAP_S before its
        # video (audio leads the picture) at motivated hard cuts. The clip
        # chain is contiguous, so every later scene's audio start shifts by
        # the accumulated lead amounts — pre_lap starts[0] = 0.0.
        lead = leads[i - 1] if i > 0 else 0.0
        delay_ms = int(round(max(0.0, (offsets[i - 1] - lead) * 1000))) if i > 0 else 0
        fc.append(
            f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"adelay=delays={delay_ms}ms:all=1[a{i}]"
        )

    # FIX-052: kinetic hook card OVERLAYS scene 1 instead of prepending to
    # the concat. The old prepend put ~1.8s of dead air before the first
    # word — a slideshow-open mistake. Now the narration starts at t≈0 and
    # the title card plays ON TOP of the opening line, popping off as the
    # sentence lands. (open_card carries no audio into the mix.)
    start_label = "v0"
    if open_card is not None and open_card.exists():
        card_dur = get_duration(open_card)
        card_idx = len(clips)
        inputs += ["-i", str(open_card)]
        fc.append(
            f"[{card_idx}:v]fps=30,settb=AVTB,scale={width}:{height}:"
            f"force_original_aspect_ratio=increase,crop={width}:{height},"
            f"setsar=1,format=yuv420p[cardv]"
        )
        fc.append(
            f"[v0][cardv]overlay=0:0:eof_action=pass:"
            f"enable='lte(t,{card_dur:.2f})'[v0o]"
        )
        start_label = "v0o"

    # Chain the video transitions — hard cut is the baseline (FIX-043.1);
    # mood accents (fadeblack/smoothright/dissolve/circleopen) punctuate.
    prev_label = start_label
    for j in range(len(clips) - 1):
        out_label = f"x{j}"
        t = trans_durs[j]
        style = hard_cuts[j] if j < len(hard_cuts) else None
        transition, mult = _transition_for_cut(moods[j], trans_cfg, j, index_offset=cut_index_offset)
        if style in ("prelap_hard_cut", "hard_cut") or t <= 0 or transition == "none":
            # FIX-059: motivated hard cut — the confident straight cut a
            # human editor reaches for. "prelap_hard_cut" additionally lets
            # the incoming audio start early (handled by the delays above).
            fc.append(f"[{prev_label}][v{j + 1}]concat=n=2:v=1:a=0[{out_label}]")
        else:
            fc.append(
                f"[{prev_label}][v{j + 1}]xfade=transition={transition}:"
                f"duration={t * mult:.2f}:offset={offsets[j]:.3f}[{out_label}]"
            )
        prev_label = out_label

    n = len(clips)
    fc.append(
        f"{''.join(f'[a{i}]' for i in range(n))}amix=inputs={n}:"
        f"duration=longest:normalize=0[aout]"
    )

    args = inputs + [
        "-filter_complex", ";".join(fc),
        "-map", f"[{prev_label}]",
        "-map", "[aout]",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        str(output_path),
    ]
    try:
        run_ffmpeg(args, f"Assemble {n} clips with xfade transitions")
    except RuntimeError:
        if n <= 8:
            raise
        # FIX-068 fallback: even mid-size graphs can OOM on a busy machine —
        # re-render the same edit plan in bounded passes.
        log.warning(
            f"Single-pass assembly of {n} clips failed; retrying in bounded "
            f"passes of ≤{_MAX_FILTER_INPUTS} inputs (FIX-068)"
        )
        return _assemble_chunked(
            clips, moods, template, output_path,
            open_card=open_card, leads=leads, hard_cuts=hard_cuts,
        )

    # Every input's audio start on the final timeline (input 0 = 0.0).
    input_starts = [0.0] + list(offsets)
    return output_path, input_starts


def _assemble_chunked(
    clips: list[Path],
    moods: list[str | None],
    template: dict,
    output_path: Path,
    open_card: Path | None = None,
    leads: list[float] | None = None,
    hard_cuts: list[str] | None = None,
) -> tuple[Path, list[float]]:
    """
    Bounded-pass assembly for long xfade chains (FIX-068).

    A filter_complex input holds its decoder + frame buffers open for the
    whole render, so a 40+-input graph exhausts RAM. This renders the chain
    in passes of ≤ _MAX_FILTER_INPUTS clips — each pass delegates to
    _assemble_with_transitions (single-pass at that size) — then joins the
    pass files with stream-copy concat. A pass boundary becomes a true hard
    cut: the outgoing pass ends with its last clip fully shown, so the next
    clip starts exactly there. `cut_index_offset` keeps accent rhythm and
    act punctuation on GLOBAL cut indices, so the edit plan matches the
    hypothetical single pass.

    Returns (output_path, input_starts), same semantics as
    _assemble_with_transitions.
    """
    n = len(clips)
    durations = [get_duration(p) for p in clips]
    leads = leads or [0.0] * max(n - 1, 0)
    hard_cuts = hard_cuts or []

    max_cuts = max(1, _MAX_FILTER_INPUTS - 2)
    breaks = set(range(max_cuts, n - 1, max_cuts))
    pass_specs: list[tuple[int, int]] = []
    lo = 0
    for j in range(n - 1):
        if j in breaks:
            pass_specs.append((lo, j))
            lo = j + 1
    pass_specs.append((lo, n - 1))

    log.info(
        f"Assembly in {len(pass_specs)} bounded passes "
        f"(≤{max_cuts} cuts each — FIX-068 memory guard)"
    )

    starts = [0.0] * n
    pass_paths: list[Path] = []
    for pi, (lo, hi) in enumerate(pass_specs):
        pass_out = output_path.with_name(f"{output_path.stem}_pass{pi:02d}.mp4")
        _, local_starts = _assemble_with_transitions(
            clips[lo:hi + 1],
            moods[lo:hi + 1],
            template,
            pass_out,
            open_card=open_card if pi == 0 else None,
            leads=leads[lo:hi],
            hard_cuts=hard_cuts[lo:hi],
            cut_index_offset=lo,
        )
        base = starts[lo]
        for m, ls in enumerate(local_starts):
            starts[lo + m] = base + ls
        if hi < n - 1:
            # Stream-copy boundary: the next pass starts exactly when this
            # pass's last clip ends (no transition overlap across passes).
            starts[hi + 1] = base + local_starts[-1] + durations[hi]
        pass_paths.append(pass_out)

    concat_videos(pass_paths, output_path)
    for p in pass_paths:
        p.unlink(missing_ok=True)
    return output_path, starts


# SFX cue → filename stem prefix. A "pool" is every assets/sfx/<stem>*.wav
# (e.g. whoosh_01.wav, whoosh_02.wav); one is drawn deterministically per
# placement so consecutive cuts don't reuse the identical sample (A25).
SFX_CUE_STEMS = {
    "whoosh": "whoosh",
    "sub_impact": "sub_impact",
    "camera_shutter": "camera_shutter",
    "tension_riser": "tension_riser",
}
SFX_VOLUME = 0.35  # SFX bed level pre-ducking; loud enough to feel, quiet
                   # enough that narration always owns the mix


def _pick_sfx_file(cue: str, pick_seed: int) -> Path | None:
    """Pick one file from a cue's pool (assets/sfx/<stem>*.wav)."""
    sfx_dir = Path(__file__).parent.parent / "assets" / "sfx"
    stem = SFX_CUE_STEMS.get(cue)
    if not stem:
        return None
    pool = sorted(sfx_dir.glob(f"{stem}*.wav"))
    if not pool:
        return None
    return pool[pick_seed % len(pool)]


def _build_sfx_track(
    script: Script,
    scene_offsets: list[float],
    total_duration: float,
    temp_dir: Path,
) -> tuple[Path | None, list[dict]]:
    """
    Constructs a synchronized SFX audio track with edit-suite discipline
    (A25 / Phase 1.2):
      - cue pools: a different sample per placement (no single-loop feel)
      - volume 0.35, then sidechain-ducked under narration in the mix
      - riser lands 1.5s before the midpoint scene (tension into the turn)
      - impact hits only on scenes explicitly cued `sub_impact`
      - camera shutter only on photo scenes cued for it

    Also returns every placement (cue/file/timeline offset) so the DaVinci
    Resolve FCPXML export can rebuild the same SFX as individual clips.
    """
    placements: list[dict] = []
    cues_to_mix: list[tuple[Path, float]] = []
    scenes = script.scenes
    midpoint_idx = len(scenes) // 2

    # FIX-058 news restraint: with transition_sfx "restrained", cut whooshes
    # are capped at ~2 per video (structural hits and shutters unaffected) —
    # "no random whooshes on every cut".
    audio_cfg = {}
    try:
        audio_cfg = script.template_config.get("audio", {}) if getattr(script, "template_config", None) else {}
    except Exception:
        audio_cfg = {}
    restrained = str(audio_cfg.get("transition_sfx", "")) == "restrained"
    whoosh_budget = 2 if restrained else None
    whoosh_used = 0

    for idx, scene in enumerate(scenes):
        cue = getattr(scene, "sfx_cue", None) or "whoosh"
        if cue == "none":
            continue
        offset = scene_offsets[idx] if idx < len(scene_offsets) else 0.0

        # FIX-045 restraint: calm scenes (emotional/dark) get NO cut accents —
        # a human editor lets the moment breathe. Script-cued structural
        # hits (sub_impact, tension_riser) and photo shutters still land.
        if cue == "whoosh" and not _edit_taste(scene, idx)["sfx"]:
            continue
        if cue == "whoosh" and restrained and whoosh_used >= (whoosh_budget or 0):
            continue

        if cue == "whoosh":
            # Advance slightly before the cut for anticipation.
            if offset > 0.2:
                offset = max(0.0, offset - 0.12)
            f = _pick_sfx_file("whoosh", pick_seed=idx)
            whoosh_used += 1
        elif cue == "camera_shutter":
            # Shutter reads as "evidence" only on actual photos.
            if not scene.photo_path:
                continue
            f = _pick_sfx_file("camera_shutter", pick_seed=idx)
        elif cue == "sub_impact":
            f = _pick_sfx_file("sub_impact", pick_seed=idx)
        elif cue == "tension_riser":
            f = _pick_sfx_file("tension_riser", pick_seed=idx)
        else:
            continue

        if f and f.exists():
            cues_to_mix.append((f, offset))
            placements.append({
                "cue": cue,
                "file": str(f),
                "offset_seconds": round(offset, 3),
            })

    # One riser 1.5s ahead of the midpoint scene, regardless of per-scene cue
    # (the midpoint is where attention sags — this is the tension-into-turn
    # lift). Skip if the scene already carries a riser at that spot, or when
    # the midpoint sits at the very start of the video (nothing to build
    # tension out of — e.g. single-scene scripts).
    riser = _pick_sfx_file("tension_riser", pick_seed=len(scenes) + 7)
    if riser and riser.exists() and midpoint_idx < len(scene_offsets):
        mid_scene_start = scene_offsets[midpoint_idx]
        if mid_scene_start >= 2.0:
            mid_off = max(0.0, mid_scene_start - 1.5)
            already = any(
                abs(off - mid_scene_start) < 0.3 for _, off in cues_to_mix
            )
            if not already:
                cues_to_mix.append((riser, mid_off))
                placements.append({
                    "cue": "tension_riser",
                    "file": str(riser),
                    "offset_seconds": round(mid_off, 3),
                    "note": "midpoint riser",
                })

    if not cues_to_mix:
        return None, placements

    sfx_out = temp_dir / "sfx_track.wav"
    inputs = []
    filters = []
    for i, (sfx_file, offset) in enumerate(cues_to_mix):
        inputs.extend(["-i", str(sfx_file)])
        delay_ms = int(round(offset * 1000.0))
        filters.append(
            f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={SFX_VOLUME},"
            f"adelay=delays={delay_ms}:all=1[sfx{i}]"
        )

    n = len(cues_to_mix)
    concat_labels = "".join(f"[sfx{i}]" for i in range(n))
    filters.append(f"{concat_labels}amix=inputs={n}:duration=longest:normalize=0[sfxout]")

    cmd = inputs + [
        "-filter_complex", ";".join(filters),
        "-map", "[sfxout]",
        "-t", f"{total_duration:.2f}",
        "-c:a", "pcm_s16le",
        "-y", str(sfx_out)
    ]
    try:
        run_ffmpeg(cmd, "Generate synchronized SFX track")
        return (sfx_out if sfx_out.exists() else None), placements
    except Exception as e:
        log.warning(f"Failed to build SFX track: {e}")
        return None, placements


def _caption_bands_for_scenes(prepared: dict, temp_dir: Path) -> dict[int, str]:
    """FIX-062: choose the caption band (bottom/top) per scene from the render.

    Captions are burned over the picture, so placement has to know where the
    subject is. Each prepared clip is sampled once; a face that sits low in the
    frame lifts that scene's captions to the top band. An override file written
    by the QC repair pass wins, so a post-render frame review that reports
    "caption obscures subject face" actually changes the render instead of
    re-rolling the visual (which never touched the caption).
    """
    try:
        from utils.frame_occupancy import caption_band
    except Exception:
        return {}
    overrides: dict[int, str] = {}
    src = Path(temp_dir) / "caption_band_overrides.json"
    if src.exists():
        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
            overrides = {int(k): str(v) for k, v in (raw or {}).items()
                         if str(v) in ("top", "bottom")}
            log.info(f"[dim]FIX-062: caption band overrides from QC: {overrides}[/dim]")
        except Exception:
            overrides = {}
    bands: dict[int, str] = {}
    for idx, entry in (prepared or {}).items():
        clip = entry[0] if isinstance(entry, (tuple, list)) else entry
        if idx in overrides:
            bands[idx] = overrides[idx]
            continue
        if clip is None or not Path(clip).exists():
            continue
        try:
            bands[idx] = caption_band(Path(clip), at=0.6)
        except Exception:
            continue
    return bands


def assemble_video(
    script: Script,
    template: dict,
    output_dir: Path,
) -> Path:
    """
    Assemble the final video from all scene assets.

    Steps:
    1. Prepare each scene clip (scale/crop or Ken Burns)
    2. Add narration audio to each scene
    3. Concatenate all scenes
    4. Generate and burn captions
    5. Mix with background music (with ducking)
    6. Normalize audio to YouTube standard
    7. Output final MP4

    Args:
        script: Script with all assets populated.
        template: The loaded template config.
        output_dir: Project output directory.

    Returns:
        Path to the final rendered MP4.
    """
    log.info("[bold blue]Starting video assembly (FFmpeg mode)[/bold blue]")

    scenes_dir = output_dir / "scenes"
    temp_dir = output_dir / "_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    audio_cfg = template.get("audio", {})
    caption_cfg = template.get("captions", {})
    visuals_cfg = template.get("visuals", {})
    width = int(visuals_cfg.get("resolution", "1920x1080").split("x")[0])
    height = int(visuals_cfg.get("resolution", "1920x1080").split("x")[1])

    # ── Step 1 & 2: Prepare scene clips (parallel, FIX-026) ──
    # Scenes are independent ffmpeg runs — they prepare in a process pool
    # (punch-cuts tripled per-scene work; Phase 3.10). Serial fallback keeps
    # assembly working where pools can't spawn.
    prepared = _prepare_all_scenes_parallel(script, template, temp_dir)
    scene_cuts: dict[int, list[tuple[float, float]]] = {}

    scene_clips_with_audio = []
    for scene in script.scenes:
        idx = scene.scene_number

        prepped = prepared.get(idx)
        if prepped and prepped[1]:
            scene_cuts[idx] = prepped[2]
        if prepped is None or not prepped[0].exists():
            log.warning(f"Scene {idx}: preparation failed — creating black frame")
            prepared_path = temp_dir / f"scene_{idx:02d}_prepared.{PREPARED_V}.mp4"
            if not prepared_path.exists():
                run_ffmpeg(
                    [
                        "-f", "lavfi",
                        "-i", f"color=c=black:s={width}x{height}:d={scene.audio_duration_seconds or scene.duration_target_seconds or 5.0}:r=30",
                        "-c:v", "libx264", "-crf", "18",
                        "-pix_fmt", "yuv420p",
                        str(prepared_path),
                    ],
                    f"Black frame for failed scene {idx}"
                )
        else:
            prepared_path = prepped[0]

        # Add narration audio (video length == audio length by construction)
        if scene.audio_path and Path(scene.audio_path).exists():
            clip_with_audio = temp_dir / f"scene_{idx:02d}_with_audio.mp4"
            add_audio_to_video(prepared_path, Path(scene.audio_path), clip_with_audio)
            scene_clips_with_audio.append(clip_with_audio)
        elif _narration_must_have_audio(scene):
            # FIX-094: a narrated scene with no audio is the silent-tail
            # defect — the narration simply stops while the picture and
            # captions keep running (measured: 3-5s of -91 dB dead air on
            # 2026-10-04 uploads). The voice stage already refuses to finish
            # in this state; this is the last line of defense.
            raise RuntimeError(
                f"FIX-094: scene {idx} has narration but no usable audio "
                f"(audio_path={getattr(scene, 'audio_path', None)!r}) — "
                f"refusing to pad it with silence. Re-run the voice stage; "
                f"already-generated scene audio is reused from disk.")
        else:
            log.info(f"Scene {idx} is a narration-free visual beat — adding silent audio track.")
            clip_with_audio = temp_dir / f"scene_{idx:02d}_with_silent_audio.mp4"
            run_ffmpeg(
                [
                    "-i", str(prepared_path),
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-ar", "44100", "-ac", "2", "-shortest",
                    str(clip_with_audio)
                ],
                f"Add silent audio to scene {idx}"
            )
            scene_clips_with_audio.append(clip_with_audio)

    if not scene_clips_with_audio:
        raise RuntimeError("No scene clips were prepared — cannot assemble video")

    # ── Step 3: Concatenate all scenes + Hook + Outro ──
    log.info(f"Concatenating {len(scene_clips_with_audio)} scenes...")

    # FIX-059 pre-lap sizing: measure the real trailing silence of each
    # scene's narration (bounded probe) — 0.0 when unusable.
    pre_lap_starts = [
        _audio_leads_by(s.audio_path) for s in script.scenes
    ]

    final_clips_to_concat = []
    hook_sec = 3.0

    # Generate Hook Rewind (First 3 seconds of the first clip, reversed — video only).
    # Disable via visuals.hook_rewind=false (shorts use this — 3s of 59 is too expensive).
    hook_enabled = visuals_cfg.get("hook_rewind", True) and bool(scene_clips_with_audio)
    if hook_enabled:
        hook_path = temp_dir / "hook_rewind.mp4"
        run_ffmpeg(
            [
                "-i", str(scene_clips_with_audio[0]),
                "-t", "3",
                "-vf", "reverse",
                "-an",
                "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                str(hook_path)
            ],
            "Generate Hook Rewind"
        )
        # Add a silent audio stream so concat doesn't choke
        hook_with_audio = temp_dir / "hook_rewind_audio.mp4"
        run_ffmpeg(
            [
                "-i", str(hook_path),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-shortest",
                str(hook_with_audio)
            ],
            "Add silent audio to hook"
        )
        final_clips_to_concat.append(hook_with_audio)

    # FIX-043.3 + FIX-052: kinetic hook card is an OVERLAY on scene 1 — the
    # narration starts at frame 0 underneath it. (The old prepend blocked
    # the timeline with ~1.8s of silence before the first word.)
    hook_card = None
    if visuals_cfg.get("hook_card", True):
        try:
            hook_card = _build_hook_card(script, template, temp_dir)
        except Exception as card_err:
            log.warning(f"Hook card skipped: {card_err}")

    final_clips_to_concat.extend(scene_clips_with_audio)
    
    # Outro: end ON the story — the final scene's narration already closes on
    # an open question. A 3s black "Subscribe for more!" card reads as AI
    # template filler and drops the session right after the payoff. Off by
    # default; visuals.end_card=true restores the old card.
    if visuals_cfg.get("end_card", False):
        outro_path = temp_dir / "outro.mp4"
        run_ffmpeg(
            [
                "-f", "lavfi",
                "-i", f"color=c=black:s={width}x{height}:d=3:r=30",
                "-f", "lavfi", 
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-vf", "drawtext=text='Subscribe for more!':fontcolor=white:fontsize=80:x=(w-text_w)/2:y=(h-text_h)/2",
                "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-shortest",
                str(outro_path)
            ],
            "Generate Outro"
        )
        final_clips_to_concat.append(outro_path)
    
    concat_path = temp_dir / "concat_raw.mp4"
    trans_cfg = visuals_cfg.get("transitions", {})
    # FIX-059: one editorial style per boundary (pre-lap J-cut vs motivated
    # hard cut vs the default xfade plan) + accumulated audio leads.
    build_styles = _cut_styles(script.scenes, pre_lap_starts)
    audio_leads = [0.0]
    for j in range(len(script.scenes) - 1):
        audio_leads.append(
            audio_leads[-1] + (pre_lap_starts[j] if build_styles[j] == "prelap_hard_cut" else 0.0)
        )
    pre_count = (1 if hook_enabled else 0)
    if trans_cfg.get("enabled", True) and len(final_clips_to_concat) > 1:
        # Mood per cut: the outgoing clip's mood picks the transition style
        # (hook, if present, falls back to the default; no outro unless enabled).
        clip_moods = [None] * pre_count + [s.mood for s in script.scenes]
        if visuals_cfg.get("end_card", False):
            clip_moods.append(None)
        # Per-cut hard-cut plan (FIX-059): hard cut is baseline, dissolves
        # are accents, and question→answer / contrast beats get a J-cut
        # pre-lap instead of a transition. The hook (clip 0) always uses the
        # default plan.
        cut_hard = ["xfade"] + build_styles
        concat_path, input_starts = _assemble_with_transitions(
            final_clips_to_concat, clip_moods, template, concat_path,
            open_card=hook_card,
            hard_cuts=cut_hard,
        )
        # Scenes are inputs [pre_count .. pre_count+N-1]; outro is the last input.
        scene_offsets = input_starts[pre_count: pre_count + len(script.scenes)]
    else:
        # Hard-cut fallback: the card can't overlay here, so it prepends
        # (rare path — transitions enabled by default).
        if hook_card:
            final_clips_to_concat.insert(0, hook_card)
        concat_videos(final_clips_to_concat, concat_path)
        # Hard-cut fallback: cumulative start times, hook at the beginning
        scene_offsets = []
        cumulative = hook_sec if hook_enabled else 0.0
        for scene in script.scenes:
            scene_offsets.append(cumulative)
            cumulative += scene.audio_duration_seconds or scene.duration_target_seconds
        # FIX-059: no pre-lap machinery on this rare path — keep captions/SFX
        # aligned with unshifted audio starts.
        audio_leads = [0.0] * len(script.scenes)
    if len(script.scenes) > 1:
        n_pre = sum(1 for s in build_styles if s == "prelap_hard_cut")
        n_hard = sum(1 for s in build_styles if s == "hard_cut")
        log.info(
            f"Edit plan: {n_pre} pre-lap J-cut(s), {n_hard} motivated hard "
            f"cut(s), {len(build_styles) - n_pre - n_hard} xfade accent(s)"
        )

    # ── Step 4: Generate and burn captions ──
    log.info("Generating captions...")
    # scene_offsets now come from the assembler (transition-aware) or fallback

    # Collect all timestamps
    all_timestamps = [scene.timestamps or [] for scene in script.scenes]

    # FIX-059: pre-laps shift audio earlier than the picture — captions are
    # timed to the AUDIO, so subtract each scene's accumulated lead.
    caption_starts = [
        off - (audio_leads[i] if i < len(audio_leads) else 0.0)
        for i, off in enumerate(scene_offsets)
    ]

    ass_path = temp_dir / "captions.ass"
    # FIX-062: face-aware caption band per scene (bottom default; lifted to the
    # top band only where the subject actually sits low in the frame).
    caption_bands = _caption_bands_for_scenes(prepared, temp_dir)
    if caption_bands:
        log.info(f"[dim]FIX-062 caption bands: {caption_bands}[/dim]")
    generate_ass_subtitles(
        all_timestamps, caption_starts, ass_path,
        caption_cfg, resolution=(width, height),
        hook_overlay_text=getattr(script, "hook_overlay_text", None),
        band_overrides=caption_bands,
    )

    # ── Step 4: Burn subtitles and apply cinematic filters (Single Pass) ──
    ass_escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
    vf_filters = []

    # Unified color grade FIRST so captions stay pure white/gold.
    # Knits together Ken Burns JPEGs, stock clips and yt-dlp rips that all
    # have different color temperatures.
    if visuals_cfg.get("color_grade", "cinematic") != "none":
        vf_filters.append(CINEMATIC_GRADE)

    vf_filters.append(f"ass='{ass_escaped}'")

    vignette_enabled = visuals_cfg.get("vignette", False)
    grain_enabled = visuals_cfg.get("film_grain", False)
    if grain_enabled:
        vf_filters.append("noise=alls=10:allf=t+u")
    if vignette_enabled:
        vf_filters.append("vignette=PI/4")

    captioned_path = temp_dir / "captioned.mp4"
    run_ffmpeg(
        [
            "-i", str(concat_path),
            "-vf", ",".join(vf_filters),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "copy",
            str(captioned_path),
        ],
        "Burn subtitles & cinematic filters (single pass)"
    )
    current_video_track = captioned_path

    # ── Step 5: Mix with SFX and background music ──
    total_vid_dur = get_duration(current_video_track)
    # FIX-059: SFX placement follows the AUDIO timeline (same compensation
    # as captions) so cut accents land on the actual cut moment.
    sfx_starts = [
        off - (audio_leads[i] if i < len(audio_leads) else 0.0)
        for i, off in enumerate(scene_offsets)
    ]
    sfx_track, sfx_placements = _build_sfx_track(script, sfx_starts, total_vid_dur, temp_dir)

    music_mood = audio_cfg.get("music_mood", "dramatic")
    # FIX-058: subject-aware override — "auto" (or a blanket mood that
    # clashes with somber/tense narration) resolves from the actual words.
    narration_all = " ".join(getattr(s, "narration", "") or "" for s in script.scenes)
    music_mood = resolve_music_mood(music_mood, narration_all)
    music_track = _find_music_track(music_mood)

    # FIX-043.4: two-act bed — switch to the contrasting mood at the midpoint
    # scene. Falls back to the single track when the video is short (<75s)
    # or no second track exists (the bed helper bakes the tail fade then).
    music_bed = None
    music_fadeout = audio_cfg.get("music_fadeout_seconds", 2.5)
    if music_track:
        try:
            act2_track = _find_music_track(ACT_MOOD_PAIR.get(music_mood, "dramatic"))
            midpoint_s = (
                scene_offsets[len(scene_offsets) // 2]
                if scene_offsets else total_vid_dur / 2
            )
            music_bed = _build_music_bed(
                music_track, act2_track, float(midpoint_s), total_vid_dur, temp_dir
            )
        except Exception as bed_err:
            log.warning(f"Two-act bed skipped ({bed_err}); using single track")
        if music_bed:
            music_track = music_bed
            music_fadeout = 0.0  # tail fade is baked into the bed

    # Extract audio from captioned video
    voice_audio = temp_dir / "voice_track.aac"
    run_ffmpeg(
        ["-i", str(current_video_track), "-vn", "-c:a", "aac", str(voice_audio)],
        "Extract voice track"
    )

    mixed_audio = temp_dir / "mixed_audio.aac"
    if music_track:
        log.info(f"Mixing with background music ({music_track.name}) + SFX track...")
        mix_audio_with_music(
            voice_audio, music_track, mixed_audio,
            voice_volume_db=audio_cfg.get("voice_volume_db", -14),
            music_volume_db=audio_cfg.get("music_volume_db", -22),
            ducking=audio_cfg.get("ducking", True),
            ducking_reduction_db=audio_cfg.get("ducking_reduction_db", -12),
            sfx_path=sfx_track,
            music_fadeout_seconds=music_fadeout,
            total_duration=total_vid_dur,
        )
    elif sfx_track and sfx_track.exists():
        log.info("Mixing voice with SFX track (no background music)...")
        run_ffmpeg(
            [
                "-i", str(voice_audio), "-i", str(sfx_track),
                "-filter_complex", "[0:a]volume=0dB[v];[1:a]volume=0dB[s];[v][s]amix=inputs=2:duration=first[out]",
                "-map", "[out]", "-c:a", "aac", "-b:a", "192k", str(mixed_audio)
            ],
            "Mix voice + SFX"
        )
    else:
        mixed_audio = voice_audio

    # Replace audio in video
    final_with_audio = temp_dir / "with_audio_mix.mp4"
    run_ffmpeg(
        [
            "-i", str(current_video_track),
            "-i", str(mixed_audio),
            "-c:v", "copy", "-c:a", "aac",
            "-map", "0:v:0", "-map", "1:a:0",
            # FIX-094: never let picture outlive its audio — without
            # -shortest a shorter mix leaves a silent tail behind burned-in
            # captions that keep "speaking" to nobody.
            "-shortest",
            str(final_with_audio),
        ],
        "Replace audio with multi-track mix"
    )
    current = final_with_audio

    # ── Step 6: Normalize audio ──
    log.info("Normalizing audio loudness...")
    normalized = temp_dir / "normalized.mp4"
    target_lufs = audio_cfg.get("master_lufs", -14)

    # Extract audio, normalize, re-mux
    temp_audio = temp_dir / "pre_norm_audio.aac"
    run_ffmpeg(
        ["-i", str(current), "-vn", "-c:a", "aac", str(temp_audio)],
        "Extract audio for normalization"
    )
    norm_audio = temp_dir / "norm_audio.aac"
    normalize_audio(temp_audio, norm_audio, target_lufs)

    run_ffmpeg(
        [
            "-i", str(current),
            "-i", str(norm_audio),
            "-c:v", "copy", "-c:a", "aac",
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest",  # FIX-094: same guarantee as the mix mux above
            str(normalized),
        ],
        "Re-mux normalized audio"
    )

    # ── Step 7: Move to final output ──
    is_vertical = height > width or visuals_cfg.get("aspect_ratio") == "9:16"
    final_name = "final_9x16_short.mp4" if is_vertical else "final_16x9.mp4"
    final_path = output_dir / final_name
    shutil.move(str(normalized), str(final_path))
    if is_vertical and not (output_dir / "final_16x9.mp4").exists():
        shutil.copyfile(str(final_path), str(output_dir / "final_16x9.mp4"))

    # ── Timeline manifest (FIX-004): transition-aware scene starts for metadata
    # chapters, shorts slicing, and any downstream consumer. Lives in the
    # project dir (not _temp) so it survives cleanup. FIX-031 adds the edit
    # manifest the FCPXML export consumes: which concrete media files make up
    # each scene's cuts (hook/punch-cuts/prepared), SFX placements, and the
    # chosen music track — so DaVinci Resolve can rebuild the edit 1:1.
    edit_media: dict[int, list[Path]] = {}
    for idx, (clip_path, cuts_used, _cuts_meta) in prepared.items():
        base = clip_path.parent
        cut_files = sorted(base.glob(f"scene_{idx:02d}_cut*.{PREPARED_V}.mp4"))
        edit_media[idx] = cut_files if (cuts_used and cut_files) else [clip_path]

    try:
        timeline = {
            "hook_rewind_seconds": hook_sec if hook_enabled else 0.0,
            "transitions_enabled": bool(trans_cfg.get("enabled", True)),
            "scene_count": len(script.scenes),
            "fps": int(visuals_cfg.get("fps", 30)),
            "resolution": f"{width}x{height}",
            "edit_media_dir": str(output_dir / "edit_media"),
            "music_track": str(music_track) if music_track else None,
            "music_mood": music_mood,
            "sfx_placements": sfx_placements,
            "audio_leads": [round(x, 3) for x in audio_leads],
            "cut_styles": list(build_styles),
            "scenes": [
                {
                    "scene_number": s.scene_number,
                    "start_seconds": round(off, 3),
                    "pre_lap_seconds": round(
                        audio_leads[i] if i < len(audio_leads) else 0.0, 3
                    ),
                    "duration_seconds": round(
                        s.audio_duration_seconds or s.duration_target_seconds, 3
                    ),
                    "mood": s.mood,
                    "cuts": [
                        {
                            "file": str(p),
                            "start_seconds": round(cs, 3),
                            "duration_seconds": round(cl, 3),
                        }
                        for p, (cs, cl) in zip(
                            edit_media.get(s.scene_number, []),
                            scene_cuts.get(s.scene_number, []),
                        )
                    ],
                }
                for i, (s, off) in enumerate(zip(script.scenes, scene_offsets))
            ],
        }
        (output_dir / "timeline.json").write_text(
            json.dumps(timeline, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"Could not write timeline.json: {e}")

    # ── Persist the exact edited media (FIX-031): the FCPXML references these
    # files, so they must survive _temp cleanup. Broken/missing per-scene
    # entries fall back to the concatenated prepared clip.
    try:
        edit_media_dir = output_dir / "edit_media"
        edit_media_dir.mkdir(exist_ok=True)
        for idx, files in edit_media.items():
            for src in files:
                if src and Path(src).exists():
                    dest = edit_media_dir / src.name
                    if not dest.exists():
                        shutil.copyfile(src, dest)
                elif files and files[0] == src:
                    log.warning(f"Scene {idx}: edited media missing; FCPXML falls back to black")
    except Exception as e:
        log.warning(f"Could not preserve edit media: {e}")

    # Clean up temp files
    log.info("Cleaning up temporary files...")
    shutil.rmtree(temp_dir, ignore_errors=True)

    file_size_mb = final_path.stat().st_size / (1024 * 1024)
    log.info(
        f"[bold green]✅ Video assembled:[/bold green] {final_path.name} "
        f"({file_size_mb:.1f} MB)"
    )
    return final_path


# ──────────────────────────────────────────────
# CLI test mode
# ──────────────────────────────────────────────
if __name__ == "__main__":
    print("FFmpeg Assembler module loaded.")
    print(f"FFmpeg binary: {config.FFMPEG_BIN}")
    print("Run the full pipeline via main.py to test assembly.")
