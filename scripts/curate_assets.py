"""
GhostDirector — Asset Curation Helper (plan item A2)

The #1 real quality cap (SCALABILITY.md §2) was asset variety: one music
track per mood and one file per SFX pool meant every video shared the same
bed. The renderer already auto-uses everything it finds:
  - SFX pools:  assets/sfx/<stem>*.wav          (e.g. whoosh_01..N.wav)
  - Music libs: assets/music/<mood>/*.mp3|wav

This script SYNTHESIZES additional pool variants with ffmpeg so day-one
renders rotate between files. Synthesized beds are placeholders: replace
them with licensed/CC0 tracks using the checklist below for a permanent
quality lift (dropped-in files need zero code).

Usage:
    python scripts/curate_assets.py            # add missing pool variants
    python scripts/curate_assets.py --list     # show current library state
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SFX_DIR = ROOT / "assets" / "sfx"
MUSIC_DIR = ROOT / "assets" / "music"

# ffmpeg synthesis recipes (placeholder-grade but usable; anullsrc silence is
# the fallback the old library effectively had via one shared file per pool).
SFX_RECIPES = {
    # whoosh variants: band-passed noise sweeps at different pitches
    "whoosh_02.wav": ["-f", "lavfi", "-i",
                      "anoisesrc=color=pink:duration=0.7:amplitude=0.55",
                      "-af", "lowpass=f=900,highpass=f=180,afade=t=in:d=0.25,afade=t=out:st=0.35:d=0.35,volume=0.9",
                      "-ar", "44100", "-ac", "2"],
    "whoosh_03.wav": ["-f", "lavfi", "-i",
                      "anoisesrc=color=brown:duration=0.9:amplitude=0.5",
                      "-af", "lowpass=f=600,highpass=f=90,afade=t=in:d=0.4,afade=t=out:st=0.5:d=0.4,volume=0.85",
                      "-ar", "44100", "-ac", "2"],
    "sub_impact_02.wav": ["-f", "lavfi", "-i",
                          "sine=frequency=55:duration=1.4",
                          "-af", "aecho=0.7:0.6:90:0.35,afade=t=out:st=0.2:d=1.2,volume=0.95",
                          "-ar", "44100", "-ac", "2"],
    "tension_riser_02.wav": ["-f", "lavfi", "-i",
                             "sine=frequency=120:duration=3.0",
                             "-af", "vibrato=f=6:d=0.9,asetrate=44100*1.12,aresample=44100,"
                                    "afade=t=in:d=2.2,afade=t=out:st=2.5:d=0.5,volume=0.5",
                             "-ar", "44100", "-ac", "2"],
    "camera_shutter_02.wav": ["-f", "lavfi", "-i",
                              "anoisesrc=color=white:duration=0.12:amplitude=0.7",
                              "-af", "highpass=f=1800,afade=t=out:st=0.04:d=0.08,volume=0.8",
                              "-ar", "44100", "-ac", "2"],
}

# Ambient pad per mood (slow detuned sines = low-key cinematic bed).
MUSIC_RECIPES = {
    "dark":       {"file": "dark_track_2.mp3", "freqs": (55, 58.3, 110), "dur": 120, "vol": 0.16},
    "dramatic":   {"file": "dramatic_track_2.mp3", "freqs": (65.4, 98, 130.8), "dur": 120, "vol": 0.17},
    "suspenseful":{"file": "suspenseful_track_2.mp3", "freqs": (49, 51.9, 98), "dur": 120, "vol": 0.14},
    "upbeat":     {"file": "upbeat_track_2.mp3", "freqs": (130.8, 164.8, 196), "dur": 90, "vol": 0.15},
    "chill":      {"file": "chill_track_2.mp3", "freqs": (87.3, 116.5, 174.6), "dur": 120, "vol": 0.13},
}

CC0_CHECKLIST = """
──────────────────────────────────────────────────────────────
OPERATOR CURATION CHECKLIST (30 min, permanent quality lift)
──────────────────────────────────────────────────────────────
The synthesized files above are functional placeholders. To finish A2:
 1. Music: download 2-4 royalty-free/CC0 tracks per mood folder
    (assets/music/<mood>/). Good sources: YouTube Audio Library,
    pixabay.com/music, freepd.com, incompetech.com (CC-BY — keep credits).
    Name them anything; the renderer picks randomly from the folder.
 2. SFX: drop 2-3 more files per stem (assets/sfx/whoosh_*.wav,
    sub_impact_*.wav, tension_riser_*.wav, camera_shutter_*.wav).
    Sources: freesound.org (CC0 filter), pixabay.com/sound-effects.
 3. Voices (optional): set an ElevenLabs key in .env for premium narration,
    or extend VOICE_POOL in utils/channel_state.py with more Edge voices
    (en-US-AriaNeural, en-GB-RyanNeural, en-AU-WilliamNeural...).
The code uses everything you drop in — no code changes, no re-runs needed.
──────────────────────────────────────────────────────────────
"""


def run_ffmpeg(args: list[str]) -> bool:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                              encoding="utf-8", errors="replace")
        return proc.returncode == 0
    except Exception as e:
        print(f"  ffmpeg failed: {e}")
        return False


def list_library() -> None:
    print("── SFX pool ──")
    for p in sorted(SFX_DIR.glob("*.wav")):
        print(f"  {p.name}")
    print("── Music library ──")
    if MUSIC_DIR.exists():
        for mood in sorted(d for d in MUSIC_DIR.iterdir() if d.is_dir()):
            tracks = sorted(list(mood.glob("*.mp3")) + list(mood.glob("*.wav")))
            print(f"  {mood.name}: {len(tracks)} track(s) — {', '.join(t.name for t in tracks)}")


def main() -> None:
    # Windows CP1252 console would crash on box-drawing chars (same bug class
    # the blueprint fixed in utils/logger.py — force UTF-8 here too).
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower() not in ("utf-8", "utf8"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if "--list" in sys.argv:
        list_library()
        return

    print("Synthesizing missing SFX pool variants...")
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    for name, recipe in SFX_RECIPES.items():
        out = SFX_DIR / name
        if out.exists():
            print(f"  skip {name} (exists)")
            continue
        ok = run_ffmpeg([*recipe, str(out)])
        print(f"  {'OK' if ok else 'FAILED'} {name}")

    print("Synthesizing ambient music tracks per mood...")
    for mood, spec in MUSIC_RECIPES.items():
        mood_dir = MUSIC_DIR / mood
        mood_dir.mkdir(parents=True, exist_ok=True)
        out = mood_dir / spec["file"]
        if out.exists():
            print(f"  skip {mood}/{spec['file']} (exists)")
            continue
        f1, f2, f3 = spec["freqs"]
        filt = (
            f"sine=frequency={f1}:duration={spec['dur']}[a];"
            f"sine=frequency={f2}:duration={spec['dur']}[b];"
            f"sine=frequency={f3}:duration={spec['dur']}[c];"
            f"[a][b][c]amix=inputs=3:normalize=1,afade=t=in:d=3,"
            f"afade=t=out:st={spec['dur'] - 4}:d=4,volume={spec['vol']}"
        )
        ok = run_ffmpeg([
            "-f", "lavfi", "-i", filt,
            "-ar", "44100", "-ac", "2", "-b:a", "192k", str(out),
        ])
        print(f"  {'OK' if ok else 'FAILED'} {mood}/{spec['file']}")

    print()
    list_library()
    print(CC0_CHECKLIST)


if __name__ == "__main__":
    main()
