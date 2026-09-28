"""
Rockefeller thumbnail v2 — fixes the murky-portrait problem.

Design (MagnatesMedia-grade, channel-brand consistent):
  * 1885 public-domain portrait (piercing gaze) on the RIGHT, contrast-graded
    with a gold/purple duotone so the face READS at feed size
  * left-edge fade into the brand charcoal->purple gradient
  * short copy stack: THE / $400 BILLION / MONOPOLY (gold accent word)
  * thin gold frame inset — the user's my_gold_frame template

Run: venv/Scripts/python.exe scripts/make_rockefeller_thumb.py
Out: assets/brand/rockefeller_thumbnail_v2.png  (1280x720)
"""
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FONT_PATH = PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Bold.ttf"
PORTRAIT = PROJECT_ROOT / "assets" / "brand" / "_tmp" / "rockefeller_1885.jpg"
OUT = PROJECT_ROOT / "assets" / "brand" / "rockefeller_thumbnail_v2.png"

W, H = 1280, 720
CHARCOAL_TOP = (26, 24, 30)
CHARCOAL_BOT = (8, 8, 10)
PURPLE = (75, 0, 130)
GOLD = (212, 175, 55)
GOLD_BRIGHT = (255, 215, 90)
WHITE = (248, 248, 248)


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def background() -> Image.Image:
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        t = y / (H - 1)
        r = int(CHARCOAL_TOP[0] + (CHARCOAL_BOT[0] - CHARCOAL_TOP[0]) * t)
        g = int(CHARCOAL_TOP[1] + (CHARCOAL_BOT[1] - CHARCOAL_TOP[1]) * t)
        b = int(CHARCOAL_TOP[2] + (CHARCOAL_BOT[2] - CHARCOAL_TOP[2]) * t)
        for x in range(W):
            px[x, y] = (r, g, b)
    glow = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-int(W * 0.25), H - int(H * 0.55), int(W * 0.45), int(H * 1.2)], fill=90)
    glow = glow.filter(ImageFilter.GaussianBlur(140))
    tint = Image.new("RGB", (W, H), PURPLE)
    return Image.composite(Image.blend(img, tint, 0.5), img, glow)


def portrait_panel() -> Image.Image:
    """Right-side portrait: graded, warm duotone, left-edge faded to black."""
    src = Image.open(PORTRAIT).convert("RGB")
    # --- grade for feed-size legibility ---
    src = ImageEnhance.Contrast(src).enhance(1.22)
    src = ImageEnhance.Brightness(src).enhance(1.12)
    src = ImageEnhance.Color(src).enhance(0.0)  # pure mono base
    # warm gold duotone: map shadows->deep brown, highlights->warm cream
    duo = ImageOps.colorize(src.convert("L"), black=(38, 30, 26), white=(244, 232, 205), mid=(128, 102, 78))
    src = duo

    # crop a face-forward window: source 1495x2048, face in upper-center
    sw, sh = src.size
    crop_w = int(sw * 0.92)
    crop_h = int(crop_w * H / (W * 0.52) * (H / H))  # panel aspect handled below
    panel_w = int(W * 0.52)          # panel occupies right 52% of canvas
    panel_h = H
    # fill panel: scale so height covers, center on the face (upper third)
    scale = panel_h / sh
    new_w = int(sw * scale)
    src = src.resize((new_w, panel_h), Image.LANCZOS)
    face_x = int(new_w * 0.44)       # face sits ~44% across the source
    x0 = max(0, min(new_w - panel_w, face_x - panel_w // 2))
    panel = src.crop((x0, 0, x0 + panel_w, panel_h))

    # left-edge fade to transparent so it melts into the background
    mask = Image.new("L", (panel_w, panel_h), 255)
    mp = mask.load()
    fade = int(panel_w * 0.38)
    for x in range(fade):
        v = int(255 * (x / fade) ** 1.5)
        for y in range(panel_h):
            mp[x, y] = v
    # subtle top/bottom vignette
    for y in range(int(panel_h * 0.06)):
        v = int(255 * y / (panel_h * 0.06))
        for x in range(panel_w):
            mp[x, y] = min(mp[x, y], v)
    for y in range(int(panel_h * 0.94), panel_h):
        v = int(255 * (panel_h - y) / (panel_h * 0.06))
        for x in range(panel_w):
            mp[x, y] = min(mp[x, y], v)

    panel = panel.convert("RGBA")
    panel.putalpha(mask)
    return panel


def text_stack(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    lines = [
        ("THE", 64, WHITE, 6),
        ("$400 BILLION", 118, WHITE, 9),
        ("MONOPOLY", 96, GOLD_BRIGHT, 9),
    ]
    x = 72
    y = 132
    for text, size, fill, stroke in lines:
        f = font(size)
        d.text((x, y), text, font=f, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
        bb = d.textbbox((0, 0), text, font=f, stroke_width=stroke)
        y += (bb[3] - bb[1]) + 26
    # gold rule under the stack — brand echo
    d.rectangle([x, y + 6, x + 380, y + 13], fill=GOLD)


def gold_frame(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    inset, t = 22, 4
    d.rectangle([inset, inset, W - inset, inset + t], fill=GOLD)
    d.rectangle([inset, H - inset - t, W - inset, H - inset], fill=GOLD)
    d.rectangle([inset, inset, inset + t, H - inset], fill=GOLD)
    d.rectangle([W - inset - t, inset, W - inset, H - inset], fill=GOLD)


def main() -> None:
    img = background().convert("RGBA")
    panel = portrait_panel()
    img.alpha_composite(panel, (W - panel.width, 0))
    text_stack(img)
    gold_frame(img)
    img.convert("RGB").save(OUT, quality=95)
    print("saved", OUT)

    # feed-size self-check: does the copy still read at 320px wide?
    small = img.convert("RGB").resize((320, 180), Image.LANCZOS)
    small.save(OUT.parent / "rockefeller_thumbnail_v2_feedsize.png")
    print("saved feed-size check", OUT.parent / "rockefeller_thumbnail_v2_feedsize.png")


if __name__ == "__main__":
    main()
