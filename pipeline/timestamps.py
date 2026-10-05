"""
GhostDirector — Timestamps Module

Runs narration audio through Whisper (locally) to extract
word-level timestamps for animated captions.
"""

import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Script
import config
from utils.logger import get_logger

log = get_logger("timestamps")


def _load_whisper_model():
    """Load the Whisper model once and cache it."""
    import whisper_timestamped as whisper

    device = config.WHISPER_DEVICE
    model_name = config.WHISPER_MODEL

    # Check if CUDA is available; fall back to CPU
    try:
        import torch
        if device == "cuda" and not torch.cuda.is_available():
            log.warning("CUDA not available, falling back to CPU for Whisper")
            device = "cpu"
    except ImportError:
        device = "cpu"

    log.info(f"Loading Whisper model '{model_name}' on {device}...")
    model = whisper.load_model(model_name, device=device)
    log.info(f"Whisper model loaded successfully")
    return model


_vad_down = False  # FIX-101: process-wide memory that VAD cannot load


def extract_word_timestamps(audio_path: Path, model=None) -> list[dict]:
    """
    Extract word-level timestamps from an audio file.

    Args:
        audio_path: Path to the .mp3 audio file.
        model: Pre-loaded Whisper model (optional, loaded if None).

    Returns:
        List of dicts: [{"word": "Hello", "start": 0.0, "end": 0.45}, ...]
    """
    global _vad_down
    import whisper_timestamped as whisper

    if model is None:
        model = _load_whisper_model()

    log.info(f"Extracting timestamps from: {audio_path.name}")

    use_vad = (os.environ.get("GD_WHISPER_VAD", "1") != "0") and not _vad_down
    try:
        result = whisper.transcribe(
            model,
            str(audio_path),
            language=config.WHISPER_LANGUAGE,
            detect_disfluencies=False,
            vad=use_vad,  # VAD reduces hallucinations on noisy audio
        )
    except Exception as e:
        if not use_vad:
            raise
        # FIX-101: the silero VAD loads through torch.hub, whose interactive
        # trust prompt stalls on the non-interactive CI runner — every cloud
        # scene then fell back to EVENLY-SPACED fake word timings, starving
        # caption sync, callout timing and cut points. TTS narration is clean
        # speech, so a real no-VAD pass beats fake timings; remember the
        # failure so later scenes don't relitigate it.
        _vad_down = True
        log.warning(
            f"Whisper VAD unavailable ({str(e)[:120]}) — retrying without "
            f"VAD for the rest of this run")
        result = whisper.transcribe(
            model,
            str(audio_path),
            language=config.WHISPER_LANGUAGE,
            detect_disfluencies=False,
            vad=False,
        )

    # Flatten word-level timestamps from all segments
    words = []
    for segment in result.get("segments", []):
        for word_data in segment.get("words", []):
            words.append({
                "word": word_data["text"].strip(),
                "start": round(word_data["start"], 3),
                "end": round(word_data["end"], 3),
                "confidence": round(word_data.get("confidence", 1.0), 3),
            })

    log.info(f"  → Extracted {len(words)} words from {audio_path.name}")
    return words


def _fallback_timestamps(narration: str, duration: float) -> list[dict]:
    """
    Fallback: evenly distribute word timestamps across the audio duration.
    Used if Whisper fails.
    """
    words_list = narration.split()
    if not words_list or duration <= 0:
        return []

    time_per_word = duration / len(words_list)
    result = []
    for i, word in enumerate(words_list):
        result.append({
            "word": word,
            "start": round(i * time_per_word, 3),
            "end": round((i + 1) * time_per_word, 3),
            "confidence": 0.5,  # Low confidence = fallback
        })
    return result


def generate_timestamps(script: Script, output_dir: Path) -> Script:
    """
    Generate word-level timestamps for all scenes in the script.

    Args:
        script: The Script with audio_path populated for each scene.
        output_dir: Project output directory (scenes/ subdirectory).

    Returns:
        Updated Script with timestamps populated for each scene.
    """
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

    scenes_dir = output_dir / "scenes"

    # Load model once for all scenes
    model = None
    try:
        model = _load_whisper_model()
    except Exception as e:
        log.error(f"Failed to load Whisper model: {e}")
        log.warning("Using fallback timestamp method (even distribution)")

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
    ) as progress:
        task = progress.add_task("Extracting timestamps", total=len(script.scenes))

        for scene in script.scenes:
            progress.update(task, description=f"Timestamps: Scene {scene.scene_number}")

            if not scene.audio_path or not Path(scene.audio_path).exists():
                log.warning(f"Scene {scene.scene_number}: no audio file, skipping timestamps")
                progress.advance(task)
                continue

            audio_path = Path(scene.audio_path)
            timestamps_path = scenes_dir / f"scene_{scene.scene_number}_timestamps.json"

            # Stage-resume guard: reuse persisted timestamps when they exist
            # (Whisper is the slowest per-scene stage; never redo it twice).
            if timestamps_path.exists() and timestamps_path.stat().st_size > 2:
                try:
                    scene.timestamps = json.loads(
                        timestamps_path.read_text(encoding="utf-8"))
                    if scene.timestamps:
                        progress.advance(task)
                        continue
                except Exception:
                    pass

            try:
                if model is not None:
                    words = extract_word_timestamps(audio_path, model)
                else:
                    # Fallback
                    duration = scene.audio_duration_seconds or scene.duration_target_seconds
                    words = _fallback_timestamps(scene.narration, duration)

                # Save timestamps to file
                timestamps_path.write_text(
                    json.dumps(words, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

                # Update scene
                scene.timestamps = words

            except Exception as e:
                log.error(f"Scene {scene.scene_number}: Whisper failed: {e}")
                # Use fallback
                duration = scene.audio_duration_seconds or scene.duration_target_seconds
                words = _fallback_timestamps(scene.narration, duration)
                timestamps_path.write_text(
                    json.dumps(words, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                scene.timestamps = words

            progress.advance(task)

    total_words = sum(len(s.timestamps or []) for s in script.scenes)
    log.info(f"[bold green]Timestamps complete:[/bold green] {total_words} words across {len(script.scenes)} scenes")
    return script


# ──────────────────────────────────────────────
# CLI test mode
# ──────────────────────────────────────────────
if __name__ == "__main__":
    from models import Scene, Script

    # Quick test: create a dummy scene with an audio file
    print("Timestamps module loaded. Run with an actual audio file to test.")
    print(f"Whisper model: {config.WHISPER_MODEL}")
    print(f"Device: {config.WHISPER_DEVICE}")
