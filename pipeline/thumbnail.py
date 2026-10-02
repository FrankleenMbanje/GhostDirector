"""
GhostDirector — Thumbnail Generator (Pro)

Creates high-CTR YouTube thumbnails:
  - Clear subject face (NOT blurred)
  - Bold text with colored outline
  - Half-gradient overlay for text contrast
  - Emotion/drama emphasis
"""

import sys
import json
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops, ImageEnhance
from models import Script
import config
from utils.logger import get_logger
from pipeline.thumbnail_styles import recommend_styles, load_user_templates  # A9a

log = get_logger("thumbnail")

# Small words that never identify a subject when matching people_to_show
# against the video title ("The Rise and Fall of Sean Diddy Combs").
_TITLE_STOPWORDS = {
    "the", "a", "an", "and", "of", "in", "on", "at", "to", "for",
    "his", "her", "their", "its", "rise", "fall", "story", "untold",
    "truth", "secret", "life", "inside", "real", "why", "how", "what",
}


def _subject_tokens(title: str) -> set[str]:
    """Content words of the video title, lower-cased ("sean", "diddy", "combs")."""
    words = re.findall(r"[A-Za-z]+", title or "")
    return {w.lower() for w in words if len(w) > 2 and w.lower() not in _TITLE_STOPWORDS}


def _pick_subject_scene(script: Script) -> int | None:
    """Index of the first scene whose people_to_show matches the title subject.

    A28: the thumbnail must show WHO the video is about. The old heuristic took
    the first photo ≥400×300, which could be B-roll b-roll (a courtroom, a
    skyline) while the subject's face never appeared. Falls back to None and
    the caller keeps its existing first-photo logic.
    """
    subj = _subject_tokens(getattr(script, "title", "") or "")
    if not subj:
        return None
    for i, scene in enumerate(script.scenes):
        for person in scene.people_to_show or []:
            person_words = {w.lower() for w in re.findall(r"[A-Za-z]+", person) if len(w) > 2}
            if person_words & subj and scene.photo_path and Path(scene.photo_path).exists():
                return i
    return None


def _make_gradient_overlay(width: int, height: int, rgb: tuple[int, int, int],
                           max_alpha: int, exponent: float = 1.35) -> Image.Image:
    """Left-dark → right-transparent gradient, vectorized with numpy.

    The previous putpixel loop touched every pixel of a 1280x720 RGBA image
    (~3.7M calls with the per-concept re-render); this runs once per image in
    C speed (~ms).
    """
    xs = np.arange(width, dtype=np.float32)
    alpha_row = (max_alpha * ((width - xs) / width) ** exponent).astype(np.uint8)
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[..., 0] = rgb[0]
    rgba[..., 1] = rgb[1]
    rgba[..., 2] = rgb[2]
    rgba[..., 3] = alpha_row[None, :]  # broadcast: constant down columns
    return Image.fromarray(rgba, mode="RGBA")


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    font_path = config.FONTS_DIR / "Montserrat-Bold.ttf"
    if font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = font.getbbox(test)
        if (bbox[2] - bbox[0]) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def generate_thumbnail(script: Script, template: dict, output_dir: Path) -> Path:
    """
    Generate a pro YouTube thumbnail.

    Strategy:
    1. Use the subject's photo as the background (clear, NOT blurred)
    2. Apply a left-to-right gradient so the text side has contrast
    3. Bold, large text with colored stroke
    4. Slight vignette and contrast boost for cinematic feel
    """
    log.info("Generating thumbnail...")

    thumb_cfg = template.get("thumbnail", {})
    width = config.THUMBNAIL_WIDTH
    height = config.THUMBNAIL_HEIGHT
    font_size = thumb_cfg.get("font_size", 96)
    color_overlay = thumb_cfg.get("color_overlay", "#FF0000")
    text_position = thumb_cfg.get("text_position", "right")

    # ── Find the best background ──
    bg_image = None
    scenes_dir = output_dir / "scenes"

    # Priority 1: a photo of the video's SUBJECT (A28) — the person the title
    # names. A courtroom B-roll shot in slot 1 used to win simply by arriving
    # first; a thumbnail without the subject's face leaks CTR.
    subject_idx = _pick_subject_scene(script)
    if subject_idx is not None:
        try:
            img = Image.open(script.scenes[subject_idx].photo_path)
            if img.size[0] >= 400 and img.size[1] >= 300:
                bg_image = img
                log.info(f"Thumbnail background: subject photo from scene {subject_idx + 1} "
                         f"({script.scenes[subject_idx].people_to_show})")
        except Exception:
            bg_image = None

    # Priority 2: highest-resolution CLEAN scene photo (FIX-049/050):
    # photos sourced from watermarked stock agencies are skipped outright,
    # and among clean candidates the LARGEST wins — the old code took the
    # first ≥400px photo regardless of quality.
    if bg_image is None:
        from pipeline.assets import is_watermarked_source
        best_img, best_px = None, 0
        for scene in script.scenes:
            if scene.photo_path and Path(scene.photo_path).exists() \
                    and not is_watermarked_source(scene.source_url):
                try:
                    with Image.open(scene.photo_path) as img:
                        px = img.size[0] * img.size[1]
                        if px > best_px and img.size[0] >= 400 and img.size[1] >= 300:
                            best_img = Image.open(scene.photo_path)
                            best_px = px
                except Exception:
                    continue
        if best_img is not None:
            bg_image = best_img

    # Fallback: extract a frame from the first video
    if bg_image is None:
        for scene in script.scenes:
            if scene.video_path and Path(scene.video_path).exists():
                frame_path = output_dir / "_thumb_frame.jpg"
                from utils.ffmpeg_cmd import run_ffmpeg
                try:
                    run_ffmpeg(
                        [
                            "-ss", "2",  # Skip 2 seconds to avoid black frames
                            "-i", str(scene.video_path),
                            "-vframes", "1", "-q:v", "2",
                            str(frame_path),
                        ],
                        "Extract thumbnail frame"
                    )
                except Exception as ff_e:
                    log.warning(f"Failed to extract frame for thumbnail: {ff_e}")
                if frame_path.exists():
                    bg_image = Image.open(frame_path)
                    break

    # Last resort
    if bg_image is None:
        bg_image = Image.new("RGB", (width, height), (20, 20, 35))

    # ── Process background ──
    bg_image = bg_image.convert("RGB")
    # FIX-050: cover-crop instead of stretch. The old resize() forced every
    # source to 16:9, squashing portraits sideways. Cover-crop scales to
    # fill and center-crops the overflow, biased toward the TOP of portrait
    # sources where faces live.
    src_w, src_h = bg_image.size
    target_ratio = width / float(height)
    src_ratio = src_w / float(src_h)
    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        x0 = (src_w - new_w) // 2
        bg_image = bg_image.crop((x0, 0, x0 + new_w, src_h))
    elif src_ratio < target_ratio:
        new_h = int(src_w / target_ratio)
        y0 = max(0, int((src_h - new_h) * 0.22))
        bg_image = bg_image.crop((0, y0, src_w, y0 + new_h))
    bg_image = bg_image.resize((width, height), Image.Resampling.LANCZOS)

    # Enforce left-anchored text to completely prevent YouTube duration timestamp clash in bottom-right
    text_position = thumb_cfg.get("text_position", "left")

    # Boost contrast and saturation slightly for pop
    bg_image = ImageEnhance.Contrast(bg_image).enhance(1.18)
    bg_image = ImageEnhance.Color(bg_image).enhance(1.22)

    # ── Apply half-gradient overlay on the text side (LEFT side) ──
    overlay_r = int(color_overlay.lstrip("#")[:2], 16)
    overlay_g = int(color_overlay.lstrip("#")[2:4], 16)
    overlay_b = int(color_overlay.lstrip("#")[4:6], 16)

    # Darkest on left (for text readability), fading to transparent on right
    # (subject face). Vectorized once per render — the old per-pixel loop was
    # ~3.7M putpixel calls across the base + 3 concept re-renders.
    gradient = _make_gradient_overlay(width, height, (10, 10, 15), max_alpha=230, exponent=1.4)

    bg_rgba = bg_image.convert("RGBA")
    bg_rgba = Image.alpha_composite(bg_rgba, gradient)
    bg_image = bg_rgba.convert("RGB")

    # ── Subtle vignette ──
    vignette = Image.new("L", (width, height), 255)
    v_draw = ImageDraw.Draw(vignette)
    cx, cy = width // 2, height // 2
    max_r = (cx**2 + cy**2) ** 0.5
    for r in range(int(max_r), 0, -4):
        brightness = max(0, min(255, int(255 - 90 * (r / max_r) ** 3)))
        v_draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=brightness)
    r_ch, g_ch, b_ch = bg_image.split()
    r_out = ImageChops.multiply(r_ch, vignette)
    g_out = ImageChops.multiply(g_ch, vignette)
    b_out = ImageChops.multiply(b_ch, vignette)
    bg_image = Image.merge("RGB", (r_out, g_out, b_out))

    # ── Power Hook Typography (Top/Center-Left Anchored) ──
    draw = ImageDraw.Draw(bg_image)
    power_font = _get_font(int(font_size * 1.15))

    # Generate a punchy 2-4 word power hook for the thumbnail text. A28:
    # prefer the script's hook_overlay_text — Gemini already writes a 5-7
    # word muted-autoplay hook aligned with the packaging — and fall back to
    # impact words from the title.
    overlay_text = (getattr(script, "hook_overlay_text", None) or "").strip()
    if overlay_text:
        power_words = overlay_text.upper().split()[:4]
    else:
        raw_title = (script.title or "THE UNTOLD STORY").upper().replace(":", "").replace("-", "")
        all_words = raw_title.split()
        if len(all_words) <= 4:
            power_words = all_words
        else:
            # Pick 3 highest impact words
            stopwords = {"A", "THE", "IN", "OF", "ON", "AT", "TO", "FOR", "AND", "IS", "IT"}
            meaningful = [w for w in all_words if w not in stopwords]
            power_words = meaningful[:3] if len(meaningful) >= 3 else all_words[:3]

    # ── Render Concept 1: Subject Focal + Left Power Typography ──
    v1_img = _render_concept_focal(bg_image.copy(), power_words, font_size, width, height)
    v1_path = output_dir / "thumbnail_v1_focal.png"
    v1_img.save(str(v1_path), "PNG", quality=95)

    # ── Render Concept 2: High-Contrast Accent Box / Pill ──
    v2_img = _render_concept_split_box(bg_image.copy(), power_words, font_size, width, height)
    v2_path = output_dir / "thumbnail_v2_split.png"
    v2_img.save(str(v2_path), "PNG", quality=95)

    # ── Render Concept 3: Dark Vignette Minimal Statement + Accent Bar ──
    v3_img = _render_concept_minimal(bg_image.copy(), power_words, font_size, width, height)
    v3_path = output_dir / "thumbnail_v3_minimal.png"
    v3_img.save(str(v3_path), "PNG", quality=95)

    # ── A9: Style-library variants (user templates first, then curated) ──
    # ── EXCLUSIVE kit: stash the CLEAN subject photo + a second face so the
    # exclusive_news painter builds its split from raw photos (the graded
    # bg has a baked-in gradient that poisons the right half).
    global _EXCLUSIVE_KIT
    _EXCLUSIVE_KIT = {"primary": None, "secondary": None}
    subject_idx = _pick_subject_scene(script)
    if subject_idx is not None and script.scenes[subject_idx].photo_path:
        _EXCLUSIVE_KIT["primary"] = script.scenes[subject_idx].photo_path
    _EXCLUSIVE_KIT["secondary"] = _pick_secondary_face(script, subject_idx, output_dir)

    style_rows = render_style_variants(
        bg_image.copy(), power_words, font_size, width, height, output_dir,
        hook_text=(getattr(script, "hook_overlay_text", None) or "") + " " + (script.title or ""),
        niche=thumb_cfg.get("niche", config.CHANNEL_NICHE),
        preferred_styles=thumb_cfg.get("preferred_styles") or None,
    )

    # Primary thumbnail for backward compatibility. When the template names
    # preferred_styles, that style SHIPS as the primary (the operator's look
    # beats the v1 default); the full matrix stays on disk for A/B swaps.
    primary_img = v1_img
    preferred = thumb_cfg.get("preferred_styles") or []
    if preferred:
        row = next((r for r in style_rows if r.get("id") == preferred[0]), None)
        if row and (output_dir / row["file"]).exists():
            try:
                primary_img = Image.open(output_dir / row["file"]).convert("RGB")
                log.info(f"Primary thumbnail promoted: {preferred[0]} style")
            except Exception as e:
                log.warning(f"preferred style load failed ({e}); keeping v1")
    thumb_path = output_dir / "thumbnail.png"
    primary_img.save(str(thumb_path), "PNG", quality=95)

    # Save Thumbnail Concepts metadata brief
    concepts_spec = {
        "concepts": [
            {
                "variant": "v1_focal",
                "file": "thumbnail_v1_focal.png",
                "approach": "Focal subject with left-anchored power typography and electric yellow accent",
                "paired_title_type": "curiosity_gap",
            },
            {
                "variant": "v2_split",
                "file": "thumbnail_v2_split.png",
                "approach": "High-contrast documentary pill boxes with bold punchy hook",
                "paired_title_type": "direct_benefit",
            },
            {
                "variant": "v3_minimal",
                "file": "thumbnail_v3_minimal.png",
                "approach": "Deep vignette bold minimal statement with glowing accent rule",
                "paired_title_type": "contrarian",
            },
        ]
    }
    concepts_spec["concepts"].extend(style_rows)
    (output_dir / "thumbnail_concepts.json").write_text(json.dumps(concepts_spec, indent=2), encoding="utf-8")
    log.info(f"Thumbnail matrix: 3 base concepts + {len(style_rows)} style variants → {output_dir.name}")
    return thumb_path


def _render_concept_focal(bg_image: Image.Image, power_words: list[str], font_size: int, width: int, height: int) -> Image.Image:
    """Concept 1: Focal subject with left-to-right gradient and electric yellow accent word."""
    img = bg_image.copy()
    # Apply left-to-right gradient (vectorized; see _make_gradient_overlay)
    gradient = _make_gradient_overlay(width, height, (8, 8, 14), max_alpha=235, exponent=1.3)
    bg_rgba = img.convert("RGBA")
    bg_rgba = Image.alpha_composite(bg_rgba, gradient)
    img = bg_rgba.convert("RGB")

    draw = ImageDraw.Draw(img)
    power_font = _get_font(int(font_size * 1.15))
    line_height = power_font.getbbox("Ay")[3] - power_font.getbbox("Ay")[1]
    total_h = len(power_words) * (line_height + 16)
    y_start = max(50, (height - total_h) // 2 - 20)
    x_offset = int(width * 0.05)

    stroke_color = (0, 0, 0)
    accent_color = (255, 230, 0)

    for i, line in enumerate(power_words):
        y = y_start + i * (line_height + 16)
        x = x_offset
        fill_color = accent_color if i == len(power_words) - 1 else (255, 255, 255)
        for dx in range(-6, 7):
            for dy in range(-6, 7):
                if dx*dx + dy*dy <= 36:
                    draw.text((x + dx, y + dy), line, font=power_font, fill=stroke_color)
        draw.text((x, y), line, font=power_font, fill=fill_color)
    return img


def _render_concept_split_box(bg_image: Image.Image, power_words: list[str], font_size: int, width: int, height: int) -> Image.Image:
    """Concept 2: Documentary accent pill boxes behind text lines for maximum readability."""
    img = ImageEnhance.Contrast(bg_image.copy()).enhance(1.25)
    img = ImageEnhance.Color(img).enhance(1.3)
    
    # Overlay semi-transparent dark tint on left 55%
    tint = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    t_draw = ImageDraw.Draw(tint)
    t_draw.rectangle([0, 0, int(width * 0.58), height], fill=(12, 12, 18, 180))
    img_rgba = Image.alpha_composite(img.convert("RGBA"), tint)
    img = img_rgba.convert("RGB")

    draw = ImageDraw.Draw(img)
    power_font = _get_font(int(font_size * 1.1))
    line_height = power_font.getbbox("Ay")[3] - power_font.getbbox("Ay")[1]
    total_h = len(power_words) * (line_height + 24)
    y_start = max(60, (height - total_h) // 2 - 10)
    x_offset = int(width * 0.06)

    for i, line in enumerate(power_words):
        y = y_start + i * (line_height + 24)
        bbox = power_font.getbbox(line)
        text_w = bbox[2] - bbox[0]
        # Draw solid contrasting background box
        box_bg = (230, 0, 40) if i == len(power_words) - 1 else (15, 15, 20)
        padding_x, padding_y = 16, 6
        draw.rounded_rectangle(
            [x_offset - padding_x, y - padding_y, x_offset + text_w + padding_x, y + line_height + padding_y],
            radius=8,
            fill=box_bg
        )
        text_fill = (255, 255, 255)
        draw.text((x_offset, y), line, font=power_font, fill=text_fill)
    return img


def _render_concept_minimal(bg_image: Image.Image, power_words: list[str], font_size: int, width: int, height: int) -> Image.Image:
    """Concept 3: Minimalist dark cinematic vignette with bold statement and glowing bottom bar."""
    img = ImageEnhance.Color(bg_image.copy()).enhance(0.7)  # Desaturate slightly for brooding mood
    # Deep overall vignette
    dark_tint = Image.new("RGBA", (width, height), (5, 5, 10, 140))
    img_rgba = Image.alpha_composite(img.convert("RGBA"), dark_tint)
    img = img_rgba.convert("RGB")

    draw = ImageDraw.Draw(img)
    power_font = _get_font(int(font_size * 1.2))
    line_height = power_font.getbbox("Ay")[3] - power_font.getbbox("Ay")[1]
    total_h = len(power_words) * (line_height + 18)
    y_start = max(50, int(height * 0.18))
    x_offset = int(width * 0.06)

    max_w = 0
    for i, line in enumerate(power_words):
        y = y_start + i * (line_height + 18)
        bbox = power_font.getbbox(line)
        text_w = bbox[2] - bbox[0]
        if text_w > max_w:
            max_w = text_w
        for dx in range(-5, 6):
            for dy in range(-5, 6):
                if dx*dx + dy*dy <= 25:
                    draw.text((x_offset + dx, y + dy), line, font=power_font, fill=(0, 0, 0))
        draw.text((x_offset, y), line, font=power_font, fill=(255, 255, 255))

    # Bold accent bar underneath
    bar_y = y_start + len(power_words) * (line_height + 18) + 12
    draw.rectangle([x_offset, bar_y, x_offset + min(max_w, int(width * 0.45)), bar_y + 10], fill=(255, 230, 0))
    return img


# ════════════════════════════════════════════
# A9: Style-library renderer
# ════════════════════════════════════════════

def _layout_left_stack(img, words, font_size, width, height, style):
    """One word per line, left-anchored — accent color on the last word."""
    accent = _hex(style.get("palette", {}).get("accent"), (255, 230, 0))
    font = _get_font(int(font_size * (style.get("font") or {}).get("size", 0) / 96
                         if (style.get("font") or {}).get("size") else 1.15))
    return _draw_word_stack(img, words, font, width, height, accent_last=accent)


def _layout_pill_stack(img, words, font_size, width, height, style):
    box = _hex(style.get("palette", {}).get("box"), (230, 0, 40))
    font = _get_font(int(font_size * 1.1))
    line_h = font.getbbox("Ay")[3] - font.getbbox("Ay")[1]
    total_h = len(words) * (line_h + 24)
    y = max(60, (height - total_h) // 2 - 10)
    x = int(width * 0.06)
    draw = ImageDraw.Draw(img)
    for i, word in enumerate(words):
        bbox = font.getbbox(word)
        w = bbox[2] - bbox[0]
        draw.rounded_rectangle([x - 16, y - 6, x + w + 16, y + line_h + 6],
                               radius=8, fill=(15, 15, 20) if i < len(words) - 1 else box)
        draw.text((x, y), word, font=font, fill=(255, 255, 255))
        y += line_h + 24
    return img


def _layout_corner_short(img, words, font_size, width, height, style):
    """1–2 big words in the bottom-left corner; subject stays fully visible."""
    short = words[:2]
    font = _get_font(int(font_size * 1.3))
    line_h = font.getbbox("Ay")[3] - font.getbbox("Ay")[1]
    y = height - line_h * len(short) - int(height * 0.08)
    x = int(width * 0.05)
    accent = _hex(style.get("palette", {}).get("accent"), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    if style.get("accent") == "red-circle":
        # Draw the callout circle on the right half (over the subject)
        cx, cy, r = int(width * 0.68), int(height * 0.42), int(width * 0.13)
        for wdt in range(10, 0, -2):
            draw.ellipse([cx - r - wdt, cy - r - wdt, cx + r + wdt, cy + r + wdt],
                         outline=(255, 29, 37))
        draw.line([cx + r * 0.7, cy + r * 0.7, cx + r * 1.5, cy + r * 1.5],
                  fill=(255, 29, 37), width=12)
    elif style.get("accent") == "emoji":
        _draw_starburst(draw, width, height, cx=int(width * 0.75), cy=int(height * 0.35))
    for i, word in enumerate(short):
        _stroked_text(draw, (x, y + i * (line_h + 6)), word, font,
                      fill=accent if i == len(short) - 1 else (255, 255, 255))
    return img


def _layout_split_labels(img, words, font_size, width, height, style):
    """THEN | NOW split screen — desaturated left, saturated right."""
    pal = style.get("palette", {})
    left_label, right_label = pal.get("left_label", "THEN"), pal.get("right_label", "NOW")
    left = ImageEnhance.Color(img).enhance(0.35)
    left = ImageEnhance.Brightness(left).enhance(0.55)
    split_x = width // 2
    img.paste(left.crop((0, 0, split_x, height)), (0, 0))
    draw = ImageDraw.Draw(img)
    # Divider
    draw.line([split_x, 0, split_x, height], fill=(240, 240, 240), width=6)
    label_font = _get_font(int(font_size * 0.8))
    _stroked_text(draw, (int(width * 0.05), int(height * 0.08)), left_label, label_font,
                  fill=(200, 200, 200))
    _stroked_text(draw, (int(width * 0.62), int(height * 0.08)), right_label, label_font,
                  fill=(255, 230, 0))
    return img


def _layout_vs_center(img, words, font_size, width, height, style):
    """Confrontation frame: dark split + VS badge in the center."""
    img = _layout_split_labels(img, words, font_size, width, height,
                               {**style, "palette": {**style.get("palette", {}),
                                                     "left_label": words[0] if words else "A",
                                                     "right_label": words[1] if len(words) > 1 else "B"}})
    draw = ImageDraw.Draw(img)
    cx, cy, r = width // 2, height // 2, int(width * 0.09)
    badge = _hex(style.get("palette", {}).get("badge"), (255, 59, 48))
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=badge,
                 outline=(255, 255, 255), width=8)
    vs_font = _get_font(int(r * 0.9))
    bbox = vs_font.getbbox("VS")
    draw.text((cx - (bbox[2] - bbox[0]) / 2, cy - (bbox[3] - bbox[1]) / 2 - bbox[1]),
              "VS", font=vs_font, fill=(255, 255, 255))
    return img


def _layout_hero_number(img, words, font_size, width, height, style):
    """Giant $ figure as the hero element (price-tag style)."""
    m = re.search(r"\$\s?\d[\d.,]*\s?(?:billion|million|B|M|K|bn|b|\+)?",
                  " ".join(words), flags=re.IGNORECASE)
    hero = m.group(0).strip() if m else ""
    if not hero:
        m2 = re.search(r"(\d[\d.,]*)\s?(billion|million)", " ".join(words), flags=re.IGNORECASE)
        if m2:
            sym = "B" if m2.group(2).lower().startswith("b") else "M"
            hero = f"${m2.group(1).rstrip('.')}{sym}"
        else:
            hero = "$99M"
    hero = hero.upper().replace("BILLION", "B").replace("MILLION", "M")
    hero = hero.replace(" ", "")
    accent = _hex(style.get("palette", {}).get("accent"), (124, 252, 0))
    draw = ImageDraw.Draw(img)
    # Fit the number to ~60% width
    size = int(font_size * 2.2)
    while size > 40:
        f = _get_font(size)
        bbox = f.getbbox(hero)
        if bbox[2] - bbox[0] <= width * 0.6:
            break
        size = int(size * 0.9)
    f = _get_font(size)
    bbox = f.getbbox(hero)
    x = int(width * 0.05)
    y = (height - (bbox[3] - bbox[1])) // 2
    _stroked_text(draw, (x, y), hero, f, fill=accent, stroke=8)
    return img


def _layout_stamp(img, words, font_size, width, height, style):
    """Evidence-board: desaturated bg + rotated stamp text."""
    img = ImageEnhance.Color(img).enhance(0.45)
    stamp_words = " ".join(words[:3]).upper() or "CLASSIFIED"
    accent = _hex(style.get("palette", {}).get("stamp"), (200, 162, 75))
    f = _get_font(int(font_size * 1.4))
    # Render the stamp on its own layer, then rotate + paste
    pad = 30
    bbox = f.getbbox(stamp_words)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rectangle([pad // 2, pad // 2, tw + pad * 1.5, th + pad * 1.5],
                 outline=accent + (255,), width=8)
    ld.text((pad, pad - bbox[1]), stamp_words, font=f, fill=accent + (255,))
    layer = layer.rotate(-8, expand=True, resample=Image.Resampling.BICUBIC)
    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(layer, dest=(int(width * 0.28), int(height * 0.32)))
    return img_rgba.convert("RGB")


_LAYOUTS = {
    "left_stack": _layout_left_stack,
    "pill_stack": _layout_pill_stack,
    "corner_short": _layout_corner_short,
    "split_labels": _layout_split_labels,
    "vs_center": _layout_vs_center,
    "hero_number": _layout_hero_number,
    "stamp": _layout_stamp,
}

# ── EXCLUSIVE news-split kit (operator reference, 2026-09-22) ──
# The 2Pac-style celebrity-news thumbnail: two faces split by a white line,
# red EXCLUSIVE bar, white headline bar with black text, LIVE badge. The
# painter needs the CLEAN (ungraded) subject photo plus a second face, so
# generate_thumbnail stashes them here before calling render_style_variants.
_EXCLUSIVE_KIT: dict = {"primary": None, "secondary": None}

# The operator's real PNG kit (assets/Thumbnail PNGs/): pre-drawn furniture
# overlays composited 1:1 on a 1280x720 canvas — LIVE badge, EXCLUSIVE tab,
# white headline bar, middle line, mid-frame red box are all baked in; only
# the headline text is drawn on top, fitted into the bar's blank interior.
_THUMB_KIT_DIR = Path(__file__).parent.parent / "assets" / "Thumbnail PNGs"
_KIT_CANVAS = (1280, 720)


def _kit_overlay(name: str, width: int, height: int) -> Image.Image | None:
    """Load a furniture overlay from the operator kit, or None if the kit is
    missing / the canvas isn't the kit's native geometry (falls back to the
    drawn version in that case — never fails a production run)."""
    if (width, height) != _KIT_CANVAS or not _THUMB_KIT_DIR.exists():
        return None
    path = _THUMB_KIT_DIR / name
    try:
        im = Image.open(path).convert("RGBA")
        if im.size != (width, height):
            im = im.resize((width, height), Image.Resampling.LANCZOS)
        return im
    except Exception:
        return None


def _kit_sticker(name: str, target_w: int) -> Image.Image | None:
    """Load a kit sticker (arrow, circle), trimmed to its alpha bbox and
    scaled to target_w — or None if unavailable."""
    path = _THUMB_KIT_DIR / name
    try:
        im = Image.open(path).convert("RGBA")
        bbox = im.getbbox()
        if not bbox:
            return None
        im = im.crop(bbox)
        if im.width < 50 or im.height < 50:
            return None
        h = max(1, int(im.height * target_w / im.width))
        return im.resize((target_w, h), Image.Resampling.LANCZOS)
    except Exception:
        return None


def _fill_crop(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale-to-fill + center crop (thumbnail-safe, no distortion)."""
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    img = img.resize((max(1, int(iw * scale)), max(1, int(ih * scale))), Image.Resampling.LANCZOS)
    iw, ih = img.size
    x, y = (iw - w) // 2, max(0, int((ih - h) * 0.28))   # bias crop toward the face
    return img.crop((x, y, x + w, y + h))


def _italic_text(text: str, font, fill, shear: float = 0.22):
    """Render bold text with a fake-italic lean on its own RGBA layer."""
    bbox = font.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 12
    layer = Image.new("RGBA", (tw + pad * 2 + int(shear * th), th + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.text((pad, pad - bbox[1]), text, font=font, fill=fill)
    layer = layer.transform(
        (layer.width + int(shear * th), layer.height),
        Image.AFFINE, (1, shear, -shear * th, 0, 1, 0),
        resample=Image.Resampling.BICUBIC,
    )
    return layer


_KIT_SEAM_CACHE: dict[tuple[int, int], int] = {}


def _kit_divider_x(width: int, height: int) -> int:
    """FIX-074: the x where the kit's white middle line ACTUALLY sits.

    The outline overlay bakes its divider right of centre (x≈683–695 on
    1280x720); the painter used to seam the two faces at width//2 (x=640),
    leaving ~43px of the right face peeking out LEFT of the white line —
    which read as a missing/diverged divider. Detect the line's centre
    column from the kit overlay so the faces butt exactly at the line.
    Falls back to the midpoint when the kit is missing/off-size.
    """
    key = (width, height)
    if key in _KIT_SEAM_CACHE:
        return _KIT_SEAM_CACHE[key]
    seam = width // 2
    if (width, height) == _KIT_CANVAS and _THUMB_KIT_DIR.exists():
        try:
            arr = np.asarray(
                Image.open(_THUMB_KIT_DIR / "outline for thumbnails.png")
                .convert("RGBA"), dtype=float)
            alpha = arr[..., 3]
            full_height = [x for x in range(width)
                           if (alpha[:, x] > 200).mean() > 0.7]
            white_cols = []
            for x in full_height:
                opaque = alpha[:, x] > 200
                if opaque.any() and arr[:, x, :3][opaque].mean() > 200:
                    white_cols.append(x)
            if white_cols:
                seam = (min(white_cols) + max(white_cols)) // 2
        except Exception:
            seam = width // 2
    _KIT_SEAM_CACHE[key] = seam
    return seam


def _layout_exclusive_news(img, words, font_size, width, height, style):
    """EXCLUSIVE News Split (2Pac reference): faces left+right split by a
    white line, red EXCLUSIVE bar, white headline bar, LIVE badge.
    Ignores the pre-graded bg — builds from clean photos in _EXCLUSIVE_KIT,
    falling back to the passed bg for the left half.

    Furniture comes from the operator's real PNG kit when the canvas is
    1280x720 (LIVE badge, EXCLUSIVE tab, headline bar, middle line and the
    mid-frame red box are baked into the overlay); accent stickers (red
    circle / curved arrow) are chosen via the style's `accent`.
    Falls back to drawn furniture if the kit is unavailable."""
    pal = style.get("palette", {})
    red = _hex(pal.get("bar"), (230, 0, 40))
    white = (255, 255, 255)
    ink = _hex(pal.get("headline_text"), (17, 17, 17))

    canvas = Image.new("RGB", (width, height), (10, 10, 14))
    # FIX-074: seam the faces AT the kit's white line, not at the midpoint —
    # the line sits at x≈689 on 1280x720, so the right face starts there.
    seam_x = _kit_divider_x(width, height)

    def _face(photo, w: int) -> Image.Image | None:
        try:
            p = _EXCLUSIVE_KIT.get(photo)
            if not p:
                return None
            im = Image.open(p).convert("RGB")
            if im.size[0] < 300 or im.size[1] < 300:
                return None
            im = _fill_crop(im, w, height)
            im = ImageEnhance.Color(im).enhance(1.18)
            im = ImageEnhance.Contrast(im).enhance(1.08)
            return im
        except Exception:
            return None

    left = _face("primary", seam_x)
    if left is None:
        left = _fill_crop(img.convert("RGB"), seam_x, height)
    right = _face("secondary", width - seam_x) or left.transpose(Image.FLIP_LEFT_RIGHT)

    canvas.paste(left, (0, 0))
    canvas.paste(right, (seam_x, 0))

    headline = " ".join(words[:5]).upper() or "BREAKING NOW"

    # ── preferred path: the operator's real kit overlays (1280x720) ──
    outline = _kit_overlay("outline for thumbnails.png", width, height)
    if outline is not None:
        accent = style.get("accent", "red_bar")
        if accent == "red_circle":
            sticker = _kit_sticker("Red Circle.PNG", int(width * 0.20))
            if sticker:
                canvas.paste(sticker, (int(width * 0.645), int(height * 0.11)), sticker)
        elif accent == "red_arrow":
            sticker = _kit_sticker("Red Curved Arrow.PNG", int(width * 0.42))
            if sticker:
                canvas.paste(sticker, (int(width * 0.035), int(height * 0.10)), sticker)
        canvas.paste(outline, (0, 0), outline)
        # Headline text fitted into the overlay's blank headline bar
        # (measured from the kit: interior ≈ x 25..1080, y 608..706 @1280x720).
        ix0, ix1 = int(width * 0.02), int(width * 0.84)
        iy0, iy1 = int(height * 0.845), int(height * 0.98)
        hl_font_size = int((iy1 - iy0) * 0.80)
        hl_font = _get_font(hl_font_size)
        while hl_font_size > 30 and (hl_font.getbbox(headline)[2] - hl_font.getbbox(headline)[0]) > (ix1 - ix0):
            hl_font_size -= 4
            hl_font = _get_font(hl_font_size)
        hb = hl_font.getbbox(headline)
        tw, th = hb[2] - hb[0], hb[3] - hb[1]
        hl_layer = _italic_text(headline, hl_font, ink + (255,), shear=0.10)
        tx = ix0 + ((ix1 - ix0) - hl_layer.width) // 2
        ty = (iy0 + iy1) // 2 - hl_layer.height // 2
        canvas.paste(hl_layer, (tx, ty), hl_layer)
        return canvas

    # ── fallback: drawn furniture (kit missing or off-size canvas) ──
    draw = ImageDraw.Draw(canvas)

    # White middle line (the kit's "Middle Line Only")
    div = _hex(pal.get("divider"), white)
    draw.rectangle([half_w - 4, 0, half_w + 4, height], fill=div)

    # Headline bar (white, black impact text, slight lean like the reference)
    hl_font_size = int(height * 0.155)
    hl_font = _get_font(hl_font_size)
    while hl_font_size > 40 and (hl_font.getbbox(headline)[2] - hl_font.getbbox(headline)[0]) > width - 90:
        hl_font_size -= 6
        hl_font = _get_font(hl_font_size)
    hl_bbox = hl_font.getbbox(headline)
    hl_th = hl_bbox[3] - hl_bbox[1]
    bar_h = hl_th + int(hl_th * 0.75)
    bar_y = height - bar_h - 14
    draw.rectangle([10, bar_y, width - 10, bar_y + bar_h], fill=white)
    hl_layer = _italic_text(headline, hl_font, ink + (255,), shear=0.10)
    canvas.paste(hl_layer, (int(width * 0.035), bar_y + (bar_h - hl_layer.height) // 2), hl_layer)

    # EXCLUSIVE bar above the headline (red, italic white text)
    ex_font = _get_font(int(height * 0.095))
    ex_layer = _italic_text("EXCLUSIVE", ex_font, white + (255,), shear=0.28)
    ex_bar_h = ex_layer.height + 8
    ex_bar_y = bar_y - ex_bar_h - 12
    draw.rectangle([18, ex_bar_y, 18 + ex_layer.width + 44, ex_bar_y + ex_bar_h], fill=red)
    canvas.paste(ex_layer, (40, ex_bar_y + 4), ex_layer)

    # LIVE badge, top-left
    live_font = _get_font(int(height * 0.062))
    lv_layer = _italic_text("LIVE", live_font, white + (255,), shear=0.18)
    lv_h = lv_layer.height + 12
    draw.rectangle([16, 16, 16 + lv_layer.width + 34, 16 + lv_h], fill=red)
    canvas.paste(lv_layer, (32, 22), lv_layer)

    return canvas


_LAYOUTS["exclusive_news"] = _layout_exclusive_news   # registered after its def


def _pick_secondary_face(script: Script, subject_idx: int | None,
                         output_dir: Path | None = None) -> str | None:
    """FIX-073: choose the split's RIGHT face deliberately, not blindly.

    The old code took the first photo that wasn't the primary's — on a
    two-name story ("Bruno Mars vs Drake") that let an unrelated red-lit
    stage shot stand in for the second name and wreck the thumbnail.

    Priority:
      1. A scene whose people_to_show names the OTHER title tokens (the
         second celebrity) — the same matching rule the primary uses.
      2. Any scene photo that is NOT a Tier-4 gradient fallback (checked
         against scenes/scene_fallback.json) and differs from the primary.
      3. A scene photo that differs from the primary at all.
    Returns a path or None (painter then mirrors the primary rather than
    shipping a mismatched stranger).
    """
    scenes = list(getattr(script, "scenes", []) or [])
    if not scenes:
        return None

    primary_path = None
    if subject_idx is not None and 0 <= subject_idx < len(scenes):
        primary_path = scenes[subject_idx].photo_path

    subject = _subject_tokens(getattr(script, "title", "") or "")

    # 1) the OTHER named person: among scenes whose people hit the title's
    #    tokens, take the EARLIEST-MENTIONED one whose people don't overlap
    #    the primary's ("Bruno Mars vs Drake: The War For Karol G" -> Drake,
    #    not Karol G, who is merely mentioned later in the title).
    title_lc = (getattr(script, "title", "") or "").lower()
    positions = {w: i for i, w in enumerate(
        w for w in re.findall(r"[A-Za-z]+", title_lc) if len(w) > 2)}

    def _people_tokens(scene) -> set[str]:
        return {w.lower()
                for p in (scene.people_to_show or [])
                for w in re.findall(r"[A-Za-z]+", p)
                if len(w) > 2}

    primary_people: set[str] = set()
    if subject_idx is not None and 0 <= subject_idx < len(scenes):
        primary_people = _people_tokens(scenes[subject_idx])

    best: tuple[int, int, str] | None = None   # (token_pos, scene_idx, path)
    if subject:
        for i, scene in enumerate(scenes):
            if i == subject_idx or not scene.photo_path:
                continue
            person_words = _people_tokens(scene)
            if not person_words or (primary_people and person_words & primary_people):
                continue
            pos = min((positions[t] for t in person_words if t in positions),
                      default=None)
            if pos is None:
                continue
            if best is None or pos < best[0]:
                best = (pos, i, scene.photo_path)
    if best is not None:
        return best[2]

    secondary: str | None = None

    # Fallback ledger: scene_no -> {"reason": ...} for Tier-4 gradients.
    ledger: dict = {}
    ledger_path = (Path(output_dir) / "scenes" / "scene_fallback.json") if output_dir else None
    if ledger_path and ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except Exception:
            ledger = {}

    def _is_fallback(idx: int) -> bool:
        # scenes are 1-numbered in the ledger (scene_1, scene_2, ...)
        return str(idx + 1) in ledger

    # 2) any non-fallback photo distinct from the primary
    for i, scene in enumerate(scenes):
        if i == subject_idx or not scene.photo_path or _is_fallback(i):
            continue
        if scene.photo_path != primary_path and Path(scene.photo_path).exists():
            secondary = secondary or scene.photo_path
            break

    # 3) last resort: the primary's photo (painter mirrors it) — never a
    #    fallback-marked scene, which would poison the right half again.
    if secondary is None:
        for i, scene in enumerate(scenes):
            if i != subject_idx and scene.photo_path and not _is_fallback(i):
                secondary = scene.photo_path
                break
    return secondary

_BG_TREATMENTS = {
    "gradient": lambda im, w, h: Image.alpha_composite(
        im.convert("RGBA"),
        _make_gradient_overlay(w, h, (8, 8, 14), max_alpha=235, exponent=1.3)).convert("RGB"),
    "tint_left": lambda im, w, h: Image.alpha_composite(
        im.convert("RGBA"),
        _flat_tint(w, h, (12, 12, 18, 180), 0.58)).convert("RGB"),
    "heavy_vignette": lambda im, w, h: Image.alpha_composite(
        im.convert("RGBA"),
        _flat_tint(w, h, (5, 5, 10, 150), 1.0)).convert("RGB"),
    "desaturate": lambda im, w, h: ImageEnhance.Color(im).enhance(0.5),
    "split": lambda im, w, h: im,
    "split_faces": lambda im, w, h: im,   # painter builds its own canvas
    "none": lambda im, w, h: im,
}


def _hex(value, fallback):
    try:
        v = value.lstrip("#")
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except Exception:
        return fallback


def _flat_tint(width, height, rgba, frac):
    """Solid RGBA tint covering `frac` of the width from the left."""
    tint = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(tint)
    d.rectangle([0, 0, int(width * frac), height], fill=rgba)
    return tint


def _draw_word_stack(img, words, font, width, height, accent_last=(255, 230, 0)):
    line_h = font.getbbox("Ay")[3] - font.getbbox("Ay")[1]
    total_h = len(words) * (line_h + 16)
    y = max(50, (height - total_h) // 2 - 20)
    x = int(width * 0.05)
    draw = ImageDraw.Draw(img)
    for i, word in enumerate(words):
        _stroked_text(draw, (x, y + i * (line_h + 16)), word, font,
                      fill=accent_last if i == len(words) - 1 else (255, 255, 255))
    return img


def _stroked_text(draw, xy, text, font, fill=(255, 255, 255), stroke=6):
    x, y = xy
    draw.text((x, y), text, font=font, fill=fill,
              stroke_width=stroke, stroke_fill=(0, 0, 0))


def _draw_starburst(draw, width, height, cx, cy):
    import math
    r_out, r_in = int(width * 0.09), int(width * 0.06)
    pts = []
    for i in range(16):
        ang = math.pi * i / 8
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    draw.polygon(pts, fill=(255, 230, 0), outline=(0, 0, 0))


def render_style_variants(bg_image: Image.Image, power_words: list[str], font_size: int,
                          width: int, height: int, output_dir: Path,
                          hook_text: str = "", niche: str = "",
                          only_styles: list[str] | None = None,
                          preferred_styles: list[str] | None = None) -> list[dict]:
    """Render the recommended style-library variants into output_dir.

    User templates lead the ranking (their taste + their A/B data), curated
    styles follow, ranked by niche match. preferred_styles (from a channel
    template) are promoted to the front of the auto matrix without disabling
    the rest. Deterministic per style id. Returns rows that extend
    thumbnail_concepts.json so the packaging log and the A/B leaderboard can
    distinguish WHICH style shipped.
    """
    from pipeline.thumbnail_styles import recommend_styles

    rows: list[dict] = []
    styles = recommend_styles(niche)
    if preferred_styles:
        pref = [s for s in styles if s.get("id") in set(preferred_styles)]
        rest = [s for s in styles if s.get("id") not in set(preferred_styles)]
        styles = pref + rest
    if only_styles:
        wanted = set(only_styles)
        styles = [s for s in styles if s.get("id") in wanted]
        # preserve the requested order; explicit requests bypass the 6-cap
        order = {sid: i for i, sid in enumerate(only_styles)}
        styles.sort(key=lambda s: order.get(s.get("id"), 99))
    else:
        styles = styles[:6]      # auto mode: keep the matrix sane (6 variants max)

    for style in styles:
        sid = style.get("id", "style")
        painter = _LAYOUTS.get(style.get("text_layout"), _layout_left_stack)
        treatment = _BG_TREATMENTS.get(style.get("bg_treatment"), _BG_TREATMENTS["gradient"])
        words = list(power_words) or ["WATCH"]
        if sid == "price-tag":
            words = (hook_text or " ").split() or words
        try:
            img = treatment(bg_image.copy(), width, height)
            img = painter(img, words, font_size, width, height, style)
            fname = f"thumbnail_style_{sid.replace('-', '_')}.png"
            img.save(str(output_dir / fname), "PNG", quality=95)
            rows.append({
                "variant": f"style_{sid}",
                "id": sid,
                "file": fname,
                "approach": style.get("name", sid),
                "paired_title_type": style.get("pair_with", ""),
                "source": style.get("source", "curated"),
            })
        except Exception as e:
            log.warning(f"style '{sid}' failed to render: {e}")
    log.info(f"Style variants rendered: {[r['id'] for r in rows]}")
    return rows


if __name__ == "__main__":
    print("Thumbnail module loaded. Run via main.py.")
