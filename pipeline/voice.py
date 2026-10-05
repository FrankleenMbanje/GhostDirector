import sys
import re
import os
import shutil
import random
import asyncio
import subprocess
import tempfile
from pathlib import Path
import httpx
import edge_tts
from utils.ffmpeg_cmd import get_duration, run_ffmpeg
from rich.progress import Progress, SpinnerColumn, TextColumn

# Add parent dir to sys.path to resolve models/config/utils
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Script, Scene
import config
from utils.logger import get_logger
from utils.retry import retry

logger = get_logger(__name__)

@retry(max_attempts=3, base_delay=2.0)
async def _generate_edge_tts(text: str, voice_id: str, rate: str, pitch: str, output_path: Path):
    """Generate audio using Edge-TTS."""
    logger.debug(f"Generating Edge-TTS audio for text: '{text[:30]}...' with voice: {voice_id}")
    communicate = edge_tts.Communicate(text, voice_id, rate=rate, pitch=pitch)
    await communicate.save(str(output_path))


def _split_sentences(text: str) -> list[str]:
    """Split narration into sentences (keep content intact) for pause injection."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p.strip()]


async def _generate_edge_tts_with_pauses(
    text: str, voice_id: str, rate: str, pitch: str, output_path: Path,
    sentence_gap_ms: int = 240,
    seed: int = 0,
):
    """
    Natural-delivery variant (FIX-006 + FIX-012): synthesize per sentence and
    join with real silence gaps. Edge-TTS reads wall-to-wall text without
    breathing room — these micro-pauses are the single biggest anti-robotic win.

    Human-delivery jitter (FIX-012): each sentence gets a small deterministic
    rate variation (+-4%) and each gap a varied length (75-125% of base) —
    real speakers never deliver two sentences at identical cadence. Uniform
    rate + uniform gaps is itself a TTS fingerprint.

    Output stays a single mp3 so Whisper/timestamps/assembler flow unchanged.
    Falls back to plain synthesis if per-sentence synthesis fails.

    FIX-025: sentence joining uses the concat demuxer + ffmpeg instead of
    pydub, which cannot import on Python ≥3.13 (audioop removed from stdlib).
    Silence gaps are generated lavfi clips re-encoded to match the sentence
    clips' codec/rate, so no in-memory audio library is needed at all.
    """
    sentences = _split_sentences(text)
    if len(sentences) < 2:
        await _generate_edge_tts(text, voice_id, rate, pitch, output_path)
        return

    # Parse the already-modulated rate (e.g. "+6%") once for per-sentence jitter
    try:
        base_rate_val = int(str(rate).replace("%", "").replace("+", "").strip() or 0)
    except ValueError:
        base_rate_val = 0
    # FIX-075: parse the base pitch too — a constant pitch is as much a TTS
    # fingerprint as a constant rate; real readers drift up and down.
    try:
        base_pitch_val = int(str(pitch).replace("Hz", "").replace("+", "").strip() or 0)
    except ValueError:
        base_pitch_val = 0

    with tempfile.TemporaryDirectory(prefix="gd_tts_") as tmp:
        tmpdir = Path(tmp)
        parts: list[Path] = []
        gaps_ms: list[int] = []
        for i, sent in enumerate(sentences):
            part = tmpdir / f"part_{i:03d}.mp3"
            # Per-sentence rate jitter: +-4%, deterministic per (seed, sentence)
            jitter = round(random.Random(f"voice:{seed}:{i}").uniform(-4, 4))
            rate_i = f"{base_rate_val + jitter:+d}%"
            # FIX-075: per-sentence pitch drift ±6 Hz around the template base
            pitch_jitter = round(random.Random(f"pitch:{seed}:{i}").uniform(-6, 6))
            pitch_i = f"{base_pitch_val + pitch_jitter:+d}Hz"
            try:
                await _generate_edge_tts(sent, voice_id, rate_i, pitch_i, part)
                if part.exists() and part.stat().st_size > 0:
                    parts.append(part)
                    if i < len(sentences) - 1:
                        # Varied pause lengths: 75-125% of base, deterministic per gap
                        gap_scale = random.Random(f"pause:{seed}:{i}").uniform(0.75, 1.25)
                        gaps_ms.append(int(sentence_gap_ms * gap_scale))
            except Exception as e:
                logger.warning(f"Sentence {i} synthesis failed ({e}); continuing")

        if not parts:
            await _generate_edge_tts(text, voice_id, rate, pitch, output_path)
            return

        if not _concat_audio_with_gaps(parts, gaps_ms, output_path, tmpdir):
            logger.warning("ffmpeg sentence concat failed — falling back to plain synthesis")
            await _generate_edge_tts(text, voice_id, rate, pitch, output_path)


def _concat_audio_with_gaps(
    parts: list[Path], gaps_ms: list[int], output_path: Path, tmpdir: Path,
) -> bool:
    """Join sentence mp3s with silence gaps using the ffmpeg concat demuxer.

    FIX-025 (replaces pydub, broken on Python 3.13+): the concat demuxer
    requires every entry to share codec/sample-rate/channels, so each lavfi
    silence gap is re-encoded to match the sentence clips. Gaps interleave
    with parts via a generated list file; a single pass produces the mp3.
    Returns True on success.
    """
    if not parts:
        return False
    if len(parts) == 1:
        shutil.copyfile(parts[0], output_path)
        return True

    # Probe the sentence clips so the generated silence matches them exactly
    # (concat demuxer behavior is undefined for mismatched stream params).
    try:
        rate = int(get_media_info(parts[0]).get("sample_rate", 44100))
    except Exception:
        rate = 44100

    entries: list[Path] = []
    silence_dir = tmpdir / "silence"
    silence_dir.mkdir(exist_ok=True)
    for i, part in enumerate(parts):
        entries.append(part)
        if i < len(parts) - 1 and i < len(gaps_ms):
            gap = silence_dir / f"gap_{i:03d}.mp3"
            if not gap.exists():
                try:
                    run_ffmpeg(
                        [
                            "-f", "lavfi",
                            "-i", f"anullsrc=r={rate}:cl=mono",
                            "-t", f"{gaps_ms[i] / 1000.0:.3f}",
                            "-c:a", "libmp3lame", "-b:a", "64k",
                            "-ar", str(rate), "-ac", "1",
                            str(gap),
                        ],
                        f"Silence gap {gaps_ms[i]}ms",
                    )
                except Exception as e:
                    logger.warning(f"Silence gap {i} failed ({e}); joining without pause")
                    continue
            entries.append(gap)

    if len(entries) < 2:
        shutil.copyfile(parts[0], output_path)
        return True

    concat_list = tmpdir / "concat.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for e in entries:
            escaped = str(e.resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    try:
        run_ffmpeg(
            [
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c:a", "libmp3lame", "-b:a", "128k",
                str(output_path),
            ],
            f"Join {len(parts)} sentences with {len(entries) - len(parts)} pauses",
        )
        return output_path.exists() and output_path.stat().st_size > 0
    except Exception as e:
        logger.warning(f"Concat failed: {e}")
        return False


def get_media_info(path: Path) -> dict:
    """Best-effort stream info (sample_rate) via ffprobe."""
    try:
        import json as _json
        import config as _config
        ffprobe = getattr(_config, "FFPROBE_BIN", "ffprobe")
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_streams", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            streams = _json.loads(result.stdout or "{}").get("streams", [])
            if streams:
                s = streams[0]
                return {
                    "sample_rate": int(s.get("sample_rate", 44100) or 44100),
                    "channels": int(s.get("channels", 1) or 1),
                }
    except Exception:
        pass
    return {}

@retry(max_attempts=3, base_delay=2.0)
async def _generate_elevenlabs(text: str, voice_id: str, template: dict, output_path: Path):
    """Generate audio using ElevenLabs API with template settings."""
    logger.debug(f"Generating ElevenLabs audio for text: '{text[:30]}...' with voice: {voice_id}")
    if not config.ELEVENLABS_API_KEY:
         raise ValueError("ELEVENLABS_API_KEY is not set. Add it to your .env file.")

    # Read settings from template, with sensible defaults
    el_cfg = template.get("voice_elevenlabs", {})
    model_id = el_cfg.get("model", config.ELEVENLABS_MODEL)
    stability = el_cfg.get("stability", config.ELEVENLABS_STABILITY)
    similarity = el_cfg.get("similarity_boost", config.ELEVENLABS_SIMILARITY_BOOST)
    style = el_cfg.get("style", 0.35)
    speaker_boost = el_cfg.get("use_speaker_boost", True)

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": config.ELEVENLABS_API_KEY
    }
    data = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity,
            "style": style,
            "use_speaker_boost": speaker_boost,
        }
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=data, headers=headers)
        response.raise_for_status()

        with open(output_path, "wb") as f:
            f.write(response.content)

def _get_audio_duration(audio_path: Path) -> float:
    """Get audio duration in seconds using ffprobe."""
    try:
        return get_duration(audio_path)
    except Exception as e:
        logger.error(f"Failed to get audio duration for {audio_path}: {e}")
        return 0.0


def trim_clip_edges(audio_path: Path, keep_s: float = 0.20,
                    min_gap_s: float = 0.50) -> float:
    """FIX-100: cut OVERSIZED leading/trailing silence from a TTS clip.

    Every engine pads its clips (edge-tts, ElevenLabs and gTTS all do), and a
    padded edge is the source of the dead spots the tail check cannot see:
    Eskl7JKbSxU (2026-10-05) shipped 1.16s of digital silence at 46.0-47.2s
    mid-short. Edge silence longer than min_gap_s is reduced to keep_s;
    sentence pauses INSIDE the clip are untouched. Idempotent — a clip
    already inside the budget is returned unchanged. GD_TTS_TRIM=0 disables.
    Returns the final duration (0.0 only when the file cannot be probed).
    """
    dur = _get_audio_duration(audio_path)
    if dur <= 0:
        return 0.0
    if os.environ.get("GD_TTS_TRIM", "1") == "0":
        return dur
    try:
        p = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", str(audio_path),
             "-af", "silencedetect=noise=-40dB:d=0.05", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        starts = [float(x) for x in
                  re.findall(r"silence_start: (-?[0-9.]+)", p.stderr or "")]
        ends = [(float(a), float(b)) for a, b in re.findall(
            r"silence_end: (-?[0-9.]+) \| silence_duration: ([0-9.]+)",
            p.stderr or "")]
        lead = 0.0
        trail = 0.0
        for i, s in enumerate(starts):
            seg_len = ends[i][1] if i < len(ends) else dur - s
            e = ends[i][0] if i < len(ends) else dur
            if s <= 0.02:
                lead = max(lead, seg_len)
            if e >= dur - 0.02:
                trail = max(trail, seg_len)
    except Exception:
        return dur  # the trim must never break a stage
    if lead <= min_gap_s and trail <= min_gap_s:
        return dur

    new_start = max(0.0, lead - keep_s)
    new_end = dur - max(0.0, trail - keep_s)
    span = new_end - new_start
    if span <= 0.2:
        return dur
    tmp = audio_path.with_suffix(".trim.mp3")
    for cmd in (
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{new_start:.3f}",
         "-t", f"{span:.3f}", "-i", str(audio_path), "-c", "copy", str(tmp)],
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{new_start:.3f}",
         "-t", f"{span:.3f}", "-i", str(audio_path), "-c:a", "libmp3lame",
         "-q:a", "4", str(tmp)],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1024:
                trimmed = _get_audio_duration(tmp)
                if trimmed > 0.2:
                    tmp.replace(audio_path)
                    logger.info(
                        f"FIX-100: trimmed {lead:.2f}s lead / {trail:.2f}s "
                        f"trail silence from {audio_path.name} "
                        f"({dur:.2f}s -> {trimmed:.2f}s)")
                    return trimmed
        except Exception:
            pass
        tmp.unlink(missing_ok=True)
    return dur


async def _generate_gtts(text: str, output_path: Path):
    """Fallback: Generate audio using Google Translate TTS."""
    def _sync():
        from gtts import gTTS
        tts = gTTS(text=text, lang='en', tld='co.uk')
        tts.save(str(output_path))
    await asyncio.get_running_loop().run_in_executor(None, _sync)

def _modulate_voice_settings(base_rate: str, base_pitch: str, mood: str, is_hook: bool = False) -> tuple[str, str]:
    """
    Dynamically modulate voice rate and pitch based on scene mood to eliminate robotic monotone.
    Follows voice-ai-development best practices.
    """
    try:
        clean_rate = base_rate.replace("%", "").strip()
        rate_val = int(clean_rate) if clean_rate else 0
    except Exception:
        rate_val = 0

    try:
        clean_pitch = base_pitch.replace("Hz", "").strip()
        pitch_val = int(clean_pitch) if clean_pitch else 0
    except Exception:
        pitch_val = 0

    mood_lower = (mood or "").lower()

    if is_hook or "hook" in mood_lower or "high-energy" in mood_lower or "upbeat" in mood_lower:
        # Fast punchy retention grabber (+14% rate, +2Hz pitch)
        rate_val += 14
        pitch_val += 2
    elif "suspenseful" in mood_lower or "dark" in mood_lower:
        # Deliberate, conspiratorial cadence (-6% rate, -2Hz pitch)
        rate_val -= 6
        pitch_val -= 2
    elif "revelation" in mood_lower or "dramatic" in mood_lower or "triumphant" in mood_lower:
        # Authoritative, impactful
        rate_val += 2
        pitch_val += 1
    elif "curious" in mood_lower:
        # Intrigued inflection
        rate_val += 6
        pitch_val += 2
    elif "emotional" in mood_lower:
        # Slower, softer
        rate_val -= 4
        pitch_val -= 1

    # Clamp to natural boundaries
    rate_val = max(-25, min(40, rate_val))
    pitch_val = max(-10, min(10, pitch_val))

    sign_r = "+" if rate_val >= 0 else ""
    sign_p = "+" if pitch_val >= 0 else ""
    return f"{sign_r}{rate_val}%", f"{sign_p}{pitch_val}Hz"


async def generate_voices(script: Script, template: dict, output_dir: Path) -> Script:
    """
    Generates narration audio for each scene in the script with dynamic mood modulation.

    Uses scene.scene_number for file naming to stay consistent with
    timestamps.py and assembler_ffmpeg.py.
    """
    logger.info("Starting voice generation with dynamic mood inflection...")
    scenes_dir = output_dir / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)

    voice_config = template.get("voice", {})
    provider = voice_config.get("provider", "edge-tts").lower()
    voice_id = voice_config.get("voice_id", "en-US-AndrewMultilingualNeural")
    base_rate = voice_config.get("rate", "+0%")
    base_pitch = voice_config.get("pitch", "+0Hz")
    natural_pauses = voice_config.get("natural_pauses", True)

    # If user chose elevenlabs via CLI, grab the elevenlabs voice_id
    if provider == "elevenlabs":
        el_cfg = template.get("voice_elevenlabs", {})
        voice_id = el_cfg.get("voice_id", voice_id)

    # Safe-switch: without a key or a configured voice, ElevenLabs would fail
    # per scene and cascade noisily. Switch the whole run to Edge-TTS once.
    if provider == "elevenlabs" and (not config.ELEVENLABS_API_KEY or not voice_id):
        logger.warning(
            "ElevenLabs selected but API key/voice_id missing — "
            "using Edge-TTS for the whole run (set ELEVENLABS_API_KEY and "
            "voice_elevenlabs.voice_id to enable)."
        )
        provider = "edge-tts"
        voice_id = voice_config.get("voice_id", "en-US-AndrewMultilingualNeural")

    # FIX-094: a scene can never leave this stage without audio. The old
    # code logged the failure and left scene.audio_path=None; the assembler
    # then padded the scene with anullsrc, captions kept running, and the
    # uploads shipped with audio that simply stops before the picture does
    # (measured: 5.3s and 3.6s dead-air tails on 2026-10-04 uploads). Retry
    # the whole engine chain; if it still fails, RAISE so the stage fails
    # and the scheduler retries/aborts instead of shipping a broken video.
    attempts = 3
    try:
        attempts = max(1, int(os.environ.get("GD_TTS_ATTEMPTS", "3")))
    except ValueError:
        pass
    try:
        backoff = max(0.0, float(os.environ.get("GD_TTS_BACKOFF", "1.5")))
    except ValueError:
        backoff = 1.5
    failed_scenes: list[int] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=False
    ) as progress:
        task = progress.add_task(f"[cyan]Generating audio using {provider}...", total=len(script.scenes))

        for idx, scene in enumerate(script.scenes):
            sn = scene.scene_number
            if not scene.narration:
                logger.warning(f"Scene {sn} has no narration, skipping voice generation.")
                progress.advance(task)
                continue

            # Compute scene-specific dynamic inflection
            scene_rate, scene_pitch = _modulate_voice_settings(
                base_rate, base_pitch, scene.mood, is_hook=(idx == 0)
            )

            # Use scene_number for consistency across all modules
            output_path = scenes_dir / f"scene_{sn}_audio.mp3"

            # Stage-resume guard: a valid existing audio clip is reused so an
            # interrupted run resumes instead of re-spending TTS time (and,
            # for ElevenLabs, money) on every scene.
            if output_path.exists() and output_path.stat().st_size > 1024:
                # FIX-100: resumed clips get the edge trim too (idempotent),
                # so an interrupted run still ships gap-free narration.
                existing_duration = trim_clip_edges(output_path)
                if existing_duration > 0:
                    scene.audio_path = str(output_path)
                    scene.audio_duration_seconds = existing_duration
                    progress.advance(task)
                    continue

            last_error = ""
            for attempt in range(1, attempts + 1):
                try:
                    if provider == "elevenlabs":
                        await _generate_elevenlabs(scene.narration, voice_id, template, output_path)
                    elif natural_pauses:
                        await _generate_edge_tts_with_pauses(
                            scene.narration, voice_id, scene_rate, scene_pitch, output_path,
                            seed=sn,
                        )
                    else:
                        await _generate_edge_tts(scene.narration, voice_id, scene_rate, scene_pitch, output_path)
                except Exception as e:
                    last_error = str(e)
                    logger.error(f"Failed to generate audio for scene {sn} with {provider}: {e}")
                    if provider == "elevenlabs":
                        logger.warning(f"Falling back to Edge-TTS for scene {sn}...")
                        try:
                            edge_voice = template.get("voice", {}).get("voice_id", "en-US-AndrewMultilingualNeural")
                            await _generate_edge_tts_with_pauses(
                                scene.narration, edge_voice, scene_rate, scene_pitch, output_path,
                                seed=sn,
                            )
                        except Exception as edge_e:
                            last_error = str(edge_e)
                            logger.error(f"Edge-TTS fallback failed for scene {sn}: {edge_e}")
                            try:
                                await _generate_gtts(scene.narration, output_path)
                            except Exception as fb_e:
                                last_error = str(fb_e)
                                logger.error(f"gTTS fallback also failed for scene {sn}: {fb_e}")
                    else:
                        logger.warning(f"Falling back to gTTS for scene {sn}...")
                        try:
                            await _generate_gtts(scene.narration, output_path)
                        except Exception as fallback_e:
                            last_error = str(fallback_e)
                            logger.error(f"gTTS fallback also failed for scene {sn}: {fallback_e}")

                if output_path.exists() and _get_audio_duration(output_path) > 0:
                    break
                if output_path.exists():
                    output_path.unlink(missing_ok=True)
                if attempt < attempts:
                    logger.warning(
                        f"Scene {sn}: no usable audio after attempt "
                        f"{attempt}/{attempts} — retrying in "
                        f"{backoff * attempt:.1f}s")
                    await asyncio.sleep(backoff * attempt)

            if output_path.exists() and _get_audio_duration(output_path) > 0:
                # FIX-100: trim the engine's edge padding before the duration
                # drives the scene length (a padded edge = a mid-video hole).
                duration = trim_clip_edges(output_path)
                if duration <= 0:
                    duration = _get_audio_duration(output_path)
                scene.audio_path = str(output_path)
                scene.audio_duration_seconds = duration
                logger.debug(f"Scene {sn} audio generated: {duration:.2f}s")
            else:
                logger.error(
                    f"Scene {sn}: TTS produced no audio after {attempts} "
                    f"attempt(s) (last error: {last_error or 'empty output'}) "
                    f"— recording the scene as FAILED")
                output_path.unlink(missing_ok=True)
                scene.audio_path = None
                scene.audio_duration_seconds = 5.0
                failed_scenes.append(sn)

            progress.advance(task)

    if failed_scenes:
        raise RuntimeError(
            f"FIX-094: TTS produced no audio for scene(s) {failed_scenes} "
            f"after {attempts} attempt(s) each — refusing to continue, "
            f"because a silent scene ships as dead air while the captions "
            f"keep running. Audio already generated is kept on disk (the "
            f"resume guard reuses it) — retry the stage, or raise "
            f"GD_TTS_ATTEMPTS.")
    logger.info("Voice generation completed.")
    return script
