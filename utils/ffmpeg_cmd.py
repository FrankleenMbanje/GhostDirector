"""
GhostDirector — FFmpeg Command Builders

All FFmpeg command construction in one place. No module calls FFmpeg directly
except through these builders.
"""

import subprocess
import json
import random
import math
import re
from pathlib import Path
from typing import Optional

import config
from utils.logger import get_logger

log = get_logger("ffmpeg")


# ──────────────────────────────────────────────
# Face-aware vertical cropping (Phase 4 / FIX-028)
# ──────────────────────────────────────────────

def detect_faces(path: Path) -> list[tuple[float, float, float, float]] | None:
    """Return [(x, y, w, h)] normalized face boxes, or None when cv2 is absent.

    Uses OpenCV's default Haar cascade on one mid-frame. Returns [] when the
    frame has no detectable face (caller falls back to saliency).
    """
    try:
        import cv2
        import tempfile
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frames // 2))  # mid-frame
        ok, frame = cap.read()
        cap.release()
        if not ok:
            return None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
        h_img, w_img = gray.shape
        return [(x / w_img, y / h_img, w / w_img, h / h_img) for (x, y, w, h) in faces]
    except ImportError:
        return None
    except Exception as e:
        log.debug(f"Face detection failed: {e}")
        return None


def detect_saliency_center_y(path: Path) -> float | None:
    """Brightness-energy center (0..1, normalized y) for the no-cv2 fallback.

    People shots usually put the subject in the brighter middle band; a pure
    center crop beheads tall subjects. Cheap numpy proxy: find the row band
    with the highest contrast energy in the top 2/3 (where heads live).
    """
    try:
        import tempfile
        from PIL import Image
        import numpy as np
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            frame_path = Path(tf.name)
        run_ffmpeg(
            ["-ss", "1", "-i", str(path), "-vframes", "1", "-q:v", "3", str(frame_path)],
            "Saliency probe frame",
        )
        img = np.asarray(Image.open(frame_path).convert("L"), dtype=np.float32)
        frame_path.unlink(missing_ok=True)
        if img.size == 0:
            return None
        # Row-wise edge energy (gradient magnitude), smoothed
        gy = np.abs(np.diff(img, axis=0)).mean(axis=1)
        window = max(8, len(gy) // 10)
        kernel = np.ones(window) / window
        energy = np.convolve(gy, kernel, mode="same")
        top_part = energy[: int(len(energy) * 0.75)]  # heads live in the top 75%
        return float(np.argmax(top_part) / len(energy))
    except Exception as e:
        log.debug(f"Saliency probe failed: {e}")
        return None


def smart_crop_origin_y(path: Path, in_ar: float, target_ar: float) -> float | None:
    """Vertical crop origin (0=top, 1=bottom) that keeps faces in frame.

    For a landscape source cropped to a taller target canvas, ffmpeg's crop
    filter centers by default (y=(ih-oh)/2) — which beheads people when the
    subject stands high in frame. Returns a 0..1 crop-center bias instead so
    the caller computes the offset. None → keep centered.
    """
    faces = detect_faces(path)
    if faces:
        # Weighted centroid of the two largest faces
        faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)[:2]
        total = sum(f[2] * f[3] for f in faces) or 1.0
        cy = sum((f[1] + f[3] / 2) * (f[2] * f[3]) for f in faces) / total
        return max(0.0, min(1.0, cy))

    if in_ar > 1.15:  # landscape sources benefit most
        sal = detect_saliency_center_y(path)
        if sal is not None:
            return max(0.0, min(1.0, sal))
    return None


def _fair_use_transform(seed: int, width: int, height: int) -> str:
    """
    Fair-use transformation for scraped assets (policy: UPGRADE_PLAN.md §2).

    Mirroring (hflip) was removed deliberately: YouTube's spam-detection
    treats mirrored footage as an evasion signal and it visibly reverses
    on-screen text (jerseys, mugshot placards, signage).

    Instead we apply deterministic per-asset variation that changes the
    pixel presentation without mirroring:
      - crop window shifted toward one edge (center/right/left bias)
      - a slight additional zoom past the exact canvas crop
      - small per-asset brightness/saturation jitter
    The seed (e.g. scene number) makes each scraped asset transform
    differently while staying reproducible for re-renders.
    """
    rng = random.Random(f"fairuse:{seed}")

    # Crop bias: where the extra zoomed-in window sits horizontally.
    bias = rng.choice(("center", "right", "left"))
    if bias == "center":
        x_expr = "(iw-ow)/2"
    elif bias == "right":
        x_expr = "(iw-ow)*0.82"
    else:  # left
        x_expr = "(iw-ow)*0.18"

    # Extra zoom: crop a slightly smaller window (10-18%) then scale back up.
    zoom = rng.uniform(0.82, 0.90)
    crop_w = int(width * zoom)
    crop_h = int(height * zoom)

    # Tiny grade jitter so identical stock from two videos doesn't match pixel-for-pixel.
    contrast = rng.uniform(1.03, 1.08)
    saturation = rng.uniform(1.05, 1.12)

    return (
        f"crop={crop_w}:{crop_h}:{x_expr}:(ih-oh)/2,"
        f"scale={width}:{height},"
        f"eq=contrast={contrast:.3f}:saturation={saturation:.3f}"
    )


def run_ffmpeg(args: list[str], desc: str = "FFmpeg") -> subprocess.CompletedProcess:
    """Run an FFmpeg command and handle errors."""
    ffmpeg_bin = getattr(config, "FFMPEG_BIN", "ffmpeg")
    cmd = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "warning"] + args
    log.info(f"[dim]{desc}:[/dim] {' '.join(cmd[:8])}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"FFmpeg failed: {result.stderr}")
        # Keep the TAIL of stderr: FFmpeg prints benign warnings first
        # (swscaler 'deprecated pixel format', etc.) and the actual fatal
        # error LAST — the old [:500] slice hid the real cause entirely
        # (found live on the Khaled long-form xfade failure, 2026-09-27).
        raise RuntimeError(f"FFmpeg error: {result.stderr[-1200:]}")
    return result


def get_duration(file_path: Path) -> float:
    """Get the duration of an audio or video file in seconds."""
    ffprobe_bin = getattr(config, "FFPROBE_BIN", "ffprobe")
    cmd = [
        ffprobe_bin, "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(file_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {file_path}: {result.stderr}")
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def get_media_aspect_ratio(path: Path) -> float:
    """Get the true display aspect ratio (width / height) of an image or video file."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            w, h = img.size
            if w and h:
                return float(w) / float(h)
    except Exception:
        pass

    try:
        ffprobe_bin = getattr(config, "FFPROBE_BIN", "ffprobe")
        cmd = [
            ffprobe_bin, "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            str(path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            for s in data.get("streams", []):
                if s.get("codec_type") == "video":
                    w = int(s.get("width", 0))
                    h = int(s.get("height", 0))
                    if w and h:
                        return float(w) / float(h)
    except Exception:
        pass
    return 16.0 / 9.0


def scale_and_crop(
    input_path: Path,
    output_path: Path,
    width: int = 1920,
    height: int = 1080,
    duration: Optional[float] = None,
    apply_fair_use: bool = False,
    seed: int = 0,
) -> None:
    """Scale and fit a video/clip to exact dimensions without distortion."""
    args = []
    if duration is not None:
        # Loop input video seamlessly if its source duration is shorter than target narration
        args += ["-stream_loop", "-1"]
    args += ["-i", str(input_path)]

    if duration is not None:
        args += ["-t", str(duration)]

    in_ar = get_media_aspect_ratio(input_path)
    target_ar = float(width) / float(height)

    # Detect orientation mismatch (e.g. landscape video in 9:16 Short, or portrait in 16:9)
    is_mismatch = (target_ar < 1.0 and in_ar > 1.15) or (target_ar >= 1.0 and in_ar < 0.85)

    if is_mismatch:
        # Face-aware vertical crop (FIX-028): bias the crop window toward
        # detected faces (cv2) or brightness-energy center so subjects in
        # landscape sources survive the 9:16 conversion uncropped-headless.
        # Full-frame fill: scale up + center crop. The old pillarbox+blur
        # layout read as amateur hour on Shorts (Cruise short QC) — modern
        # short-form always fills the frame.
        filter_str = (
            f"[0:v]setsar=1,scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1"
        )
        if apply_fair_use:
            filter_str += "," + _fair_use_transform(seed, width, height)
        # Auto-grade (stream_loop repeats the source, so its midpoint level
        # is representative of what ships).
        grade = _auto_grade_filter(
            _segment_mean_luma(input_path, (get_duration(input_path) or 4.0) / 2.0)
        )
        if grade:
            filter_str += "," + grade
        args += [
            "-filter_complex", filter_str,
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-an",
            str(output_path),
        ]
    else:
        filter_str = (
            f"setsar=1,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            f"setsar=1"
        )
        if apply_fair_use:
            filter_str += "," + _fair_use_transform(seed, width, height)
        grade = _auto_grade_filter(
            _segment_mean_luma(input_path, (get_duration(input_path) or 4.0) / 2.0)
        )
        if grade:
            filter_str += "," + grade
        args += [
            "-vf", filter_str,
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-an",
            str(output_path),
        ]

    run_ffmpeg(args, f"Scale/crop -> {output_path.name}")
    # Monitor pass: verify what actually shipped.
    out_min = _segment_mean_luma(output_path)
    if out_min is not None and out_min < 45:
        run_ffmpeg(
            _dark_lift_args(args, filter_str, out_min),
            f"Dark-output lift ({out_min:.0f}) -> {output_path.name}",
        )


def _segment_mean_luma(path: Path, start: float = 0.5) -> float | None:
    """DARKEST frame luma across the WHOLE clip — full decode, no seeking.

    Seek-based sampling was the bug: -ss snaps to keyframes, so probes
    landed on bright frames beside near-black sections and the grade never
    fired (dark theater stock shipped twice on the Cruise short). Decoding
    everything and taking min(YAVG) cannot miss a dark stretch.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", str(path), "-vf",
             "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        vals = [float(v) for v in re.findall(r"YAVG=([\d.]+)", r.stdout or "")]
        return min(vals) if vals else None
    except Exception:
        return None


def _auto_grade_filter(luma: float | None) -> str:
    """Colorist lift for dark b-roll. Captions die below ~50 luma on a
    phone screen (the dark-theater-stock look); a graded lift keeps the
    shot usable instead of shipping a black hole."""
    if luma is None or luma >= 48:
        return ""
    if luma < 30:
        return "eq=brightness=0.2:gamma=1.45:contrast=1.02"
    return "eq=brightness=0.1:gamma=1.2"


def _dark_lift_args(args: list[str], filter_str: str, out_min: float) -> list[str]:
    """Rebuild render args with a corrective lift appended to the filter.

    Output verification — the colorist's monitor pass: internal dimming
    (blur-fill backgrounds, fair-use transforms) can sink even a bright
    source into near-black. If the shipped file has a dark stretch, this
    rebuilds once with a lift tuned to how dark it actually reads.
    """
    lift = ("eq=brightness=0.15:gamma=1.5" if out_min < 25
            else "eq=brightness=0.07:gamma=1.25")
    if filter_str.rstrip().endswith("[vout]"):
        new_filter = filter_str.replace("[vout]", "," + lift + "[vout]")
    else:
        new_filter = filter_str + "," + lift
    return [new_filter if a == filter_str else a for a in args]


def _image_mean_luma(path: Path) -> float | None:
    """Mean luma of a still image (same 0-255 scale as the video check)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", str(path), "-frames:v", "1", "-vf",
             "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=15,
        )
        m = re.search(r"YAVG=([\d.]+)", r.stdout or "")
        return float(m.group(1)) if m else None
    except Exception:
        return None


def _subject_crop(path: Path, width: int, height: int) -> str:
    """FIX-056: scale-then-crop fragment that aims the crop at the SUBJECT.

    The old chains always center-cropped; a face off to the left died in a
    9:16 reframe. The visual director's saliency scan finds where the
    subject actually is and the crop is aimed there (clamped in-filter so
    rounding can never push it out of bounds). Falls back to the plain
    center crop whenever the scan can't run or says "center is fine".
    """
    try:
        from pipeline.visual_director import saliency_focus, saliency_focus_video
        if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            fx, fy = saliency_focus(path)
        else:
            fx, fy = saliency_focus_video(path)
        if abs(fx - 0.5) <= 0.06 and abs(fy - 0.5) <= 0.06:
            return f",crop={width}:{height}"  # centered subject: plain crop
        x_expr = f"max(0\\,min(iw-{width}\\,{int(round(fx * 1000))}/1000*iw-{width}/2))"
        y_expr = f"max(0\\,min(ih-{height}\\,{int(round(fy * 1000))}/1000*ih-{height}/2))"
        return f",crop={width}:{height}:x='{x_expr}':y='{y_expr}'"
    except Exception:
        return f",crop={width}:{height}"


def cut_video_segment(
    input_path: Path,
    output_path: Path,
    start: float,
    duration: float,
    width: int = 1920,
    height: int = 1920,
    apply_fair_use: bool = False,
    seed: int = 0,
) -> None:
    """Slice one [start, start+duration) segment of a video, re-encoded.

    Phase 1 punch-cut support (A23 extension): video scenes (youtube_clip /
    stock_video) get the same multi-cut treatment as photos, so every scene
    in the video changes framing at sentence boundaries. Re-encoding with
    input seeking gives frame-accurate cut points (stream copy would snap to
    keyframes and mis-time the slice against narration). Audio is dropped —
    narration is overlaid by the assembler.
    """
    start = max(0.0, float(start))
    duration = max(0.4, float(duration))

    in_ar = get_media_aspect_ratio(input_path)
    target_ar = float(width) / float(height)
    is_mismatch = (target_ar < 1.0 and in_ar > 1.15) or (target_ar >= 1.0 and in_ar < 0.85)

    # Same orientation: scale/crop straight onto the canvas. Orientation
    # mismatch (landscape clip in a 9:16 Short): blur-fill background with a
    # face-aware vertical crop bias (FIX-028).
    if is_mismatch:
        # Full-frame fill (no pillarbox bars, no blur background — the
        # blur-pillbox look screams template video; crop fills like a
        # native vertical shoot). FIX-056: crop aims at the subject.
        filter_str = (
            f"[0:v]setsar=1,scale={width}:{height}:force_original_aspect_ratio=increase"
            f"{_subject_crop(input_path, width, height)},setsar=1"
        )
    else:
        filter_str = (f"[0:v]setsar=1,scale={width}:{height}:force_original_aspect_ratio=increase"
                      f"{_subject_crop(input_path, width, height)},setsar=1")
    if apply_fair_use:
        filter_str += "," + _fair_use_transform(seed, width, height)
    # Auto-grade: dark footage gets lifted like a human colorist would —
    # near-black b-roll swallowed captions on phones (Cruise short QC).
    # Measured AT the segment's own offset: a source can average bright and
    # still carry the dark stretch this cut actually samples.
    grade = _auto_grade_filter(_segment_mean_luma(input_path, start + 0.5))
    if grade:
        filter_str += "," + grade
    filter_str += "[vout]"

    args = [
        "-ss", f"{start:.3f}",          # input seek before -i = fast and frame-accurate on re-encode
        "-i", str(input_path),
        "-t", f"{duration:.3f}",
        "-filter_complex", filter_str,
        "-map", "[vout]",
        "-an",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        str(output_path),
    ]
    run_ffmpeg(args, f"Cut segment {start:.1f}s+{duration:.1f}s -> {output_path.name}")
    # Monitor pass: verify what actually shipped.
    out_min = _segment_mean_luma(output_path)
    if out_min is not None and out_min < 45:
        run_ffmpeg(
            _dark_lift_args(args, filter_str, out_min),
            f"Dark-output lift ({out_min:.0f}) -> {output_path.name}",
        )


def photo_to_video(
    image_path: Path,
    output_path: Path,
    duration: float,
    width: int = 1920,
    height: int = 1080,
    zoom_start: float = 1.0,
    zoom_end: float = 1.15,
    apply_fair_use: bool = False,
    seed: int = 0,
    motion: str = "zoom_in",
) -> None:
    """
    Convert a static image to a video with a Ken Burns effect.
    Guarantees 100% distortion-free aspect ratio for both 16:9 and 9:16 Shorts.
    Uses cinematic ambient blur-fill when the image orientation is opposite to the video canvas.

    `motion` selects the camera move so consecutive photo scenes don't all
    zoom in identically (a slideshow fingerprint):
      "zoom_in"  — slow push toward the subject (default, classic Ken Burns)
      "zoom_out" — slow reveal away from the subject
      "pan_left" / "pan_right" — lateral drift at fixed zoom
      "auto"     — deterministic random pick from the above using `seed`
    """
    fps = 30
    total_frames = max(int(duration * fps), 1)
    zoom_range = zoom_end - zoom_start

    # Resolve motion variant (deterministic per seed so re-renders match)
    motion = (motion or "zoom_in").lower()
    if motion in ("auto", "random", "vary"):
        motion = random.Random(f"motion:{seed}").choice(
            ("zoom_in", "zoom_out", "pan_left", "pan_right")
        )

    # Smoothstep easing on the animation clock (FIX-043.2): zoompan's native
    # `on` ramp is LINEAR — motion starts/stops abruptly and reads as
    # slideshow drift. `p` = eased progress in [0,1] gives slow-in/slow-out.
    # clamp(on/(N-1)) keeps the last frame from sampling p>1.
    eased = (
        f"min(max(on/{max(total_frames - 1, 1)},0),1)"
    )
    p = f"({eased}*{eased}*(3-2*{eased}))"  # smoothstep(p) = p*p*(3-2p)

    # Build the zoompan expression for the chosen camera move (eased).
    if motion == "zoom_out":
        zoompan_expr = (
            f"zoompan=z='{zoom_end:.4f}-({zoom_range:.6f})*{p}':"
            f"d={total_frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps}"
        )
    elif motion == "pan_left":
        pan_zoom = max(zoom_end, 1.10)  # headroom so x has room to travel
        zoompan_expr = (
            f"zoompan=z='{pan_zoom:.4f}':"
            f"d={total_frames}:x='(iw-iw/zoom)*(1-{p})':"
            f"y='(ih-ih/zoom)/2':s={width}x{height}:fps={fps}"
        )
    elif motion == "pan_right":
        pan_zoom = max(zoom_end, 1.10)
        zoompan_expr = (
            f"zoompan=z='{pan_zoom:.4f}':"
            f"d={total_frames}:x='(iw-iw/zoom)*{p}':"
            f"y='(ih-ih/zoom)/2':s={width}x{height}:fps={fps}"
        )
    else:  # zoom_in (default)
        zoompan_expr = (
            f"zoompan=z='{zoom_start:.4f}+({zoom_range:.6f})*{p}':"
            f"d={total_frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps}"
        )

    in_ar = get_media_aspect_ratio(image_path)
    target_ar = float(width) / float(height)

    # Detect orientation mismatch (e.g. landscape photo in 9:16 Short, or portrait photo in 16:9)
    is_mismatch = (target_ar < 1.0 and in_ar > 1.15) or (target_ar >= 1.0 and in_ar < 0.85)

    if is_mismatch:
        # Full-frame Ken Burns: crop the photo to the target frame instead of
        # pillarboxing it over a blurred copy — the blur-bar layout reads
        # cheap in the Shorts feed, and captions over a crisp full-bleed
        # image always outperform captions over two layers.
        filter_str = (
            f"[0:v]setsar=1,scale={width}:{height}:force_original_aspect_ratio=increase"
            f"{_subject_crop(image_path, width, height)},setsar=1,"
            f"{zoompan_expr}"
        )
        if apply_fair_use:
            filter_str += "," + _fair_use_transform(seed, width, height)
        grade = _auto_grade_filter(_image_mean_luma(image_path))
        if grade:
            filter_str += "," + grade

        args = [
            "-loop", "1",
            "-i", str(image_path),
            "-filter_complex", filter_str,
            "-t", str(duration),
            "-c:v", "libx264", "-crf", "20", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            str(output_path),
        ]
    else:
        # Same orientation: Pre-scale and crop to exact target aspect ratio buffer, then zoompan
        if width >= height:
            pre_w = 2560
            pre_h = int(2560 * height / width)
        else:
            pre_h = 2560
            pre_w = int(2560 * width / height)

        filter_str = (
            f"setsar=1,"
            f"scale={pre_w}:{pre_h}:force_original_aspect_ratio=increase"
            f"{_subject_crop(image_path, pre_w, pre_h)},"
            f"setsar=1,"
            f"{zoompan_expr}"
        )
        if apply_fair_use:
            filter_str += "," + _fair_use_transform(seed, width, height)
        grade = _auto_grade_filter(_image_mean_luma(image_path))
        if grade:
            filter_str += "," + grade

        args = [
            "-loop", "1",
            "-i", str(image_path),
            "-vf", filter_str,
            "-t", str(duration),
            "-c:v", "libx264", "-crf", "20", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            str(output_path),
        ]

    run_ffmpeg(args, f"Ken Burns -> {output_path.name}")
    # Monitor pass: verify what actually shipped.
    out_min = _segment_mean_luma(output_path)
    if out_min is not None and out_min < 45:
        run_ffmpeg(
            _dark_lift_args(args, filter_str, out_min),
            f"Dark-output lift ({out_min:.0f}) -> {output_path.name}",
        )


def concat_videos(input_paths: list[Path], output_path: Path) -> None:
    """Concatenate multiple video files into one."""
    # Create a concat file list
    concat_file = output_path.parent / "_concat_list.txt"
    with open(concat_file, "w") as f:
        for p in input_paths:
            # FIX-068: the concat demuxer resolves list entries relative to
            # the LIST FILE's directory, so relative input paths get doubled
            # (e.g. _temp\output\...). Always write absolute paths.
            escaped = str(Path(p).resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    args = [
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        str(output_path),
    ]
    run_ffmpeg(args, f"Concat {len(input_paths)} clips")

    # Clean up
    concat_file.unlink(missing_ok=True)


def add_audio_to_video(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    video_volume_db: float = 0,
) -> None:
    """Overlay audio onto a video."""
    args = [
        "-i", str(video_path),
        "-i", str(audio_path),
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest",
        str(output_path),
    ]
    run_ffmpeg(args, f"Add audio -> {output_path.name}")


def mix_audio_with_music(
    voice_path: Path,
    music_path: Path,
    output_path: Path,
    voice_volume_db: float = 0,
    music_volume_db: float = -24,
    ducking: bool = True,
    ducking_reduction_db: float = -15,
    sfx_path: Path | None = None,
    sfx_volume_db: float = -8.0,
    music_fadeout_seconds: float = 2.5,
    total_duration: float | None = None,
) -> None:
    """
    Mix narration voice with background music and optional sound effects track.
    Applies audio ducking (music lowers when voice plays).

    Phase 1 sound discipline (A25):
      - SFX is sidechain-ducked by the voice too, so whooshes never ride on
        top of narration the way a fixed 0.6 volume did.
      - The music tail fades out over `music_fadeout_seconds` ending at the
        mix end, so the soundtrack doesn't hard-clip when the video stops.

    FIX-076 (operator: "music is too loud sometimes"):
      - default music bed -20 → -24 dB, ducker ratio 10 → 14 and threshold
        0.05 → 0.03 so the bed sits UNDER the voice even between phrases;
      - voice chain starts with single-pass loudnorm (I=-16, TP=-1.5) so the
        narration rides at a steady level before the compressor;
      - the final amix is capped by a limiter (alimiter=0.971 ≈ -0.26 dBFS)
        so loud music stingers can never clip the mix.
    """
    voice_fx = ("loudnorm=I=-16:TP=-1.5:LRA=11,"
                "acompressor=threshold=-15dB:ratio=3:attack=5:release=50,"
                "equalizer=f=3000:width_type=o:width=1:g=3,"
                "equalizer=f=100:width_type=o:width=1:g=2")

    # Music tail fade (A25): afade `st` is relative to the music stream's own
    # timeline, so we need the mix length. The caller knows it (it just
    # probed the video); without it, skip the fade rather than guess wrong.
    music_fx = f"volume={music_volume_db}dB"
    if music_fadeout_seconds > 0 and total_duration and total_duration > music_fadeout_seconds + 1.0:
        fade_st = max(0.0, total_duration - music_fadeout_seconds)
        music_fx += f",afade=t=out:st={fade_st:.2f}:d={music_fadeout_seconds:.2f}"

    args = ["-i", str(voice_path), "-i", str(music_path)]
    if sfx_path and Path(sfx_path).exists():
        args.extend(["-i", str(sfx_path)])
        if ducking:
            # FIX-069: [voice] is consumed THREE times here (music ducker,
            # SFX ducker, final mix). A filter output label may be referenced
            # only once; Frank's local Windows ffmpeg tolerates the reuse but
            # the Linux runner's ffmpeg rejects it ("Invalid stream
            # specifier: voice"). asplit fans the stream out explicitly.
            filter_str = (
                f"[0:a]{voice_fx},volume={voice_volume_db}dB,asplit=3[voice][voice2][voice3];"
                f"[1:a]{music_fx}[music];"
                f"[music][voice2]sidechaincompress=threshold=0.03:ratio=14:attack=50:release=400[ducked_music];"
                # SFX ducked under narration as well — a 3-4s whoosh played at
                # fixed volume used to talk over the first words of a scene.
                f"[2:a]volume={sfx_volume_db}dB[sfx_raw];"
                f"[sfx_raw][voice3]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=250[sfx];"
                # FIX-076: limiter after the mix — never clip the final master.
                f"[voice][ducked_music][sfx]amix=inputs=3:duration=first:dropout_transition=2[mixed];"
                f"[mixed]alimiter=limit=0.971:level=false[out]"
            )
        else:
            filter_str = (
                f"[0:a]{voice_fx},volume={voice_volume_db}dB[voice];"
                f"[1:a]{music_fx}[music];"
                f"[2:a]volume={sfx_volume_db}dB[sfx];"
                f"[voice][music][sfx]amix=inputs=3:duration=first:dropout_transition=2[mixed];"
                f"[mixed]alimiter=limit=0.971:level=false[out]"
            )
    else:
        if ducking:
            # FIX-069: [voice] consumed twice (ducker + mix) — asplit=2.
            filter_str = (
                f"[0:a]{voice_fx},volume={voice_volume_db}dB,asplit=2[voice][voice2];"
                f"[1:a]{music_fx}[music];"
                f"[music][voice2]sidechaincompress=threshold=0.03:ratio=14:attack=50:release=400[ducked_music];"
                f"[voice][ducked_music]amix=inputs=2:duration=first:dropout_transition=2[mixed];"
                f"[mixed]alimiter=limit=0.971:level=false[out]"
            )
        else:
            filter_str = (
                f"[0:a]{voice_fx},volume={voice_volume_db}dB[voice];"
                f"[1:a]{music_fx}[music];"
                f"[voice][music]amix=inputs=2:duration=first:dropout_transition=2[mixed];"
                f"[mixed]alimiter=limit=0.971:level=false[out]"
            )

    args.extend([
        "-filter_complex", filter_str,
        "-map", "[out]",
        "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ])
    run_ffmpeg(args, "Mix multi-track audio (voice + music + sfx)")


def normalize_audio(input_path: Path, output_path: Path, target_lufs: float = -14) -> None:
    """Normalize audio loudness to target LUFS (YouTube standard: -14)."""
    # First pass: measure loudness
    measure_args = [
        "-i", str(input_path),
        "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-"
    ]
    cmd = ["ffmpeg", "-y", "-hide_banner"] + measure_args
    result = subprocess.run(cmd, capture_output=True, text=True)

    # Parse measured values from stderr
    stderr = result.stderr
    try:
        # Find the JSON block in stderr
        json_start = stderr.rfind("{")
        json_end = stderr.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            measured = json.loads(stderr[json_start:json_end])
            measured_i = measured.get("input_i", "-24")
            measured_tp = measured.get("input_tp", "-1")
            measured_lra = measured.get("input_lra", "11")
            measured_thresh = measured.get("input_thresh", "-34")

            # Second pass: apply normalization
            normalize_filter = (
                f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
                f"measured_I={measured_i}:measured_TP={measured_tp}:"
                f"measured_LRA={measured_lra}:measured_thresh={measured_thresh}:"
                f"linear=true"
            )
            args = [
                "-i", str(input_path),
                "-af", normalize_filter,
                "-c:a", "aac", "-b:a", "192k",
                str(output_path),
            ]
            run_ffmpeg(args, "Normalize audio")
            return
    except (json.JSONDecodeError, KeyError):
        pass

    # Fallback: simple normalization
    args = [
        "-i", str(input_path),
        "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
        "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ]
    run_ffmpeg(args, "Normalize audio (single pass)")


def burn_subtitles(
    video_path: Path,
    ass_path: Path,
    output_path: Path,
) -> None:
    """Burn ASS subtitles onto a video."""
    # Use the ass filter with the fonts directory
    ass_escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
    args = [
        "-i", str(video_path),
        "-vf", f"ass='{ass_escaped}'",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "copy",
        str(output_path),
    ]
    run_ffmpeg(args, "Burn subtitles")
