"""Frame occupancy — where does the subject's face sit inside a rendered frame?

FIX-062. Vertical renders burn captions over the picture; the old
middle-centred block landed straight across the subject's face in portrait
crops (observed live — the post-render frame review reported "caption
obscures subject face" on three scenes of one Short). Placement now has to
know WHERE the face is before choosing a band.

Deliberately dependency-free (PIL + numpy are already core deps; no OpenCV,
no model download): a YCbCr skin-tone mass plus an area floor is enough to
choose between two caption bands, and the post-render frame review still
verifies the final choice. Never raises — an unreadable frame simply yields
the safe default.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

log = get_logger("frame_occupancy")

# Classic YCbCr skin window. Wide on purpose: varied lighting and grading
# must still register, because the detector only has to pick a band.
SKIN_Y = (60, 235)
SKIN_CB = (77, 127)
SKIN_CR = (133, 173)
MIN_FACE_AREA = 0.02     # below 2% of the frame it is a warm wall, not a face
LOW_FACE_Y = 0.58        # face centre below this ⇒ captions must move up
SAMPLE_WIDTH = 64        # decode width for the analysis pass (cheap)

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def _frame_rgb(source: Path | str, at: float = 0.0,
               width: int = SAMPLE_WIDTH):
    """Decode one frame as an (H, W, 3) uint8 RGB array; None on failure.

    Stills load through PIL; anything else is decoded by the ffmpeg binary the
    rest of the pipeline already uses (rawvideo on stdout — no temp files).
    """
    try:
        import numpy as np

        p = Path(source)
        if not p.exists():
            return None
        if p.suffix.lower() in IMAGE_SUFFIXES:
            from PIL import Image

            img = Image.open(p).convert("RGB")
            if img.width != width and img.width > 0:
                img = img.resize((width, max(2, round(img.height * width / img.width))))
            return np.asarray(img, dtype=np.uint8)

        cmd = ["ffmpeg", "-v", "error"]
        if at > 0:
            cmd += ["-ss", f"{at:.3f}"]
        cmd += ["-i", str(p), "-frames:v", "1", "-vf", f"scale={width}:-2",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        buf = r.stdout
        if not buf:
            return None
        h = len(buf) // (width * 3)
        if h < 4:
            return None
        return np.frombuffer(buf[: h * width * 3], dtype=np.uint8).reshape(h, width, 3)
    except Exception as e:  # unreadable frame is never fatal
        log.warning(f"frame decode failed for {str(source)[:60]}: {str(e)[:100]}")
        return None


def face_centre_y(source: Path | str, at: float = 0.0) -> Optional[float]:
    """Vertical centre of the skin-tone mass in 0..1 (0=top); None if none."""
    try:
        import numpy as np

        arr = _frame_rgb(source, at=at)
        if arr is None:
            return None
        r = arr[..., 0].astype("float32")
        g = arr[..., 1].astype("float32")
        b = arr[..., 2].astype("float32")
        y = 0.299 * r + 0.587 * g + 0.114 * b
        cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
        cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b
        skin = ((y >= SKIN_Y[0]) & (y <= SKIN_Y[1])
                & (cb >= SKIN_CB[0]) & (cb <= SKIN_CB[1])
                & (cr >= SKIN_CR[0]) & (cr <= SKIN_CR[1]))
        if float(skin.mean()) < MIN_FACE_AREA:
            return None
        rows = skin.sum(axis=1).astype("float32")
        total = float(rows.sum())
        if total <= 0:
            return None
        h = rows.shape[0]
        return float((rows * np.arange(h)).sum() / total / max(1, h - 1))
    except Exception:
        return None


def detect_face_band(source: Path | str, at: float = 0.0) -> str:
    """'upper' | 'middle' | 'lower' | 'none' — where the face sits."""
    cy = face_centre_y(source, at=at)
    if cy is None:
        return "none"
    if cy < 0.38:
        return "upper"
    if cy > LOW_FACE_Y:
        return "lower"
    return "middle"


def caption_band(source: Path | str, at: float = 0.0,
                 default: str = "bottom") -> str:
    """Which band captions should occupy for this frame: 'bottom' or 'top'.

    The bottom band is the professional Shorts default (clear of the face in
    a normally-framed portrait). Captions are lifted to the top band only when
    the subject's face actually sits low enough to be covered.
    """
    return "top" if detect_face_band(source, at=at) == "lower" else default
