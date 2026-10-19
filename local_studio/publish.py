"""Explicit cloud-only publishing; every upload must remain unlisted."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from .common import OUTPUT, QualityBlocked, duration, now, save

CHANNEL = 'UCkxIY_tv62QYMO5MOsBQp9w'


def service():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'FrankleenMbanje/GhostDirector' or os.environ.get('FAME_PUBLISH_UNLISTED') != 'true':
        raise QualityBlocked('Publishing requires the authorized cloud workflow and unlisted-only switch.')
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    try:
        info = json.loads(os.environ['GD_YOUTUBE_TOKEN'])
        credentials = Credentials.from_authorized_user_info(info)
        if not credentials.valid:
            credentials.refresh(Request())
        youtube = build('youtube', 'v3', credentials=credentials, cache_discovery=False)
        channels = youtube.channels().list(part='contentDetails', mine=True).execute().get('items', [])
    except Exception as error:
        raise QualityBlocked('YouTube authentication failed; refresh the dedicated GD_YOUTUBE_TOKEN secret. Details suppressed to protect credentials.') from None
    if len(channels) != 1 or channels[0]['id'] != CHANNEL:
        raise QualityBlocked('YouTube credential does not match The Fame Files. No upload attempted.')
    return youtube, channels[0]['contentDetails']['relatedPlaylists']['uploads']


def upload_body(result, project, marker):
    research = json.loads((project/'research_sources.json').read_text(encoding='utf-8'))
    assets = json.loads((project/'media_ledger.json').read_text(encoding='utf-8'))
    parts = ['An original, sourced celebrity news explainer from The Fame Files.',
             'Archive visuals illustrate the subject; they do not depict the current event unless stated.',
             '\nReporting sources:']
    parts += [s['url'] for s in research]
    parts += ['\nVisual credits (selected excerpts/photographs; resized with original narration, captions and graphics):']
    parts += [f"{a['creator']} | {a['license']} | {a['source_page']}" for a in assets]
    parts += sorted({a.get('license_url','') for a in assets if a.get('license_url')})
    parts += ['Original motion graphics: The Fame Files. AI-assisted narration.', marker]
    description = '\n'.join(parts)
    if len(description.encode('utf-8')) > 5000:
        raise QualityBlocked('Required source credits exceed YouTube description capacity; no attribution was silently removed.')
    return {'snippet': {'title': result['title'], 'description': description,
                        'categoryId': '24', 'defaultLanguage': 'en'},
            'status': {'privacyStatus': 'unlisted', 'selfDeclaredMadeForKids': False,
                       'containsSyntheticMedia': True}}


def verified_unlisted(youtube, video_id):
    items = youtube.videos().list(part='status,snippet', id=video_id).execute().get('items', [])
    if len(items) != 1 or items[0]['snippet']['channelId'] != CHANNEL:
        raise QualityBlocked('Uploaded video could not be verified on The Fame Files.')
    status = items[0]['status']
    if status.get('privacyStatus') != 'unlisted' or status.get('publishAt'):
        raise QualityBlocked('YouTube did not confirm unlisted visibility without a public release schedule.')
    if status.get('uploadStatus') in {'failed', 'rejected', 'deleted'}:
        raise QualityBlocked('YouTube rejected or failed the video processing.')
    return status.get('uploadStatus', 'unknown')


def publish():
    youtube, playlist = service()
    day = now().date().isoformat()
    receipt = OUTPUT/'runs'/day/'complete.json'
    if not receipt.is_file():
        raise QualityBlocked('No completed five-minute video exists for today. No upload attempted.')
    result = json.loads(receipt.read_text(encoding='utf-8'))
    project = Path(result['project']).resolve()
    video = Path(result['video']).resolve()
    if not project.is_relative_to((OUTPUT/'runs').resolve()) or not video.is_relative_to(project):
        raise QualityBlocked('Production receipt points outside this workflow output.')
    quality = json.loads((project/'quality_report.json').read_text(encoding='utf-8'))
    if quality.get('status') != 'local_review_ready' or quality.get('full_decode') != 'pass' or quality.get('blank_intervals') != 0 or not 300 <= duration(video) <= 540:
        raise QualityBlocked('Only a decoded, checked five-minute-plus video can be published.')
    marker = 'FameFiles production key: '+day+'-'+hashlib.sha256(result['fingerprint'].encode()).hexdigest()[:24]
    video_id = None
    # Recover accepted uploads after connection failures instead of duplicating them.
    recent = youtube.playlistItems().list(part='snippet', playlistId=playlist, maxResults=50).execute()
    for item in recent.get('items', []):
        if marker in item['snippet'].get('description', ''):
            video_id = item['snippet']['resourceId']['videoId']
            break
    pending = project/'upload_pending.json'
    if pending.exists():
        saved = json.loads(pending.read_text())
        if saved.get('marker') == marker:
            video_id = saved['video_id']
    if not video_id:
        from googleapiclient.http import MediaFileUpload
        request = youtube.videos().insert(part='snippet,status', body=upload_body(result, project, marker),
            media_body=MediaFileUpload(str(video), mimetype='video/mp4', chunksize=8*1024**2, resumable=True), notifySubscribers=False)
        response = None
        while response is None:
            _, response = request.next_chunk(num_retries=3)
        video_id = response['id']
        save(pending, {'video_id': video_id, 'marker': marker})
    processing = verified_unlisted(youtube, video_id)
    # Thumbnail failure must not cause a duplicate video on retry.
    if result.get('thumbnails'):
        from googleapiclient.http import MediaFileUpload
        thumbnail = Path(result['thumbnails'][0]).resolve()
        if not thumbnail.is_relative_to(project):
            raise QualityBlocked('Thumbnail path is outside the completed project.')
        youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumbnail))).execute(num_retries=3)
    verified_unlisted(youtube, video_id)
    result.update(uploaded=True, privacy='unlisted', youtube_id=video_id,
                  youtube_url='https://www.youtube.com/watch?v='+video_id,
                  youtube_processing=processing, review_required=True)
    save(receipt, result)
    save(project/'published.json', result)
    save(OUTPUT/'status.json', {'status':'complete','stage':'Uploaded unlisted','message':result['youtube_url'],**result})
    print('Verified unlisted upload:', result['youtube_url'], flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    if args.check:
        youtube, playlist=service()
        prefix='FameFiles production key: '+now().date().isoformat()+'-'
        recent=youtube.playlistItems().list(part='snippet',playlistId=playlist,maxResults=50).execute()
        already=False
        for item in recent.get('items',[]):
            if prefix in item['snippet'].get('description',''):
                video_id=item['snippet']['resourceId']['videoId']
                verified_unlisted(youtube,video_id)
                save(OUTPUT/'status.json',{'status':'complete','stage':'Already uploaded unlisted','message':'https://www.youtube.com/watch?v='+video_id})
                already=True;break
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as output:
                output.write('already_published='+str(already).lower()+'\n')
        print('YouTube channel verified: The Fame Files. Upload policy: unlisted only. Already published today:',already)
    else:
        publish()


if __name__ == '__main__':
    main()
