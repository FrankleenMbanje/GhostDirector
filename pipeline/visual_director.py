"""
GhostDirector — Visual Director (FIX-056: the "professional editor's eye")

Upgrades visual quality end-to-end without touching the workflow shape:

  1. SEARCH/GENERATE → INSPECT → COMPARE → SELECT for every scene asset.
     Pexels/DDG candidates are downloaded side-by-side, measured at the
     pixel level (sharpness, resolution, exposure, contrast, letterbox
     bars, compression mush), hard-rejected when they fail the floor, and
     the survivors are re-ranked by Gemini vision against the scene's
     narration + mood so the chosen asset MATCHES THE WORDS.

  2. SUBJECT-AWARE FRAMING. Crops are aimed by a cheap saliency scan
     (edge energy + color contrast) instead of always center — faces and
     subjects stay inside the safe area for 9:16 and 16:9 alike.

  3. FINAL QC PASS. Before a video may ship, the rendered file itself is
     probed: resolution/AR truth, frame-grid scan (black/frozen/broken
     frames), sharpness floor, and loudness measurement. Hard failures
     block upload exactly like the compliance gate does.

Every decision is logged with its evidence so bad assets can be banned
permanently by the existing QC ban pass.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from pathlib import Path

from utils.logger import get_logger

log = get_logger("visual_director")

# ═══════════════════════════════════════════════════════════════
# 1. PIXEL-LEVEL INSPECTION (the reject loop's measuring stick)
# ═══════════════════════════════════════════════════════════════

# Sharpness floor (Laplacian variance on a 640px-wide grayscale frame).
# Stock b-roll at real 1080p lands 150+; soft/upscaled mush lands <40.
SHARPNESS_FLOOR = 35.0
# Photo hard floor: only outright blur-mush is rejected outright; merely
# soft-but-high-res shots stay in the race (the score ranks sharper
# candidates above them, and cinematic shallow focus is legitimate).
PHOTO_SHARPNESS_FLOOR = 40.0
# Below this a photo is "soft" — scored down but not rejected.
PHOTO_SOFT_SCORE_PENALTY_BELOW = 90.0
# Exposure band (0-255 mean luma) — outside it the frame is a black hole
# or a white-out. (The auto-grade can lift moderately dark footage; this
# floor only rejects footage that grading cannot honestly repair.)
LUMA_LOW, LUMA_HIGH = 12.0, 245.0
CONTRAST_FLOOR = 18.0          # std-dev of luma; flat gray mush below this
LETTERBOX_RUN_MIN = 0.09       # fraction of height that is uniform bar
MIN_PIXELS = 921_600           # < 1280x720 is not acceptable in 2026


def _laplacian_variance(gray) -> float:
    """Variance of the 3x3 Laplacian — the standard no-reference blur metric."""
    import numpy as np

    g = gray.astype("float32")
    resp = (
        -4 * g[1:-1, 1:-1]
        + g[:-2, 1:-1] + g[2:, 1:-1]
        + g[1:-1, :-2] + g[1:-1, 2:]
    )
    return float(resp.var())


def _frame_pixels(path: Path, t: float | None = None, width: int = 640):
    """Decode one frame as a grayscale numpy array, or None."""
    import numpy as np

    cmd = ["ffmpeg", "-v", "error", "-i", str(path)]
    if t is not None:
        cmd = ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path)]
    cmd += ["-frames:v", "1", "-vf", f"scale={width}:-1", "-f", "rawvideo",
            "-pix_fmt", "gray", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        buf = r.stdout
        if not buf:
            return None
        arr = np.frombuffer(buf, dtype=np.uint8)
        # infer height from raw byte count
        n = len(arr)
        if n % width != 0 or n < width * 2:
            return None
        return arr.reshape(n // width, width)
    except Exception:
        return None


def inspect_image_quality(path: Path) -> dict:
    """Measure a still: resolution, sharpness, exposure, contrast, bars.

    Never raises — a broken file returns {'ok': False, 'reason': ...} so the
    reject loop can move to the next candidate.
    """
    try:
        from PIL import Image
        import numpy as np

        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            small = im.resize((min(640, w), int(min(640, w) * h / w) or 1))
            arr = np.asarray(small, dtype=np.float32)
            luma = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]

        sharp = _laplacian_variance(luma)
        mean = float(luma.mean())
        std = float(luma.std())

        # Letterbox / pillarbox bars: contiguous edge rows whose MEAN is
        # near-black. (Row-min was the bug: any dark shadow row on a moody
        # photo counted as a "bar" and dark scenes read as 200% letterboxed.
        # A true bar has row MEAN ≈ 0-8; a dark photo's row mean stays >15.)
        row_mean = luma.mean(axis=1)
        n_rows = int(row_mean.shape[0])
        top = 0
        for v in row_mean:
            if v < 10:
                top += 1
            else:
                break
        bot = 0
        for v in row_mean[::-1]:
            if v < 10:
                bot += 1
            else:
                break
        # Bars must not span the whole frame — that's a dark IMAGE, not bars.
        bar_frac = 0.0 if (top + bot) >= n_rows else (top + bot) / max(n_rows, 1)

        reasons = []
        if w * h < MIN_PIXELS:
            reasons.append(f"resolution {w}x{h} below 720p floor")
        if sharp < PHOTO_SHARPNESS_FLOOR:
            reasons.append(f"blurry (sharpness {sharp:.0f} < {PHOTO_SHARPNESS_FLOOR:.0f})")
        if mean < LUMA_LOW or mean > LUMA_HIGH:
            reasons.append(f"exposure {mean:.0f} outside usable band")
        if std < CONTRAST_FLOOR:
            reasons.append(f"flat/low contrast ({std:.0f})")
        if bar_frac > LETTERBOX_RUN_MIN:
            reasons.append(f"hard letterbox bars ({bar_frac:.0%} of height)")

        return {
            "ok": not reasons,
            "reasons": reasons,
            "width": w, "height": h,
            "megapixels": round(w * h / 1e6, 2),
            "sharpness": round(sharp, 1),
            "luma": round(mean, 1),
            "contrast": round(std, 1),
            "bars_frac": round(bar_frac, 3),
        }
    except Exception as e:
        return {"ok": False, "reasons": [f"unreadable: {e}"]}


def inspect_video_quality(path: Path, samples: int = 4) -> dict:
    """Measure a clip: probe truth + sampled-frame sharpness/exposure."""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(p.stdout or "{}")
        vs = next((s for s in data.get("streams", [])
                   if s.get("codec_type") == "video"), None)
        if not vs:
            return {"ok": False, "reasons": ["no video stream"]}
        w = int(vs.get("width") or 0)
        h = int(vs.get("height") or 0)
        fps = eval(vs.get("avg_frame_rate") or "0/1") if "/" in (vs.get("avg_frame_rate") or "0/1") else 0.0
        dur = float(data.get("format", {}).get("duration") or 0)

        sharps, lumas = [], []
        for i in range(samples):
            t = dur * (i + 0.5) / samples if dur > 1 else 0.0
            gray = _frame_pixels(path, t)
            if gray is None:
                continue
            import numpy as np
            luma = gray.astype("float32")
            sharps.append(_laplacian_variance(luma))
            lumas.append(float(luma.mean()))

        reasons = []
        if w * h < MIN_PIXELS:
            reasons.append(f"resolution {w}x{h} below 720p floor")
        if dur < 3.0:
            reasons.append(f"clip too short ({dur:.1f}s)")
        if fps and fps < 23.0:
            reasons.append(f"low fps ({fps:.0f})")
        if sharps and statistics.median(sharps) < SHARPNESS_FLOOR:
            reasons.append(f"blurry frames (median sharpness {statistics.median(sharps):.0f})")
        if lumas and (min(lumas) < LUMA_LOW or max(lumas) > LUMA_HIGH):
            reasons.append("frame exposure outside usable band")
        if not sharps:
            reasons.append("frames undecodable")

        return {
            "ok": not reasons,
            "reasons": reasons,
            "width": w, "height": h, "fps": round(fps, 1) if fps else None,
            "duration": round(dur, 2),
            "sharpness": round(statistics.median(sharps), 1) if sharps else 0.0,
            "luma_range": [round(min(lumas), 1), round(max(lumas), 1)] if lumas else None,
        }
    except Exception as e:
        return {"ok": False, "reasons": [f"unreadable: {e}"]}


# ═══════════════════════════════════════════════════════════════
# 2. GEMINI VISION RANKING (match the visual to the words)
# ═══════════════════════════════════════════════════════════════

_RANK_PROMPT = """You are a demanding YouTube editor choosing b-roll. The narration \
line for this scene is:

"{narration}"

Mood: {mood}. People expected on screen: {people}.

Rank these {n} candidate images by how well each one supports EXACTLY what the \
sentence says, its cinematic production value, and how well the important subject \
is framed. Reject candidates that look like AI-generated people when real footage \
is expected, that contradict the sentence, or that are visually boring/duplicated.

Answer with ONLY compact JSON: {{"best": <0-based index>, "ranking": [<indices best-to-worst>], "reason": "<max 12 words>"}}"""


async def rank_candidates_with_gemini(
    paths: list[Path], narration: str, mood: str, people: list[str],
) -> tuple[int, str] | None:
    """Ask Gemini to pick the best candidate. Returns (index, reason) or None.

    Failure is ALWAYS non-fatal: the deterministic quality score carries the
    decision offline.
    """
    try:
        import asyncio

        import config

        if not config.GEMINI_API_KEY or len(paths) < 2:
            return None

        from google import genai
        from google.genai import types as genai_types

        prompt = _RANK_PROMPT.format(
            narration=narration[:600], mood=mood,
            people=", ".join(people) or "none specified", n=len(paths),
        )
        parts: list = [{"inline_data": {"mime_type": "image/jpeg", "data": p.read_bytes()}} for p in paths]
        parts.append({"text": prompt})
        contents = genai_types.Content(role="user", parts=parts)

        last_err = None
        for model_id in config.gemini_candidates():
            try:
                client = genai.Client(api_key=config.GEMINI_API_KEY)
                def _call():
                    return client.models.generate_content(
                        model=model_id, contents=contents,
                        config=genai_types.GenerateContentConfig(
                            temperature=0.1, max_output_tokens=200,
                            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                        ),
                    )
                resp = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, _call),
                    timeout=40,
                )
                text = (resp.text or "").strip()
                m = re.search(r"\{.*\}", text, re.S)
                if not m:
                    last_err = f"unparseable reply: {text[:80]}"
                    continue
                data = json.loads(m.group(0))
                best = int(data.get("best", -1))
                if 0 <= best < len(paths):
                    return best, str(data.get("reason", ""))[:120]
                last_err = f"index out of range: {data}"
            except Exception as e:
                last_err = str(e)[:160]
                continue
        log.warning(f"Gemini ranking unavailable ({last_err}) — using pixel-score fallback")
        return None
    except Exception as e:
        log.warning(f"Gemini ranking skipped: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# 3. THE SELECTION LOOP (inspect → compare → select best)
# ═══════════════════════════════════════════════════════════════

def _face_score_adjust(m: dict) -> float:
    """FIX-095: explicit preference for a candidate where a face is visible.

    Only populated when the scene expects people (see select_best_visual):
    the best-lit photo of a crowd is the wrong shot for a line that names
    the person, so a visible subject outranks a marginally cleaner backdrop.
    None/absent leaves the score untouched.
    """
    if m.get("face") is True:
        return 0.9
    if m.get("face") is False:
        return -0.7
    return 0.0


def _quality_score(m: dict, target_w: int, target_h: int) -> float:
    """Deterministic desirability score used to order survivors."""
    if not m.get("ok"):
        return -1e9
    px = m.get("width", 0) * m.get("height", 0)
    target_px = max(target_w * target_h, 1)
    # Sweetspot at ≥ target; nothing extra beyond 2x (bigger decode cost only)
    px_score = min(px / target_px, 2.0)
    sharp = min(m.get("sharpness", 0) / 300.0, 1.0)
    # Merely-soft (not rejected) high-res shots rank below crisp equals
    if m.get("sharpness", 0) < PHOTO_SOFT_SCORE_PENALTY_BELOW:
        sharp *= 0.6
    contrast = min(m.get("contrast", 0) / 60.0, 1.0)
    # Gentle penalty for extreme ARs that would need heavy cropping
    ar = (m.get("width", 1) / max(m.get("height", 1), 1))
    ar_penalty = 0.0
    if target_w >= target_h and ar > 2.4:
        ar_penalty = 0.2
    if target_w < target_h and ar < 0.5:
        ar_penalty = 0.2
    bars = 1.0 - float(m.get("bars_frac", 0))
    return 2.0 * px_score + 2.5 * sharp + 1.0 * contrast + 0.5 * bars - ar_penalty


async def select_best_visual(
    candidate_urls: list[str],
    narration: str,
    mood: str = "neutral",
    people: list[str] | None = None,
    target_size: tuple[int, int] = (1920, 1080),
    kind: str = "photo",
    max_candidates: int = 5,
    already_used: set[str] | None = None,
) -> tuple[str | None, dict]:
    """The SEARCH → INSPECT → COMPARE → SELECT loop.

    Downloads up to max_candidates, hard-rejects the failures, orders the
    survivors by pixel quality, then lets Gemini vision pick among the top
    few for narrative relevance. Returns (chosen_url, evidence dict).
    Never raises; returns (None, evidence) when everything is rejected.
    """
    import asyncio
    import tempfile
    import time

    from pipeline.assets import download_file  # reuse the project's downloader

    people = people or []
    already_used = already_used or set()
    target_w, target_h = target_size
    evidence: dict = {"considered": 0, "rejected": [], "inspected": []}

    # Fresh URLs first, preserve order (search rank is itself a weak signal)
    urls = [u for u in candidate_urls if u and u not in already_used][:max_candidates]
    if not urls:
        return None, evidence

    loop = asyncio.get_running_loop()
    tmp_dir = Path(tempfile.mkdtemp(prefix="vd_"))
    inspected: list[tuple[float, str, Path, dict]] = []

    async def _one(i: int, url: str):
        ext = ".mp4" if kind == "video" else ".jpg"
        local = tmp_dir / f"cand_{i}{ext}"
        try:
            # Videos are big even at 1080p; 45s was too tight on real
            # home networks (TimeoutError stringifies empty, which hid it).
            await asyncio.wait_for(download_file(url, local), timeout=90 if kind == "video" else 45)
        except Exception as e:
            why = str(e) or type(e).__name__
            evidence["rejected"].append({"url": url, "why": f"download failed: {why[:60]}"})
            return
        m = (await loop.run_in_executor(None, inspect_video_quality, local)) if kind == "video" \
            else (await loop.run_in_executor(None, inspect_image_quality, local))
        evidence["considered"] += 1
        if not m.get("ok"):
            evidence["rejected"].append({"url": url, "why": "; ".join(m.get("reasons", []))})
            return
        if kind == "photo" and people:
            # FIX-095: subject visibility is part of quality — measure it
            # locally (cheap skin-tone mass probe) so the ranking prefers the
            # person the narration is about; Gemini still picks among the
            # finalists for narrative relevance.
            try:
                from utils.frame_occupancy import face_centre_y as _fcy
                m["face"] = _fcy(local) is not None
            except Exception:
                m["face"] = None
        score = _quality_score(m, target_w, target_h) + _face_score_adjust(m)
        evidence["inspected"].append({"url": url, "face": m.get("face"), **{k: m.get(k) for k in ("width", "height", "sharpness", "luma")}})
        inspected.append((score, url, local, m))

    await asyncio.gather(*[_one(i, u) for i, u in enumerate(urls)])
    if not inspected:
        return None, evidence

    inspected.sort(key=lambda t: -t[0])
    finalists = inspected[:4]
    gemini_pick = None
    if len(finalists) >= 2:
        # Video candidates can't ride to Gemini as raw MP4 bytes — extract a
        # representative mid-frame JPEG per finalist so the vision ranking
        # (match the visual to the words) works for b-roll too.
        rank_paths: list[Path] = []
        for _s, _u, local, _m in finalists:
            if kind == "video":
                frame = local.with_suffix(".rank.jpg")
                try:
                    import subprocess as _sp
                    _sp.run(
                        ["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.0",
                         "-i", str(local), "-frames:v", "1", "-q:v", "3", str(frame)],
                        capture_output=True, timeout=30,
                    )
                    rank_paths.append(frame if frame.exists() else local)
                except Exception:
                    rank_paths.append(local)
            else:
                rank_paths.append(local)
        gemini_pick = await rank_candidates_with_gemini(
            rank_paths, narration, mood, people)

    if gemini_pick is not None:
        idx, reason = gemini_pick
        chosen = finalists[idx]
        evidence["chosen_by"] = f"gemini: {reason}"
    else:
        chosen = finalists[0]
        evidence["chosen_by"] = "pixel-score (offline fallback)"

    best_score, best_url, best_path, best_m = chosen
    evidence["chosen_metrics"] = {k: best_m.get(k) for k in ("width", "height", "sharpness", "luma", "contrast")}
    log.info(
        f"  🎯 Visual selected ({evidence['chosen_by']}): {best_url[:70]}… "
        f"[{best_m.get('width')}x{best_m.get('height')} sharp={best_m.get('sharpness')}] "
        f"({len(evidence['rejected'])} rejected)"
    )
    return best_url, evidence


# ═══════════════════════════════════════════════════════════════
# 4. SUBJECT-AWARE FRAMING (saliency-aimed crops)
# ═══════════════════════════════════════════════════════════════

# ── Gemini-assisted framing (heavy reframes only, cached) ──

_FOCUS_CACHE: dict[str, tuple[float, float]] = {}
_FOCUS_PROMPT = (
    "This image will be cropped for a video frame. Locate the single most "
    "important subject (a face, a person, the key object). Reply with ONLY "
    "compact JSON: {{\"x\": <0.0-1.0 horizontal center>, \"y\": <0.0-1.0 vertical "
    "center>}}. Fractions of image width/height, origin top-left."
)


def gemini_focus(path: Path) -> tuple[float, float] | None:
    """Ask Gemini vision where the subject is. Cached per file; None on failure."""
    try:
        key = f"{path.resolve()}:{int(path.stat().st_mtime)}"
        if key in _FOCUS_CACHE:
            return _FOCUS_CACHE[key]

        import asyncio

        import config

        if not config.GEMINI_API_KEY or not path.exists():
            return None
        if path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            return None  # video subjects move; edge-scan is the honest tool there

        from google import genai
        from google.genai import types as genai_types

        parts = [
            {"inline_data": {"mime_type": "image/jpeg", "data": path.read_bytes()}},
            {"text": _FOCUS_PROMPT},
        ]
        contents = genai_types.Content(role="user", parts=parts)

        def _ask(model_id: str):
            client = genai.Client(api_key=config.GEMINI_API_KEY)
            return client.models.generate_content(
                model=model_id, contents=contents,
                config=genai_types.GenerateContentConfig(
                    temperature=0.0, max_output_tokens=60,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )

        import concurrent.futures as _cf
        for model_id in config.gemini_candidates():
            try:
                with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                    resp = _pool.submit(_ask, model_id).result(timeout=25)
                m = re.search(r"\{[^{}]*\}", resp.text or "")
                if not m:
                    continue
                data = json.loads(m.group(0))
                fx = float(data.get("x", 0.5))
                fy = float(data.get("y", 0.5))
                if 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
                    out = (min(max(fx, 0.15), 0.85), min(max(fy, 0.15), 0.85))
                    _FOCUS_CACHE[key] = out
                    return out
            except Exception:
                continue
        return None
    except Exception:
        return None


def saliency_focus(path: Path) -> tuple[float, float]:
    """Locate the visual subject of a still as (x, y) fractions in [0,1].

    Tries Gemini vision first (it actually recognizes WHO the subject is —
    the right call for celebrity stills); falls back to the cheap no-model
    saliency scan: edge energy plus color contrast, deterministic, fails
    to center.
    """
    g = gemini_focus(path)
    if g is not None:
        return g
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            im = im.resize((min(320, w), max(1, int(min(320, w) * h / w))))
            arr = np.asarray(im, dtype=np.float32)
        gray = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]

        gx = np.zeros_like(gray)
        gy = np.zeros_like(gray)
        gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
        gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
        edges = np.hypot(gx, gy)

        color_dist = np.abs(arr - arr.mean(axis=(0, 1))).mean(axis=2)
        sal = edges / (edges.mean() + 1e-6) + 0.5 * color_dist / (color_dist.mean() + 1e-6)

        # Soft window toward center: an editor frames the subject NEAR
        # center when everything else ties (rule-of-thirds anchor bias).
        hh, ww = sal.shape
        yy, xx = np.mgrid[0:hh, 0:ww]
        center_pull = 1.0 - 0.25 * (
            ((xx / ww - 0.5) ** 2 + (yy / hh - 0.5) ** 2)
        )
        sal = sal * center_pull

        total = sal.sum()
        if total <= 0:
            return (0.5, 0.5)
        cx = float((sal.sum(axis=0) * np.arange(ww)).sum() / total / ww)
        cy = float((sal.sum(axis=1) * np.arange(hh)).sum() / total / hh)
        # Clamp away from extremes — never frame a subject at the very edge
        return (min(max(cx, 0.22), 0.78), min(max(cy, 0.22), 0.78))
    except Exception:
        return (0.5, 0.5)


def crop_offsets_for_target(
    media_path: Path, width: int, height: int,
) -> tuple[float, float]:
    """Return ffmpeg crop x/y offsets that aim the crop at the subject.

    Usable directly in scale-then-crop chains: callers scale with
    force_original_aspect_ratio=increase and then crop(width,height,x,y)
    using these offsets (clamped by ffmpeg to valid ranges when passed as
    expressions).
    """
    fx, fy = saliency_focus(media_path)
    return (fx, fy)


# ═══════════════════════════════════════════════════════════════
# 5b. FULL-COVERAGE FLAT-FRAME SCAN (every frame of the final encode)
# ═══════════════════════════════════════════════════════════════

# A grey/blank frame at 35.5s shipped to the operator because sampled QC
# probes can straddle a sub-second defect. This scan decodes EVERY frame
# (10 fps probe resolution is enough — a flat card is flat at any rate)
# and flags any run of textureless frames ≥ FLAT_RUN_MIN seconds.
FLAT_STD = 8.0          # luma spread below this = one color
FLAT_GRADIENT = 1.2     # mean |horizontal gradient| below this = smooth void
FLAT_RUN_MIN = 0.25     # seconds; anything ≥ this is a rendering failure
HOOK_CARD_GRACE_S = 2.6  # kinetic title card zone: flatness is intentional


def scan_flat_frames(video_path: Path) -> list[dict]:
    """Decode the whole final encode and locate flat/blank runs.

    Returns [{'start': s, 'end': e, 'min_std': v}, ...] for every run of
    ≥ FLAT_RUN_MIN seconds whose frames are single-color or bare-gradient.
    Full coverage — sub-second defects cannot hide between samples.
    """
    try:
        import numpy as np

        W, H = 64, 48
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(video_path),
             "-vf", f"fps=10,scale={W}:-2,format=gray",
             "-f", "rawvideo", "-"],
            capture_output=True, timeout=600,
        )
        buf = r.stdout
        n = len(buf) // (W * H)
        if n < 10:
            return []
        frames = np.frombuffer(buf[: n * W * H], dtype=np.uint8).reshape(n, H, W).astype("float32")
        runs: list[dict] = []
        run = None
        for i, f in enumerate(frames):
            std = float(f.std())
            gx = float(np.abs(np.diff(f, axis=1)).mean())
            flat = std < FLAT_STD or gx < FLAT_GRADIENT
            t = i / 10.0
            if flat:
                if run is None:
                    run = {"start": t, "end": t, "min_std": std}
                else:
                    run["end"] = t
                    run["min_std"] = min(run["min_std"], std)
            else:
                if run is not None and run["end"] - run["start"] >= FLAT_RUN_MIN:
                    runs.append(run)
                run = None
        if run is not None and run["end"] - run["start"] >= FLAT_RUN_MIN:
            runs.append(run)
        return runs
    except Exception as e:
        log.warning(f"  flat-frame scan failed (non-fatal): {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# 5. FINAL QC PASS (inspect the ENTIRE rendered video)
# ═══════════════════════════════════════════════════════════════

# ── Multi-signal frame-information analysis ──
# One crude threshold fails legit cinematic simple backgrounds; these
# signals TOGETHER separate "intentionally minimal" from "unfinished".

EDGE_PIXEL_THRESHOLD = 8.0     # |laplacian| above this = textured pixel
EDGE_DENSITY_GRADIENT = 0.004  # below: almost no texture anywhere
LAP_GRADIENT_MAX = 14.0        # smooth gradation, no detail
STD_SOLID = 8.0                # luma spread this small = solid color
DARK_INVISIBLE = 14.0
BRIGHT_INVISIBLE = 242.0


def frame_information_score(gray) -> dict:
    """Classify a frame's visual information content (multi-signal).

    Returns {'label': str, ...metrics}. Labels:
      ok             — real photographic/footage content
      solid          — near-solid color card (Tier-4 fallback look)
      gradient       — smooth gradation, zero texture (fallback look)
      low_texture    — suspiciously empty; only damning in runs
      invisible_dark / invisible_bright — content effectively unreadable
    """
    import numpy as np

    g = gray.astype("float32")
    mean = float(g.mean())
    std = float(g.std())
    resp = (
        -4 * g[1:-1, 1:-1]
        + g[:-2, 1:-1] + g[2:, 1:-1]
        + g[1:-1, :-2] + g[1:-1, 2:]
    )
    lap_var = float(resp.var())
    edge_density = float((np.abs(resp) > EDGE_PIXEL_THRESHOLD).mean())

    label = "ok"
    if mean < DARK_INVISIBLE and std < 12.0:
        label = "invisible_dark"
    elif mean > BRIGHT_INVISIBLE and std < 12.0:
        label = "invisible_bright"
    elif std < STD_SOLID:
        label = "solid"
    elif lap_var < LAP_GRADIENT_MAX and edge_density < EDGE_DENSITY_GRADIENT:
        # Smooth variation with no textured detail anywhere: the rendered
        # gradient-card look. A real defocused background still carries
        # sensor noise + micro-detail; this does not.
        label = "gradient"
    elif edge_density < 0.01 and lap_var < 40.0:
        label = "low_texture"
    return {
        "label": label,
        "mean": round(mean, 1),
        "std": round(std, 1),
        "lap": round(lap_var, 1),
        "edge_density": round(edge_density, 4),
    }


def _scene_map(project_dir: Path) -> list[dict]:
    """Scene start/duration map from timeline.json (QC → scene attribution)."""
    try:
        t = json.loads((Path(project_dir) / "timeline.json").read_text(encoding="utf-8"))
        return [
            {"scene": s.get("scene_number"),
             "start": float(s.get("start_seconds") or 0),
             "dur": float(s.get("duration_seconds") or 0)}
            for s in t.get("scenes", [])
        ]
    except Exception:
        return []


def scene_at(t: float, smap: list[dict]) -> int | None:
    for s in smap:
        if s["start"] <= t < s["start"] + max(s["dur"], 0.5):
            return s.get("scene")
    return None


def _fallback_ledger(project_dir: Path) -> dict:
    """Scenes that shipped with a Tier-4 gradient fallback (assets.py marks)."""
    try:
        return json.loads((Path(project_dir) / "scenes" / "scene_fallback.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        return {}


_REVIEW_PROMPT = """You are a senior YouTube editor doing the final delivery QC on \
rendered frames. Frames come from one video; each is labeled with its scene number \
and what the narration says at that moment.

For EACH frame judge:
- Is it a real visual (footage/photo) or an unfinished placeholder (flat color card, \
bare gradient, empty backdrop)?
- Bad crop (subject cut off / face truncated), stretched or squashed image?
- Captions covering the subject's face or spilling outside the safe area?
- Does the visual actually match what the narration describes?
- Obvious AI artifacts (warped faces, melted hands, garbled text)?

Reply with ONLY compact JSON: {{"frames": [{{"i": <frame index>, "scene": <scene \
number>, "verdict": "ok" | "placeholder" | "bad_crop" | "caption_problem" | \
"mismatch" | "artifact", "note": "<max 10 words>"}}]}}"""


def gemini_frame_review(
    frames: list[tuple[int, int | None, Path]],
    narration_by_scene: dict[int, str] | None = None,
) -> list[dict] | None:
    """Gemini QC pass over sampled final-render frames.

    frames: [(sample_index, scene_number_or_None, jpeg_path), ...]
    Returns per-frame verdicts, or None when Gemini is unavailable (QC then
    relies on the deterministic signals alone). Non-fatal by design.
    """
    narration_by_scene = narration_by_scene or {}
    if len(frames) < 2:
        return None
    try:
        import config
        if not config.GEMINI_API_KEY:
            return None
        from google import genai
        from google.genai import types as genai_types

        lines = []
        for i, scene, path in frames:
            narr = (narration_by_scene.get(scene) or "(narration unavailable)")[:140]
            lines.append(f"frame {i} (scene {scene}): narration: {narr}")
        parts: list = [
            {"inline_data": {"mime_type": "image/jpeg", "data": p.read_bytes()}}
            for _, _, p in frames
        ]
        parts.append({"text": _REVIEW_PROMPT + "\n\n" + "\n".join(lines)})
        contents = genai_types.Content(role="user", parts=parts)

        for model_id in config.gemini_candidates():
            try:
                client = genai.Client(api_key=config.GEMINI_API_KEY)
                resp = client.models.generate_content(
                    model=model_id, contents=contents,
                    config=genai_types.GenerateContentConfig(
                        temperature=0.1, max_output_tokens=900,
                        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                    ),
                )
                m = re.search(r"\{.*\}", resp.text or "", re.S)
                if not m:
                    continue
                data = json.loads(m.group(0))
                out = []
                for f in data.get("frames", []):
                    try:
                        out.append({
                            "i": int(f.get("i", -1)),
                            "scene": f.get("scene"),
                            "verdict": str(f.get("verdict", "ok")),
                            "note": str(f.get("note", ""))[:100],
                        })
                    except Exception:
                        continue
                if out:
                    return out
            except Exception:
                continue
        return None
    except Exception:
        return None

def final_qc(
    video_path: Path,
    expected_size: tuple[int, int] | None = None,
    report_dir: Path | None = None,
    project_dir: Path | None = None,
    deep_review: bool = True,
) -> tuple[bool, list[str]]:
    """Inspect the finished render like an editor doing the delivery check.

    Hard failures (wrong AR, broken/black stretches, frozen tail, missing
    audio, dangerously quiet or loud mix) return False with the issue list —
    callers block upload on False exactly like the compliance gate.

    With project_dir set, findings are attributed to scenes (timeline.json),
    Tier-4 fallback scenes are cross-checked against the ledger, and Gemini
    does an editorial review of sampled frames (placeholders, crops,
    caption placement, narration match, AI artifacts).
    """
    issues: list[str] = []
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(video_path)],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(p.stdout or "{}")
        vs = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        auds = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        if not vs:
            return False, ["QC: no video stream in final render"]
        w, h = int(vs.get("width") or 0), int(vs.get("height") or 0)
        dur = float(data.get("format", {}).get("duration") or 0)

        if expected_size and (w, h) != expected_size:
            issues.append(f"QC: resolution {w}x{h} != expected {expected_size[0]}x{expected_size[1]}")
        if dur < 5.0:
            issues.append(f"QC: render suspiciously short ({dur:.1f}s)")
        # FIX-060 enforcement point: vertical (9:16) renders must fit the
        # Shorts limit — check the EXPORTED file, not the script estimate.
        if expected_size and expected_size[1] > expected_size[0] and dur > 59.5:
            issues.append(
                f"QC: CRITICAL shorts duration {dur:.1f}s exceeds the 60s "
                f"limit — trim/re-render, upload would be non-compliant")

        if expected_size:
            exp_ar = expected_size[0] / expected_size[1]
            got_ar = w / max(h, 1)
            if abs(exp_ar - got_ar) > 0.02:
                issues.append(f"QC: aspect ratio drift (got {got_ar:.3f}, want {exp_ar:.3f})")

        # ── Frame grid: multi-signal content analysis per sampled frame ──
        import numpy as np

        n = min(24, max(8, int(dur / 10)))
        smap = _scene_map(project_dir) if project_dir else []
        # Sample times: uniform grid PLUS every scene's midpoint. The grid
        # alone had a blind spot — on an 81.6s/12-scene short its last
        # sample landed at 76.5s and a 5s solid-placeholder FINALE shipped
        # unseen. A scene the grid never touches must still be inspected.
        sample_times = [dur * (i + 0.5) / n for i in range(n)]
        if smap:
            for s in smap:
                # FIX-098: the scene map comes from the PLAN, so its tail can
                # run past the exported file (audio re-mux / tail trim).
                # Probing past the render returns no frame and used to be
                # reported as a critical "frame undecodable" — on 2026-10-05
                # two shorts were rejected on t=46s/44s against 45.3s/43.6s
                # renders that were actually fine. Only midpoints that exist
                # inside the exported run are sampled.
                if s["dur"] >= 1.0:
                    mid = s["start"] + s["dur"] / 2
                    if mid <= dur - 0.5 and all(abs(mid - t) > 1.2
                                                for t in sample_times):
                        sample_times.append(mid)
            sample_times.sort()
            sample_times = sample_times[:36]  # bounded for the review pass
        prev_sig = None
        frozen_run = 0
        max_frozen = 0
        black_frames = 0
        placeholder_frames: dict[str, list[dict]] = {}
        frame_records: list[dict] = []
        sharp_samples = []
        sample_jpegs: list[tuple[int, int | None, Path]] = []
        tmp_dir = video_path.parent / "_qc_frames"
        try:
            tmp_dir.mkdir(exist_ok=True)
        except Exception:
            tmp_dir = None
        for i, t in enumerate(sample_times):
            # Sample as JPEG once; derive every signal from the same frame.
            jpeg: Path | None = None
            if tmp_dir is not None:
                jpeg = tmp_dir / f"qc_{i:02d}_s{scene_at(t, smap)}.jpg"
                try:
                    subprocess.run(
                        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
                         "-i", str(video_path), "-frames:v", "1", "-q:v", "4", str(jpeg)],
                        capture_output=True, timeout=30,
                    )
                    if not jpeg.exists():
                        jpeg = None
                except Exception:
                    jpeg = None
            if jpeg is not None:
                try:
                    from PIL import Image
                    with Image.open(jpeg) as im:
                        im8 = im.convert("L").resize((480, int(480 * im.height / im.width) or 1))
                        gray = np.asarray(im8)
                except Exception:
                    gray = _frame_pixels(video_path, t, width=480)
            else:
                gray = _frame_pixels(video_path, t, width=480)
            if gray is None:
                issues.append(f"QC: frame at {t:.0f}s undecodable")
                continue
            mean = float(gray.mean())
            if mean < 6.0:
                black_frames += 1
            sig = gray[::8, ::8].astype("int16").tobytes()
            if prev_sig is not None and sig == prev_sig:
                frozen_run += 1
                max_frozen = max(max_frozen, frozen_run)
            else:
                frozen_run = 0
            prev_sig = sig
            info = frame_information_score(gray)
            sc = scene_at(t, smap) if smap else None
            frame_records.append({"t": round(t, 1), "scene": sc, **info})
            if info["label"] in ("solid", "gradient", "invisible_dark", "invisible_bright"):
                placeholder_frames.setdefault(info["label"], []).append(frame_records[-1])
            elif info["label"] == "low_texture":
                placeholder_frames.setdefault("low_texture", []).append(frame_records[-1])
            sharp_samples.append(info["lap"])
            if jpeg is not None:
                sample_jpegs.append((i, sc, jpeg))
        if black_frames >= max(3, n // 6):
            issues.append(f"QC: {black_frames}/{n} sampled frames are near-black")
        if max_frozen >= max(3, n // 4):
            issues.append(f"QC: {max_frozen} consecutive frozen frames (stuck shot)")

        # ── Placeholder / fallback detection (multi-signal, run-aware) ──
        hard_labels = ("solid", "gradient", "invisible_dark", "invisible_bright")
        bad_scenes: dict[int | None, str] = {}
        for label, frames_ in placeholder_frames.items():
            if label in hard_labels:
                for fr in frames_:
                    bad_scenes.setdefault(fr.get("scene"), label)
            elif label == "low_texture" and len(frames_) >= 2:
                # Textureless only damns when it repeats — a single minimal
                # frame can be intentional cinematic simplicity.
                scenes_hit = {fr.get("scene") for fr in frames_}
                if len(scenes_hit) >= 2 or len(frames_) >= 3:
                    for fr in frames_:
                        bad_scenes.setdefault(fr.get("scene"), label)
        # Cross-check against the Tier-4 fallback ledger: a scene that the
        # asset pass marked FALLBACK_USED must not silently ship.
        ledger = _fallback_ledger(project_dir) if project_dir else {}
        for scene_str, meta in (ledger or {}).items():
            try:
                scn = int(scene_str)
            except Exception:
                continue
            bad_scenes.setdefault(scn, "fallback_ledger")
        for scn, label in sorted(bad_scenes.items(), key=lambda kv: (kv[0] is None, kv[0])):
            scenes_txt = f"scene {scn}" if scn is not None else "unattributed segment"
            if label == "fallback_ledger":
                issues.append(
                    f"QC: {scenes_txt} shipped with the Tier-4 gradient "
                    f"FALLBACK_USED — not visually complete; re-fetch on resume")
            else:
                issues.append(
                    f"QC: {scenes_txt} is visually unfinished ({label} frames) — "
                    f"replace the asset (resume re-fetches it)")

        # ── FULL-COVERAGE flat-frame scan of the FINAL ENCODE (hard gate) ──
        # Sub-second grey/blank runs must fail QC outright. The kinetic
        # hook card gets a grace window: its flatness is an intentional
        # title design, not a rendering failure.
        flat_runs = [r for r in scan_flat_frames(video_path)
                     if r["start"] > HOOK_CARD_GRACE_S]
        for r in flat_runs:
            scn = scene_at((r["start"] + r["end"]) / 2, smap) if smap else None
            issues.append(
                f"QC: CRITICAL flat/blank frames {r['start']:.2f}s-"
                f"{r['end']:.2f}s (scene {scn}) — rendering failure, not shippable")

        # ── FIX-059: celebrity visual variety — heavy presence of one person
        # across many scenes is the AI-slideshow fingerprint. WARNING-level:
        # the celebrity IS the story, so this is an editorial review signal
        # for the operator, never a hard gate.
        if project_dir:
            try:
                vstate = json.loads(
                    (Path(project_dir) / "scene_fingerprint.json").read_text(encoding="utf-8"))
                by_person: dict[str, list[int]] = {}
                for scn_str, rec in (vstate.get("scenes") or {}).items():
                    key = rec.get("person_key")
                    if key:
                        try:
                            by_person.setdefault(key, []).append(int(scn_str))
                        except Exception:
                            continue
                for key, scns in sorted(by_person.items()):
                    if len(scns) > 3:
                        issues.append(
                            f"QC: person '{key}' carries {len(scns)} scenes "
                            f"({scns}) — review visual variety before publishing")
            except Exception:
                pass

        # ── Gemini editorial review of the actual render ──
        if deep_review and project_dir and len(sample_jpegs) >= 2:
            narration_by_scene: dict[int, str] = {}
            try:
                scr = json.loads((Path(project_dir) / "script.json").read_text(encoding="utf-8"))
                for s in scr.get("scenes", []):
                    try:
                        narration_by_scene[int(s.get("scene_number"))] = str(s.get("narration", ""))
                    except Exception:
                        continue
            except Exception:
                pass
            # Cap the review set (Gemini context): spread over the video
            review_set = sample_jpegs[:24]
            review = gemini_frame_review(review_set, narration_by_scene)
            if review:
                for r in review:
                    v = r.get("verdict", "ok")
                    if v and v != "ok":
                        scn = r.get("scene")
                        scenes_txt = f"scene {scn}" if scn is not None else "unattributed"
                        issues.append(
                            f"QC: {scenes_txt} frame review: {v} — {r.get('note', '')}")
        med_sharp = statistics.median(sharp_samples) if sharp_samples else 0.0
        if med_sharp < SHARPNESS_FLOOR * 0.8:
            issues.append(f"QC: rendered frames soft (median sharpness {med_sharp:.0f})")

        # ── Audio truth: is there a voice-usable track at sane loudness? ──
        if not auds:
            issues.append("QC: final render has NO audio track")
        else:
            try:
                r = subprocess.run(
                    ["ffmpeg", "-v", "info", "-i", str(video_path),
                     "-map", "0:a:0", "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                    capture_output=True, text=True, timeout=120,
                )
                m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr or "")
                if m:
                    ln = json.loads(m.group(0))
                    i_lu = float(ln.get("input_i", -70))
                    if i_lu < -42:
                        issues.append(f"QC: mix nearly silent ({i_lu:.0f} LUFS)")
                    elif i_lu > -9:
                        issues.append(f"QC: mix dangerously hot ({i_lu:.0f} LUFS) — clipping risk")
                else:
                    log.info("  QC: loudness measurement unavailable (skipped)")
            except Exception:
                log.info("  QC: loudness probe failed (skipped)")

        # ── FIX-094: dead-air tail — did narration stop before the picture? ──
        # The silent-scene defect ships as a digital-silence tail (measured:
        # -91 dB vs -17 dB mid-video) while captions keep running. On every
        # healthy render the voice runs to the cut, so compare the last
        # window against the body; a large drop means the audio died early.
        if dur >= 8.0 and auds:
            try:
                tail_win = min(3.0, max(1.5, dur / 4.0))

                def _mean_db(start: float) -> float | None:
                    pr = subprocess.run(
                        ["ffmpeg", "-v", "info", "-ss", f"{start:.2f}",
                         "-t", f"{tail_win:.2f}", "-i", str(video_path),
                         "-map", "0:a:0", "-af", "volumedetect",
                         "-f", "null", "-"],
                        capture_output=True, text=True, timeout=60,
                    )
                    mm = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", pr.stderr or "")
                    return float(mm.group(1)) if mm else None

                tail_db = _mean_db(max(0.0, dur - tail_win))
                body_db = _mean_db(max(0.0, dur / 2 - tail_win / 2))
                if (tail_db is not None and body_db is not None
                        and tail_db < body_db - 8.0):
                    issues.append(
                        f"QC: CRITICAL dead-air tail — last {tail_win:.1f}s "
                        f"average {tail_db:.0f} dB vs {body_db:.0f} dB "
                        f"mid-video: narration stops before the picture "
                        f"(silent-scene defect)")
            except Exception:
                log.info("  QC: tail-silence probe failed (skipped)")

        ok = not issues
        if report_dir:
            try:
                report_dir.mkdir(parents=True, exist_ok=True)
                (report_dir / "final_qc_report.json").write_text(json.dumps({
                    "video": str(video_path),
                    "resolution": f"{w}x{h}",
                    "duration": round(dur, 2),
                    "frames_sampled": n,
                    "black_frames": black_frames,
                    "max_frozen_run": max_frozen,
                    "placeholder_frames": {k: len(v) for k, v in placeholder_frames.items()},
                    "unfinished_scenes": {str(k): v for k, v in bad_scenes.items()},
                    "frame_details": frame_records,
                    "median_sharpness": round(med_sharp, 1),
                    "issues": issues,
                    # FIX-059: report the operator truth — warn-only renders
                    # are SHIPPABLE (gate passes them; unlisted review is the
                    # backstop). "FAIL" must mean criticals, not warnings.
                    "verdict": ("PASS" if ok else
                                ("WARN" if not [i for i in issues
                                                 if any(p in i for p in CRITICAL_PATTERNS)]
                                 else "FAIL")),
                }, indent=2), encoding="utf-8")
            except Exception:
                pass
        return ok, issues
    except Exception as e:
        return False, [f"QC: crashed while inspecting render: {e}"]


# ═══════════════════════════════════════════════════════════════
# 6. QC GATE (the do-not-ship wrapper callers use)
# ═══════════════════════════════════════════════════════════════

# Objective breakage — a rebuild is mandatory and shipping is blocked when
# it persists. Cosmetic findings (softness, dark stretches) only warn:
# every upload lands UNLISTED for operator review anyway.
CRITICAL_PATTERNS = (
    "no video stream", "resolution", "aspect ratio", "NO audio",
    "suspiciously short", "undecodable", "crashed", "CRITICAL flat/blank",
    "rendering failure", "CRITICAL shorts duration", "dead-air tail",
)


def classify_qc(issues: list[str]) -> tuple[list[str], list[str]]:
    crit = [i for i in issues if any(p in i for p in CRITICAL_PATTERNS)]
    warn = [i for i in issues if i not in crit]
    return crit, warn


def _write_caption_band_overrides(project_dir: Path, scenes: list[int]) -> dict[int, str]:
    """FIX-062: fix a caption-frame problem by MOVING the caption band.

    The repair loop used to treat "caption obscures subject face" as a visual
    problem and re-selected the scene's asset — which never touched caption
    placement, so the defect survived the repair and the re-render. Placement
    is the actual variable: flip the scene's band (bottom↔top) unless the
    frame analysis clearly points the other way. The next render consumes the
    overrides file, so the fix lands in the picture.
    """
    overrides: dict[int, str] = {}
    temp_dir = Path(project_dir) / "_temp"
    path = temp_dir / "caption_band_overrides.json"
    if path.exists():
        try:
            overrides = {int(k): str(v) for k, v in
                         (json.loads(path.read_text(encoding="utf-8")) or {}).items()}
        except Exception:
            overrides = {}
    try:
        from utils.frame_occupancy import caption_band
    except Exception:
        caption_band = None  # type: ignore[assignment]
    # Prepared scene clips live in edit_media/ (the _temp/ copy is transient),
    # so search both before giving up on the frame analysis.
    search_dirs = [Path(project_dir) / "edit_media", temp_dir]
    for n in scenes:
        clip = None
        for d in search_dirs:
            for pattern in (f"scene_{n:02d}_prepared.*.mp4", f"scene_{n:02d}_prepared*.mp4"):
                hits = sorted(d.glob(pattern)) if d.exists() else []
                if hits:
                    clip = hits[0]
                    break
            if clip is not None:
                break
        rendered = overrides.get(n, "bottom")
        want = rendered
        if clip is not None and caption_band is not None:
            try:
                want = caption_band(clip, at=0.6)
            except Exception:
                want = rendered
        if want == rendered:      # frame analysis agrees with what was burned
            want = "bottom" if rendered == "top" else "top"
        overrides[n] = want
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(overrides, indent=2), encoding="utf-8")
    except Exception:
        pass
    return overrides


def qc_gate(
    video_path: Path,
    expected_size: tuple[int, int] | None = None,
    rebuild=None,
    report_dir: Path | None = None,
    project_dir: Path | None = None,
    repair=None,
) -> tuple[Path, bool, list[str]]:
    """Inspect the render; on critical failure rebuild ONCE and re-check.

    Returns (video_path, shippable, all_issues). Warnings never block
    (unlisted review is the backstop); persistent criticals do.

    With project_dir set, QC runs the deep review (scene attribution,
    fallback ledger, Gemini frame review). With repair set (an async
    callable taking the flagged scene numbers), visual-unfinished findings
    trigger the DETECT → RE-SELECT → RE-RENDER loop once before the render
    rebuild — fallback assets are temporary, never a final state.
    """
    video_path = Path(video_path)
    ok, issues = final_qc(video_path, expected_size, report_dir, project_dir=project_dir)
    if ok:
        log.info(f"  ✅ Final QC PASS: {video_path.name}")
        return video_path, True, []
    crit, warn = classify_qc(issues)
    for w in warn:
        log.warning(f"  ⚠️ QC warning: {w}")

    # Visual-unfinished findings (placeholder/fallback/placeholder-review)
    # are fixable by re-selecting the scene's asset — try that BEFORE the
    # full rebuild, then rebuild the flagged scenes into the render.
    unfinished = [i for i in issues if "visually unfinished" in i
                  or "FALLBACK_USED" in i or "frame review:" in i]

    # FIX-062: a caption-frame finding is a PLACEMENT problem. Move that
    # scene's caption band (the rebuild below picks the overrides up) instead
    # of burning quota re-selecting a visual that was never the cause.
    caption_scenes = sorted({
        int(m.group(1)) for i in unfinished
        if "caption" in i.lower() and (m := re.search(r"scene (\d+)", i))
    })
    if caption_scenes and project_dir is not None:
        moved = _write_caption_band_overrides(Path(project_dir), caption_scenes)
        log.warning(f"  🔧 QC repair: caption band moved for scenes {caption_scenes} → {moved}")

    visual_unfinished = [i for i in unfinished if "caption_problem" not in i.lower()]
    if visual_unfinished and repair is not None and project_dir is not None:
        scene_numbers = sorted({
            int(m.group(1)) for i in visual_unfinished
            if (m := re.search(r"scene (\d+)", i))
        })
        if scene_numbers:
            log.warning(
                f"  🔧 QC repair loop: re-selecting scenes {scene_numbers}…")
            try:
                import asyncio as _asyncio

                from pipeline.assets import repair_scene_assets

                async def _run_repair():
                    return await repair_scene_assets(
                        repair["script"], repair["template"],
                        Path(project_dir), scene_numbers,
                    )
                try:
                    _loop = _asyncio.get_running_loop()
                    fut = _asyncio.run_coroutine_threadsafe(_run_repair(), _loop)
                    repaired_n = fut.result(timeout=420)
                except RuntimeError:
                    repaired_n = _asyncio.run(_run_repair())
                log.info(f"  🔧 Repair re-secured {repaired_n}/{len(scene_numbers)} scenes")
            except Exception as e:
                log.warning(f"  Repair pass failed: {str(e)[:140]}")

    if not crit and not unfinished:
        return video_path, True, issues
    log.warning(f"  🔴 QC failing ({len(crit)} critical / {len(unfinished)} unfinished) — attempting one rebuild")
    if rebuild is not None:
        try:
            rebuilt = rebuild()
        except Exception as e:
            log.error(f"  QC rebuild crashed: {e}")
            rebuilt = None
        if rebuilt and Path(rebuilt).exists():
            ok2, issues2 = final_qc(Path(rebuilt), expected_size, report_dir, project_dir=project_dir,
                                    deep_review=False)
            crit2, warn2 = classify_qc(issues2)
            unfinished2 = [i for i in issues2 if "visually unfinished" in i
                           or "FALLBACK_USED" in i or "frame review:" in i]
            # FIX-068: after a successful rebuild, pass when nothing critical
            # or unfinished remains. Advisory warnings (e.g. "person 'X'
            # carries N scenes — review visual variety before publishing")
            # are publish-review notes, not render defects — final_qc's raw ok
            # flag counts them as failures, which aborted an otherwise clean
            # upload. Same rule the pre-rebuild branch applies.
            if not crit2 and not unfinished2:
                log.info("  ✅ QC PASS after rebuild")
                return Path(rebuilt), True, []
            log.warning(f"  🔴 QC still failing after rebuild: {len(crit2)} critical / {len(unfinished2)} unfinished")
            return Path(rebuilt), False, issues2
    return video_path, False, issues


# ═══════════════════════════════════════════════════════════════
# 7. SALIENCY FOR VIDEO SOURCES (aim crops in real footage too)
# ═══════════════════════════════════════════════════════════════

def saliency_focus_video(path: Path) -> tuple[float, float]:
    """saliency_focus() for a video file: decode a mid-frame, same math."""
    gray = _frame_pixels(path, None, width=320)
    if gray is None:
        return (0.5, 0.5)
    try:
        import numpy as np

        g = gray.astype("float32")
        gx = np.zeros_like(g)
        gy = np.zeros_like(g)
        gx[:, 1:-1] = g[:, 2:] - g[:, :-2]
        gy[1:-1, :] = g[2:, :] - g[:-2, :]
        edges = np.hypot(gx, gy)
        sal = edges / (edges.mean() + 1e-6)
        hh, ww = sal.shape
        yy, xx = np.mgrid[0:hh, 0:ww]
        center_pull = 1.0 - 0.25 * (((xx / ww - 0.5) ** 2 + (yy / hh - 0.5) ** 2))
        sal = sal * center_pull
        total = sal.sum()
        if total <= 0:
            return (0.5, 0.5)
        cx = float((sal.sum(axis=0) * np.arange(ww)).sum() / total / ww)
        cy = float((sal.sum(axis=1) * np.arange(hh)).sum() / total / hh)
        return (min(max(cx, 0.22), 0.78), min(max(cy, 0.22), 0.78))
    except Exception:
        return (0.5, 0.5)
