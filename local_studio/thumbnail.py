"""Free local photo composition. No image-generation API, fake faces or paid service."""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance, ImageFilter

from .common import ROOT, QualityBlocked, save


def font(size):
    return ImageFont.truetype(str(ROOT / 'assets/fonts/Montserrat-Bold.ttf'), size)


def create_variants(script: dict, photo: Path, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    choices = script.get('thumbnail_words', [])
    if len(choices) != 3:
        raise QualityBlocked('Each video needs three thumbnail concepts.')
    outputs = []
    specs = []
    palettes = [('#101821', '#edbd70'), ('#101821', '#7ee0df'), ('#201310', '#ffcb74')]
    for i, (words, (background, accent)) in enumerate(zip(choices, palettes)):
        words = re.sub(r'[^A-Za-z0-9\s]', '', str(words)).upper().strip()
        if not 1 <= len(words.split()) <= 4 or len(words) > 32:
            raise QualityBlocked('Thumbnail copy must be short, factual and readable.')
        image = Image.new('RGB', (1280, 720), background)
        with Image.open(photo) as source:
            portrait = ImageOps.fit(source.convert('RGB'), (720, 720), centering=(.5, .36))
        portrait = ImageEnhance.Contrast(portrait).enhance(1.04)
        image.paste(portrait, (0, 0))
        # A local opacity mask joins the photograph to the typography field.
        mask = Image.new('L', (1280, 720), 0)
        draw_mask = ImageDraw.Draw(mask)
        for x in range(440, 850):
            draw_mask.line((x, 0, x, 720), fill=round(min(1, (x - 440) / 300) * 255))
        draw_mask.rectangle((850, 0, 1280, 720), fill=255)
        image.paste(Image.new('RGB', image.size, background), (0, 0), mask)
        draw = ImageDraw.Draw(image)
        tokens = words.split()
        lines = tokens if len(tokens) <= 3 else [' '.join(tokens[:2]), ' '.join(tokens[2:])]
        size = 110
        while size > 52 and max(draw.textbbox((0, 0), line, font=font(size))[2] for line in lines) > 540:
            size -= 2
        line_height = round(size * 1.15)
        top = (720 - line_height * len(lines)) // 2 - 15
        for n, line in enumerate(lines):
            draw.text((690, top + n * line_height), line, font=font(size), fill=accent if n == len(lines)-1 else '#f5f1e9', stroke_width=1)
        draw.rectangle((694, top - 36, 776, top - 30), fill=accent)
        target = out / f'thumbnail_{i+1}.png'
        image.save(target, optimize=True)
        if target.stat().st_size > 2_000_000:
            target = out / f'thumbnail_{i+1}.jpg'
            image.save(target, quality=91, optimize=True)
        image.resize((120, 68), Image.Resampling.LANCZOS).save(out / f'mobile_{i+1}.png')
        outputs.append(target)
        specs.append({'file': target.name, 'words': words, 'focal_point': 'genuine licensed celebrity photograph', 'mobile_preview': f'mobile_{i+1}.png', 'automatically_selected_winner': False})
    save(out / 'thumbnail_brief.json', {'title': script['title'], 'concepts': specs, 'note': 'Design options, not a measured CTR promise. Inspect the supplied phone-size previews.'})
    return outputs
