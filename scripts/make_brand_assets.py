"""
Brand asset builder for the Rise and Ruin channel.

Generates:
  assets/brand/profile_picture.png   (800x800, safe-circle composition)
  assets/brand/banner_2048x1152.png  (full-bleed TV size)
  assets/brand/banner_safe_1235x338.png (the exact safe-area crop YouTube
                                       shows on desktop/mobile — text lives
                                       entirely inside it)

Design: cinematic dark charcoal -> deep purple diagonal gradient (matches the
thumbnail style library's highlight/accent), "RISE" stacked over "& RUIN" with
a hard gold underline — same gold used by the my_gold_frame thumbnail style.
Montserrat-Bold, generous stroke, no thin strokes (legible at 24px).

Run: venv/Scripts/python.exe scripts/make_brand_assets.py
"""
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont, ImageFilter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FONT_PATH = PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Bold.ttf"
OUT_DIR = PROJECT_ROOT / "assets" / "brand"

CHARCOAL_TOP = (24, 24, 28)
CHARCOAL_BOT = (10, 10, 12)
PURPLE = (75, 0, 130)          # #4B0082 — the engine's accent
GOLD = (212, 175, 55)          # #D4AF37 — my_gold_frame gold
GOLD_BRIGHT = (255, 215, 90)
WHITE = (245, 245, 245)


def _diagonal_gradient(w: int, h: int, top, bottom, accent, accent_frac: float = 0.35) -> Image.Image:
    """Charcoal vertical gradient with a purple glow rising from bottom-right."""
    base = Image.new("RGB", (w, h))
    px = base.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(CHARCOAL_TOP[0] + (CHARCOAL_BOT[0] - CHARCOAL_TOP[0]) * t)
        g = int(CHARCOAL_TOP[1] + (CHARCOAL_BOT[1] - CHARCOAL_TOP[1]) * t)
        b = int(CHARCOAL_TOP[2] + (CHARCOAL_BOT[2] - CHARCOAL_TOP[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    glow = Image.new("L", (w, h), 0)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([w - int(w * 0.9), h - int(h * 0.9), int(w * 1.15), int(h * 1.3)], fill=110)
    glow = glow.filter(ImageFilter.GaussianBlur(w // 10))
    tint = Image.new("RGB", (w, h), accent)
    base = Image.composite(Image.blend(base, tint, 0.55), base, glow)
    return base


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _center(draw, text, font, cy, fill, stroke=0, stroke_fill=None):
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    cx = draw._image.width // 2
    draw.text((cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]), text,
              font=font, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)


def make_profile() -> Image.Image:
    """800x800. Wordmark sits inside the inner 66% so the circle crop keeps it."""
    size = 800
    img = _diagonal_gradient(size, size, CHARCOAL_TOP, CHARCOAL_BOT, PURPLE)
    d = ImageDraw.Draw(img)

    f_big = _font(190)
    f_small = _font(120)
    _center(d, "RISE", f_big, 320, WHITE, stroke=10, stroke_fill=(0, 0, 0))
    _center(d, "& RUIN", f_small, 520, GOLD_BRIGHT, stroke=8, stroke_fill=(0, 0, 0))

    # gold underline, offset line detail
    d.rectangle([140, 620, 660, 634], fill=GOLD)
    d.rectangle([300, 650, 500, 658], fill=PURPLE)
    return img


def make_banner(full_w: int = 2560, full_h: int = 1440) -> Image.Image:
    """Full-bleed TV banner; all text inside the central 1235x338 safe box.

    YouTube's safe area (all devices) is 1235x338 centered in the 2560x1440
    upload; everything outside gets cropped on desktop/mobile.
    """
    img = _diagonal_gradient(full_w, full_h, CHARCOAL_TOP, CHARCOAL_BOT, PURPLE, accent_frac=0.5)
    # subtle film-grain texture
    noise = Image.effect_noise((full_w, full_h), 18).convert("L")
    img = Image.composite(ImageOps_soften(img), img, noise.point(lambda v: v // 6))
    d = ImageDraw.Draw(img)

    cx, cy = full_w // 2, full_h // 2
    sw, sh = 1235, 338
    sx0, sy0 = cx - sw // 2, cy - sh // 2
    sx1, sy1 = cx + sw // 2, cy + sh // 2

    def fitted(text, target_size, max_w):
        """Largest Montserrat size <= target whose rendered width fits max_w."""
        size = target_size
        while size > 20:
            f = _font(size)
            b = d.textbbox((0, 0), text, font=f)
            if b[2] - b[0] <= max_w:
                return f
            size -= 4
        return _font(20)

    # vertical plan inside the 338px safe box: tag / title / subtitle
    tag = "EVERY EMPIRE FALLS"
    title = "RISE & RUIN"
    sub = "The rise and fall of the famous and the mighty"

    f_tag = fitted(tag, 52, sw - 300)
    f_main = fitted(title, 150, sw - 120)
    f_sub = fitted(sub, 44, sw - 200)

    tb = d.textbbox((0, 0), tag, font=f_tag)
    d.text((cx - (tb[2] - tb[0]) / 2, sy0 + 18), tag, font=f_tag, fill=GOLD_BRIGHT)

    mb = d.textbbox((0, 0), title, font=f_main)
    title_y = cy - (mb[3] - mb[1]) / 2 - mb[1] + 14
    d.text((cx - (mb[2] - mb[0]) / 2 - mb[0], title_y), title, font=f_main,
           fill=WHITE, stroke_width=10, stroke_fill=(0, 0, 0))

    sb = d.textbbox((0, 0), sub, font=f_sub)
    d.text((cx - (sb[2] - sb[0]) / 2, sy1 - (sb[3] - sb[1]) - 30), sub,
           font=f_sub, fill=(198, 198, 205))

    # gold frame strokes at the safe-box edges
    d.rectangle([sx0 + 40, sy0 - 8, sx1 - 40, sy0 - 2], fill=GOLD)
    d.rectangle([sx0 + 40, sy1 + 2, sx1 - 40, sy1 + 8], fill=GOLD)
    return img


def ImageOps_soften(img: Image.Image) -> Image.Image:
    return img.filter(ImageFilter.GaussianBlur(2))


def _center_at(draw, text, font, cy, fill, stroke=0, stroke_fill=None):
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text((draw._image.width // 2 - tw / 2 - bbox[0], cy - th / 2 - bbox[1]),
              text, font=font, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profile = make_profile()
    profile.save(OUT_DIR / "profile_picture.png")

    banner = make_banner()
    banner = banner.resize((2048, 1152), Image.LANCZOS)  # YouTube max upload size
    banner.save(OUT_DIR / "banner_2048x1152.png")

    # exact safe-area crop preview (what desktop/mobile viewers see)
    w, h = banner.size
    cx, cy = w // 2, h // 2
    safe = banner.crop((cx - 1235 // 2 * w // 2560 * 2 // 2, 0, 0, 0)) if False else None
    # simpler: crop the central 1235x338 proportionally from the 2048x1152 render
    scale = w / 2560
    sw, sh = int(1235 * scale), int(338 * scale)
    safe = banner.crop((cx - sw // 2, cy - sh // 2, cx + sw // 2, cy + sh // 2))
    safe.save(OUT_DIR / "banner_safe_area_preview.png")

    print("brand assets written to", OUT_DIR)
    for f in sorted(OUT_DIR.iterdir()):
        print("  ", f.name, f.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
