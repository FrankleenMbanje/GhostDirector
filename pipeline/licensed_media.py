"""Subject media with explicit reuse evidence; no duration-based rights claims.

Commons file licenses are checked individually. A URL, a watermark-free image,
or a short/muted excerpt does not constitute permission. Unknown rights fail.
"""
import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

API = 'https://commons.wikimedia.org/w/api.php'
UA = 'GhostDirector/3.0 (https://github.com/FrankleenMbanje/GhostDirector; editorial media license verification)'


def plain(value):
    return html.unescape(re.sub(r'<[^>]+>', '', str(value or ''))).strip()


def commons_record(page, person):
    """Accept only subject-matching files with a specific reusable license.

    BY-SA is deliberately not enabled here: adaptations carry additional
    share-alike obligations. PD evidence retains its jurisdiction in the record.
    """
    info = (page.get('imageinfo') or [{}])[0]
    ext = info.get('extmetadata') or {}
    def val(k): return plain((ext.get(k) or {}).get('value'))
    title = page.get('title', '')
    if re.search(r'\b(album cover|single side|record label|sheet music|logo|signature|autograph|AI.generated|crowd|audience|tribute|mural|statue)\b', title, re.I):
        return None
    # A description may merely mention an influence or song: require the named
    # subject in the file title, rather than accepting another performer's clip.
    context = title.casefold()
    tokens = set(re.findall(r'\w+', person.casefold()))
    if not tokens or not tokens.issubset(set(re.findall(r'\w+', context))):
        return None
    license_name = val('LicenseShortName')
    license_url = val('LicenseUrl')
    if license_url.startswith('//'): license_url = 'https:' + license_url
    if re.fullmatch(r'CC BY (?:1\.0|2\.0|2\.5|3\.0|4\.0)', license_name):
        version = license_name.removeprefix('CC BY ')
        if not re.fullmatch(r'https://creativecommons\.org/licenses/by/' + re.escape(version) + r'/?', license_url):
            return None
        license_url = license_url.rstrip('/') + '/'
    elif license_name in ('CC0', 'CC0 1.0'):
        if license_url.rstrip('/') != 'https://creativecommons.org/publicdomain/zero/1.0':
            return None
        license_url = license_url.rstrip('/') + '/'
    elif license_name == 'Public domain':
        # Preserve the original file page as evidence; never infer PD from age.
        if val('Copyrighted').lower() != 'false' or not val('License'):
            return None
    else:
        return None
    source_page = info.get('descriptionurl', '')
    media_url = info.get('url', '')
    if urlparse(source_page).hostname != 'commons.wikimedia.org': return None
    if urlparse(media_url).hostname != 'upload.wikimedia.org': return None
    mime = info.get('mime', '')
    if not (mime.startswith('image/') or mime.startswith('video/')): return None
    if mime in ('image/svg+xml', 'image/gif'): return None
    creator = val('Artist')
    if not creator: return None
    return {
        'title': title.removeprefix('File:'), 'url': media_url,
        'source_page': source_page, 'creator': creator,
        'license': license_name, 'license_url': license_url,
        'license_evidence': {k: val(k) for k in ('License', 'Copyrighted', 'UsageTerms', 'Restrictions')},
        'mime': mime, 'width': info.get('width', 0), 'height': info.get('height', 0),
        'subject': person, 'checked_at': datetime.now(timezone.utc).isoformat(),
        'changes': 'Excerpted/cropped, resized, muted and overlaid with original narration/captions as applicable.',
    }


async def search_subject(person, kind='image'):
    params = {'action': 'query', 'format': 'json', 'generator': 'search',
              'gsrsearch': f'"{person}" filetype:{"video" if kind == "video" else "bitmap"}',
              'gsrnamespace': 6, 'gsrlimit': 20,
              'prop': 'imageinfo', 'iiprop': 'url|size|mime|extmetadata'}
    async with httpx.AsyncClient(timeout=30, headers={'User-Agent': UA}) as client:
        r = await client.get(API, params=params); r.raise_for_status()
    records = [commons_record(p, person) for p in r.json().get('query', {}).get('pages', {}).values()]
    return [r for r in records if r and r['mime'].startswith(kind + '/')
            and r['width'] >= 720 and r['height'] >= 400]


def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''): digest.update(block)
    return digest.hexdigest()


def credit(record):
    return (f"{record['title']} — {record['creator']} — {record['license']}\n"
            f"{record['source_page']}\n{record.get('license_url') or 'Public-domain evidence on source page'}\n"
            f"Changes: {record['changes']}")


def rights_issues(script, output_dir, description):
    """Check selected bytes and required attribution, including paired clips."""
    try:
        manifest = json.loads((Path(output_dir)/'media_rights.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ['Missing or unreadable media rights manifest']
    if not manifest.get('complete'): return ['Incomplete licensed celebrity media package']
    records = {str(Path(r['local_path']).resolve()): r for r in manifest.get('assets', [])}
    issues = []
    for s in script.scenes:
        paths = [getattr(s, k, None) for k in ('photo_path', 'video_path', 'broll_video_path')]
        if not any(paths): issues.append(f'Scene {s.scene_number}: no subject media')
        for path in filter(None, paths):
            r = records.get(str(Path(path).resolve()))
            if not r:
                issues.append(f'Scene {s.scene_number}: selected asset lacks license evidence'); continue
            try:
                if fingerprint(path) != r.get('sha256'):
                    issues.append(f'Scene {s.scene_number}: selected asset changed since license review')
            except OSError:
                issues.append(f'Scene {s.scene_number}: selected asset is missing')
            if credit(r) not in description:
                issues.append(f'Scene {s.scene_number}: required media attribution is missing')
    return list(dict.fromkeys(issues))


async def fetch_licensed_subjects(script, template, output_dir):
    """Real subject photos + footage, or stop before rendering/uploading.

    Never silently substitute generic stock, generated celebrity likenesses,
    unknown web photos or standard-licensed YouTube clips. Existing media without
    a matching license record and digest is not reused.
    """
    from pipeline.assets import download_file, valid_photo, valid_video
    from utils.ffmpeg_cmd import run_ffmpeg
    output_dir = Path(output_dir); scenes_dir = output_dir / 'scenes'
    scenes_dir.mkdir(parents=True, exist_ok=True)
    subjects = list(dict.fromkeys(p for s in script.scenes for p in s.people_to_show))
    if not subjects: raise RuntimeError('Licensed celebrity edit needs named subjects; no generic fallback permitted')
    cache = {}; used = set(); manifest = {'assets': [], 'policy': 'licensed_subjects', 'complete': False}
    target = output_dir / 'media_rights.json'
    target.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    kinds = set()
    for i, scene in enumerate(script.scenes):
        person = (scene.people_to_show or subjects)[0]
        choices = []
        for kind in (('video', 'image') if i % 2 == 0 else ('image', 'video')):
            key = (person, kind)
            if key not in cache: cache[key] = await search_subject(person, kind)
            choices.extend(cache[key])
        choices.sort(key=lambda r: (r['url'] in used, r['mime'].startswith('video/') != (i % 2 == 0)))
        record = next((r for r in choices if r['url'] not in used), None)
        if record is None:
            raise RuntimeError(f'Scene {scene.scene_number}: no unused licensed media for {person}; acquire subject media before publishing')
        is_video = record['mime'].startswith('video/')
        path = scenes_dir / f'scene_{scene.scene_number}_licensed.{"mp4" if is_video else "jpg"}'
        scene.photo_path = scene.video_path = scene.broll_video_path = None
        if is_video:
            raw = scenes_dir / f'scene_{scene.scene_number}_source.webm'
            await download_file(record['url'], raw)
            seconds = max(float(scene.audio_duration_seconds or scene.duration_target_seconds or 6), 1)
            run_ffmpeg(['-i',str(raw),'-t',str(seconds),'-an','-r','30',
                        '-c:v','libx264','-pix_fmt','yuv420p',str(path)], 'Licensed subject excerpt')
            if not valid_video(path): raise RuntimeError(f'Invalid licensed video: {path}')
            scene.video_path = str(path)
        else:
            raw = scenes_dir / f'scene_{scene.scene_number}_source_image'
            await download_file(record['url'], raw)
            from PIL import Image
            with Image.open(raw) as im: im.convert('RGB').save(path, quality=95)
            if not valid_photo(path, min_w=720, min_h=400): raise RuntimeError(f'Invalid licensed photo: {path}')
            scene.photo_path = str(path)
        scene.source_url = record['source_page']; scene.source_title = record['title']
        scene.source_channel = f"{record['creator']} | {record['license']} | {record['license_url']}"
        record.update({'local_path': str(path.resolve()), 'sha256': fingerprint(path), 'scene': scene.scene_number})
        manifest['assets'].append(record); kinds.add('video' if is_video else 'image'); used.add(record['url'])
        target.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    if kinds != {'video', 'image'}:
        raise RuntimeError('Celebrity edit requires both verified subject photographs and real subject footage; incomplete media package')
    manifest['complete'] = True
    target.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    (output_dir/'MEDIA_CREDITS.txt').write_text('\n\n'.join(credit(r) for r in manifest['assets']), encoding='utf-8')
    return script
