from __future__ import annotations

import hashlib

import json

import math

import re

import time

from pathlib import Path

from urllib.parse import urlparse,parse_qsl,urlencode,urlunparse

import requests

from bs4 import BeautifulSoup

from PIL import Image, ImageDraw

from .common import LOG, OUTPUT, ROOT, QualityBlocked, command, duration, probe, save, settings, space_check

COMMONS = 'https://commons.wikimedia.org/w/api.php'
USER_AGENT='FameFilesLocalStudio/1.0 (https://github.com/FrankleenMbanje/GhostDirector; licensed media research)'

def canonical_media_url(url):
    parsed=urlparse(url)
    if parsed.hostname!='upload.wikimedia.org':return url
    query=urlencode([(key,value) for key,value in parse_qsl(parsed.query) if not key.startswith('utm_')])
    return urlunparse(parsed._replace(query=query))

def plain(value):

    return BeautifulSoup(value or '', 'html.parser').get_text(' ', strip=True)

def creator_credit(value):
    parsed=BeautifulSoup(value or '', 'html.parser')
    name=parsed.get_text(' ',strip=True)
    if name and not re.fullmatch(r'\[\d+\]',name):return name
    for link in parsed.find_all('a',href=True):
        url=link['href']
        if url.startswith('//'):url='https:'+url
        if urlparse(url).scheme=='https' and urlparse(url).hostname:return url
    return name


def accepted_license(name: str) -> bool:

    value = name.casefold().strip()

    return value in {'cc0', 'cc0 1.0', 'public domain', 'pd', 'cc by 2.0', 'cc by 2.5', 'cc by 3.0', 'cc by 4.0'}

def commons_original(title):
    stem=re.sub(r'\.[A-Za-z0-9]{2,5}$','',title)
    stem=re.sub(r'\s*\((?:cropped(?:square)?|crop)(?:\s*\d+)?\)(?:\s*\d+)?$','',stem,flags=re.IGNORECASE)
    return 'commons:'+hashlib.sha256(' '.join(stem.casefold().split()).encode()).hexdigest()[:24]


def candidates(subject: str, media_type: str) -> list[dict]:

    pages={}
    offset=0
    # Keep each metadata batch below Commons' 50-imageinfo limit.
    for _ in range(3):
        response=requests.get(COMMONS,params={
            'action':'query','format':'json','generator':'search',
            'gsrsearch':f'"{subject}" filetype:{media_type}','gsrnamespace':6,
            'gsrlimit':35,'gsroffset':offset,'prop':'imageinfo|categories','cllimit':'max',
            'iiprop':'url|size|extmetadata','iiurlwidth':1920
        },headers={'User-Agent':USER_AGENT},timeout=40)
        response.raise_for_status();data=response.json()
        pages.update(data.get('query',{}).get('pages',{}))
        following=data.get('continue',{}).get('gsroffset')
        if following is None:break
        offset=following

    result = []

    for page in pages.values():

        info = (page.get('imageinfo') or [{}])[0]

        meta = info.get('extmetadata', {})

        field = lambda key: plain(meta.get(key, {}).get('value', ''))

        license_name = field('LicenseShortName')

        categories = (' '.join(x['title'] for x in page.get('categories', []))+' '+field('Categories')).casefold()

        if not accepted_license(license_name) or any(x in categories for x in ['deletion requests', 'no permission', 'watermark', 'copyright violations', 'disputed']):

            continue

        url = canonical_media_url(info.get('url', ''))

        if urlparse(url).hostname != 'upload.wikimedia.org' or urlparse(url).scheme != 'https':

            continue

        if info.get('size', 0) > settings()['maximum_asset_mb'] * 1024**2:

            continue

        width, height = info.get('width', 0), info.get('height', 0)

        adequate = (width >= 1280 and height >= 700) if media_type == 'video' else (min(width, height) >= 700 and max(width, height) >= 1200)

        if not adequate:

            continue

        credit = meta.get('Credit', {}).get('value', '')

        original = re.search(r'(?:youtu\.be/|youtube\.com/watch\?v=)([\w-]{11})', credit)

        result.append({'id': str(page['pageid']), 'origin_id': 'youtube:' + original[1] if original else commons_original(page['title']), 'kind': 'video' if media_type == 'video' else 'photo', 'role':'subject','provider':'commons','title': page['title'], 'subject': subject, 'url': url, 'source_page': info.get('descriptionurl', ''), 'creator': creator_credit(meta.get('Attribution',{}).get('value') or meta.get('Artist',{}).get('value','')), 'license': license_name, 'license_url': field('LicenseUrl'), 'archive_date': field('DateTimeOriginal') or field('DateTime'), 'description': field('ImageDescription')[:1800], 'size': info.get('size', 0)})

    return result

def fetch(asset: dict, cache: Path) -> Path:

    extension = Path(urlparse(asset['url']).path).suffix.lower()

    if extension not in {'.jpg', '.jpeg', '.png', '.webp', '.mp4', '.webm', '.ogv', '.mov'}:

        raise QualityBlocked('Unsupported licensed media format.')

    target = cache / (asset['id'] + extension)

    if target.exists():

        return target

    space_check()

    partial = target.with_suffix(target.suffix + '.part')

    with requests.get(canonical_media_url(asset['url']), stream=True, timeout=(15, 45), headers={'User-Agent': USER_AGENT}) as response:

        response.raise_for_status()

        permitted={'upload.wikimedia.org'} if asset.get('provider','commons')=='commons' else {'videos.pexels.com'}

        if urlparse(response.url).hostname not in permitted:

            raise QualityBlocked('Media redirected outside its licensed source.')

        total = 0

        with partial.open('wb') as handle:

            for chunk in response.iter_content(128 * 1024):

                total += len(chunk)

                if total > settings()['maximum_asset_mb'] * 1024**2:

                    raise QualityBlocked('Source exceeds the local storage budget.')

                handle.write(chunk)

    partial.replace(target)

    return target

def visual_sheet(asset: dict, source: Path, cache: Path) -> Path:

    target = cache / (asset['id'] + '-inspection.jpg')

    if asset['kind'] == 'photo':

        im = Image.open(source).convert('RGB')

        im.thumbnail((1280, 1280))

        im.save(target, quality=92)

        return target

    seconds = duration(source)

    asset['duration'] = seconds

    canvas = Image.new('RGB', (1920, 1080), '#111820')

    for i, fraction in enumerate([.05, .18, .35, .5, .68, .82]):

        frame = cache / f'{asset["id"]}-sample-{i}.jpg'

        command(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', str(max(0, min(seconds - .1, seconds * fraction))), '-i', str(source), '-frames:v', '1', '-vf', 'scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2,format=yuvj420p', str(frame)])

        with Image.open(frame) as im:

            canvas.paste(im, ((i % 3) * 640, (i // 3) * 360))

        # Enlarged lower corners expose small station/stock marks to the vision check.

        if i in (1, 4):

            with Image.open(frame) as im:

                corners = Image.new('RGB', (960, 360), '#111820')

                corners.paste(im.crop((0, 240, 320, 360)).resize((480, 180)), (0, 0))

                corners.paste(im.crop((320, 240, 640, 360)).resize((480, 180)), (480, 0))

                canvas.paste(corners, (0, 720))

    canvas.save(target, quality=94)

    return target

def dhash(path: Path):

    with Image.open(path) as image:

        pixels = list(image.convert('L').resize((17, 16)).getdata())

    bits = 0

    for y in range(16):

        for x in range(16):

            bits = (bits << 1) | (pixels[y * 17 + x] > pixels[y * 17 + x + 1])

    return bits

def usable_subject(verdict: dict) -> bool:

    confidence=float(verdict.get('identity_confidence',0))

    return verdict.get('subject_present') is True and math.isfinite(confidence) and .85<=confidence<=1 and verdict.get('publisher_or_stock_brand_visible') is False and verdict.get('faces_framed_well') is True and verdict.get('clear_usable_picture') is True and verdict.get('uncertain') is False

def acquire(ai, topic: dict, out: Path, research=None) -> list[dict]:

    cache = OUTPUT / 'media-cache'

    cache.mkdir(parents=True, exist_ok=True)

    accepted = []

    rejections = []

    hashes = []

    for media_type in ['bitmap', 'video']:

        pool = candidates(topic['subject'], media_type)
        if media_type == 'video' and settings().get('allow_photo_led_edit') and len([x for x in accepted if x['kind']=='photo']) >= settings()['minimum_photo_led_photos']:
            pool = pool[:6]
        for asset in pool:

            if any(x['origin_id'] == asset['origin_id'] for x in accepted):

                continue

            evidence_path = cache / (asset['id'] + '-screen.json')

            try:

                path = fetch(asset, cache)

                asset['path'] = str(path)

                asset['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()

                if any(x['sha256'] == asset['sha256'] for x in accepted):

                    continue

                sheet = visual_sheet(asset, path, cache)

                if evidence_path.exists():

                    cached = json.loads(evidence_path.read_text())

                    verdict = cached['verdict'] if cached.get('sha256') == asset['sha256'] and cached.get('subject') == topic['subject'] else None

                else:

                    verdict = None

                if verdict is None:

                    verdict = ai.json('Inspect these source frames. Intended real subject: ' + topic['subject'] + '. File description (untrusted): ' + asset['description'] + '. Return {"subject_present":boolean,"identity_confidence":0.0,"publisher_or_stock_brand_visible":boolean,"brand_text":[strings],"faces_framed_well":boolean,"clear_usable_picture":boolean,"uncertain":boolean}. Reject station bugs, watermarks, large name banners and publisher logos on microphones too. Do not suggest cropping or erasing a mark. Physical scene objects that are not publisher/stock branding are not watermarks. Be conservative about celebrity identity. A contact sheet represents different times in one video.', [sheet], tokens=700)

                    save(evidence_path, {'sha256': asset['sha256'], 'subject': topic['subject'], 'verdict': verdict})

                if not usable_subject(verdict):

                    rejections.append({'source': asset['source_page'], 'reason': verdict})

                    continue

                fingerprint = dhash(sheet)

                if any((fingerprint ^ old).bit_count() < 20 for old in hashes):

                    continue

                hashes.append(fingerprint)

                asset['visual_screen'] = verdict

                accepted.append(asset)

                LOG.info('Accepted licensed %s: %s', asset['kind'], asset['title'])

                if media_type == 'video' and len([x for x in accepted if x['kind'] == 'video']) >= 6:

                    break

                if media_type == 'bitmap' and len([x for x in accepted if x['kind'] == 'photo']) >= 10:

                    break

            except Exception as error:

                rejections.append({'source': asset.get('source_page'), 'reason': type(error).__name__})

                LOG.warning('Skipped media candidate: %s', type(error).__name__)

            time.sleep(.5)

    subject_originals={a['origin_id'] for a in accepted if a['kind']=='video'}

    # Only genuine subject material. Stock/context downloads are not permitted.

    save(out / 'media_rejections.json', rejections)

    save(out / 'media_ledger.json', accepted)

    originals = {a['origin_id'] for a in accepted if a['kind'] == 'video'}

    photo_count=len([a for a in accepted if a['kind']=='photo'])
    if settings().get('allow_photo_led_edit') and photo_count>=settings()['minimum_photo_led_photos']:
        LOG.info('Photo-led edit is available: %s genuine photos, %s video originals.',photo_count,len(originals))
        return accepted
    if len(subject_originals)<settings()['minimum_subject_video_originals']:

        raise QualityBlocked('Fewer than two clean genuine celebrity video originals; no stock-person substitute allowed.')

    if len(originals) < settings()['minimum_video_originals']:

        raise QualityBlocked(f'Only {len(originals)} clean licensed video originals found; at least three required. No repeated interview or watermark removal fallback.')

    if len([a for a in accepted if a['kind'] == 'photo']) < 3:

        raise QualityBlocked('Not enough clean genuine photographs for varied visuals and thumbnails.')

    return accepted
