"""
GhostDirector — Shorts Generator (Phase 3.1)

Extracts the most dramatic segment of the script and renders a 9:16 vertical short.
"""

import sys
from pathlib import Path
import json

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Script, Scene
import config
from utils.logger import get_logger
from utils.ffmpeg_cmd import run_ffmpeg
from pipeline.assembler_ffmpeg import assemble_video

log = get_logger("shorts")

# Mood weights for climax-window scoring (FIX-010): high-tension scenes
# make better Shorts than exposition.
MOOD_WEIGHTS = {
    "dramatic": 3.0,
    "suspenseful": 2.6,
    "dark": 2.4,
    "emotional": 1.6,
    "triumphant": 1.3,
    "upbeat": 1.0,
    "neutral": 0.4,
}


def _scene_dur(scene: Scene) -> float:
    return scene.audio_duration_seconds or scene.duration_target_seconds or 5.0


def _find_dramatic_sequence(script: Script, max_duration: int = 59) -> list[Scene]:
    """
    Sliding-window search over the scene list for the highest-tension run
    that fits under max_duration. Replaces the old "first N scenes" pick,
    which regularly exported exposition instead of the climax.
    """
    scenes = script.scenes
    if not scenes:
        return []

    best: list[Scene] = []
    best_score = -1.0

    for i in range(len(scenes)):
        window: list[Scene] = []
        total = 0.0
        for j in range(i, len(scenes)):
            d = _scene_dur(scenes[j])
            if total + d > max_duration:
                break
            window.append(scenes[j])
            total += d
            if len(window) < 2:
                continue
            tension = sum(MOOD_WEIGHTS.get(s.mood, 0.4) for s in window)
            # Prefer: high tension, more scenes (more cuts), filling the budget
            score = tension + 0.3 * len(window) + 2.0 * (min(total, 45.0) / 45.0)
            if score > best_score:
                best_score = score
                best = list(window)

    return best

def generate_short(script: Script, template: dict, project_dir: Path) -> Path | None:
    """
    Generate a 9:16 short video from the full script.
    """
    shorts_cfg = template.get("shorts", {})
    if not shorts_cfg.get("enabled", False):
        log.info("Shorts generation disabled in template.")
        return None

    log.info("[bold magenta]Starting Shorts Generation (9:16)[/bold magenta]")
    
    max_dur = shorts_cfg.get("max_duration_seconds", 59)
    sequence = _find_dramatic_sequence(script, max_dur)
    
    if not sequence:
        log.warning("Could not find a valid sequence for a Short.")
        return None

    # Create a cloned script specifically for the short
    import copy
    short_script = copy.deepcopy(script)
    short_script.title = f"{script.title} (Short)"
    short_script.scenes = sequence
    short_script.total_scenes = len(sequence)
    short_script.estimated_duration_minutes = max_dur / 60.0
    
    # Create a new template tailored for 9:16
    short_template = json.loads(json.dumps(template))
    short_template["visuals"]["resolution"] = shorts_cfg.get("resolution", "1080x1920")
    short_template["visuals"]["aspect_ratio"] = "9:16"
    # FIX-010: skip the 3s reversed hook — it burns 3 of the 59s budget
    # and a Short's first spoken line IS the hook already.
    short_template["visuals"]["hook_rewind"] = False
    
    # Increase caption size for mobile viewing
    if "captions" in short_template:
        short_template["captions"]["font_size"] = shorts_cfg.get("caption_font_size", 96)

    # We will assemble it in a specific subfolder to avoid overwriting 16:9 assets
    shorts_dir = project_dir / "shorts_output"
    shorts_dir.mkdir(exist_ok=True)

    try:
        # Use the standard assembler but with the 9:16 template
        final_short = assemble_video(short_script, short_template, shorts_dir)
        
        # Move it to the main project folder
        target = project_dir / "final_9x16_short.mp4"
        import shutil
        shutil.move(str(final_short), str(target))
        log.info(f"[bold green]✅ Short generated:[/bold green] {target.name}")

        # FIX-056: same final QC gate as every other render — frame grid,
        # sharpness, flat-frame (gradient fallback) and loudness checks.
        # FIX-057: deep review (scene attribution + fallback ledger + Gemini
        # frame review) since project_dir is available here.
        # Warnings are recorded; criticals surface to the caller.
        try:
            from pipeline.visual_director import final_qc, classify_qc
            ok, issues = final_qc(target, expected_size=(1080, 1920),
                                  report_dir=project_dir, project_dir=project_dir)
            crit, warn = classify_qc(issues)
            for w in warn:
                log.warning(f"  QC warning: {w}")
            if crit:
                log.warning(f"  QC critical in short: {crit}")
        except Exception as qc_err:
            log.info(f"  Short QC skipped ({qc_err})")
        return target
    except Exception as e:
        log.error(f"Failed to generate Short: {e}")
        return None

if __name__ == "__main__":
    print("Run via main.py --shorts")
