from __future__ import annotations

import collections
import json
import math
import re
import shutil
from pathlib import Path

from .common import LOG, PACKAGE, ROOT, QualityBlocked, browser_executable, command, duration, probe, save, settings, space_check


def plan_shots(assets: list[dict], script: dict, timing: dict) -> list[dict]:
    cfg = settings()
    total = timing['duration']
    videos = [a for a in assets if a['kind'] == 'video' and a.get('duration', 0) >= 9]
    photos = [a for a in assets if a['kind'] == 'photo']
    if len({v['origin_id'] for v in videos}) < cfg['minimum_video_originals']:
        raise QualityBlocked('Three independently sourced moving videos are required.')
    offsets = {v['id']: min(max(2,v['duration']*.05),max(0,v['duration']-10)) for v in videos}
    used = collections.defaultdict(float)
    shots = []
    photo_index = 0
    last_origin = None
    for chapter_time in timing['chapters']:
        chapter = chapter_time['index']
        cursor = chapter_time['start']
        end = chapter_time['end']
        # A brief original graphic supports each chapter; never an extended filler card.
        graphic_length = min(6.5, (end-cursor)*.13)
        graphic_at = cursor + min(12, (end-cursor)*.3)
        graphic_done = False
        while cursor < end - .02:
            left = end - cursor
            if not graphic_done and cursor >= graphic_at:
                seconds = min(graphic_length, left)
                shots.append({'kind':'graphic','chapter':chapter,'start':cursor,'duration':seconds,'origin_id':f'graphic:{chapter}'})
                graphic_done = True
                cursor += seconds
                last_origin = None
                continue
            seconds = min([8.2, 10.4, 7.6, 11.2][len(shots)%4], left)
            beat_targets=[c['end'] for c in timing.get('cues',[]) if cursor+4<=c['end']<=end and abs(c['end']-(cursor+seconds))<=1.25]
            if beat_targets:seconds=min(beat_targets,key=lambda t:abs(t-(cursor+seconds)))-cursor
            if left-seconds < 2:
                seconds = left
            # Use each photo at most once. Video originals recur only as different, nonoverlapping portions.
            use_photo = photo_index < len(photos) and len(shots)%6 == 4 and left >= 4
            if use_photo:
                asset = photos[photo_index];photo_index += 1
                seconds = min(6.5,left)
                source_start = 0
            else:
                context_used=sum(used[v['origin_id']] for v in videos if v.get('role')=='context')
                eligible = [v for v in videos if v['origin_id'] != last_origin and v['duration']-offsets[v['id']] >= seconds and used[v['origin_id']]+seconds <= cfg['maximum_source_share']*total and (v.get('role')!='context' or context_used+seconds<=cfg['maximum_context_share']*total)]
                if not eligible:
                    raise QualityBlocked('Not enough distinct clean footage to finish without loops, repeated shots or an overused source.')
                asset = min(eligible,key=lambda v:used[v['origin_id']])
                source_start = offsets[asset['id']]
                offsets[asset['id']] += seconds + .3
            used[asset['origin_id']] += seconds
            shots.append({'kind':asset['kind'],'role':asset.get('role','subject'),'asset_id':asset['id'],'origin_id':asset['origin_id'],'path':asset['path'],'source_start':source_start,'start':cursor,'duration':seconds,'chapter':chapter})
            cursor += seconds
            last_origin = asset['origin_id']
    validate_mix(shots, total)
    # Quantize boundaries, not individual durations, so there are no accumulating frame gaps.
    for i, shot in enumerate(shots):
        shot['start_frame'] = round(shot['start']*cfg['fps'])
        end = round((shots[i+1]['start'] if i+1<len(shots) else total)*cfg['fps'])
        shot['frames'] = end-shot['start_frame']
    return shots


def validate_mix(shots: list[dict], total: float):
    cfg=settings()
    videos=[s for s in shots if s['kind']=='video']
    if len({s['origin_id'] for s in videos}) < cfg['minimum_video_originals']:
        raise QualityBlocked('Video-original diversity failed.')
    if len({s['origin_id'] for s in videos if s.get('role','subject')=='subject'})<cfg['minimum_subject_video_originals']:
        raise QualityBlocked('Too few genuine celebrity video originals.')
    if sum(s['duration'] for s in videos if s.get('role')=='context')/total>cfg['maximum_context_share']+.0001:
        raise QualityBlocked('Too much contextual stock footage.')
    if sum(s['duration'] for s in videos)/total < cfg['minimum_video_share']:
        raise QualityBlocked('Too little genuine moving footage.')
    if sum(s['duration'] for s in shots if s['kind']=='graphic')/total > cfg['maximum_graphics_share']:
        raise QualityBlocked('Too much graphic-only screen time.')
    by_origin=collections.defaultdict(float)
    intervals=collections.defaultdict(list)
    for s in shots:
        if s['kind'] != 'graphic':by_origin[s['origin_id']]+=s['duration']
        if s['kind']=='video':intervals[s['origin_id']].append((s['source_start'],s['source_start']+s['duration']))
    if any(v/total > cfg['maximum_source_share']+.0001 for v in by_origin.values()):
        raise QualityBlocked('One original dominates the mix.')
    for spans in intervals.values():
        spans.sort()
        if any(a[1]>b[0]+.001 for a,b in zip(spans,spans[1:])):
            raise QualityBlocked('Repeated source footage detected.')


def render(script: dict, assets: list[dict], timing: dict, out: Path) -> Path:
    cfg=settings();fps=cfg['fps']
    shots=plan_shots(assets,script,timing)
    save(out/'edit_plan.json',shots)
    clips=out/'timeline_media';clips.mkdir(exist_ok=True)
    shutil.copyfile(ROOT/'assets/fonts/Montserrat-Bold.ttf',out/'render-font.ttf')
    labeled_sources=set()
    asset_map={a['id']:a for a in assets}
    for i,shot in enumerate(shots):
        space_check()
        target=clips/f'{i+1:03}.mp4';shot['rendered_file']=str(target)
        if shot['kind']=='graphic':
            chapter=script['chapters'][shot['chapter']]
            props=clips/f'{i+1:03}-props.json'
            save(props,{'headline':chapter['graphic_headline'],'detail':chapter['graphic_detail'],'eyebrow':chapter['heading'].upper()[:50],'variant':shot['chapter'],'kind':chapter.get('graphic_kind','statement'),'points':chapter.get('graphic_points',[])})
            cli=PACKAGE/'motion/node_modules/@remotion/cli/remotion-cli.js'
            command(['node',str(cli),'render',str(PACKAGE/'motion/src/index.ts'),'GraphicPlate',str(target),'--props',str(props),'--frames',f'0-{shot["frames"]-1}','--concurrency=2','--crf=18','--browser-executable='+browser_executable()],cwd=PACKAGE/'motion')
        else:
            args=['ffmpeg','-hide_banner','-loglevel','error','-y']
            if shot['kind']=='photo':args+=['-loop','1','-framerate',str(fps)]
            else:args+=['-ss',str(shot['source_start'])]
            args+=['-i',shot['path']]
            # Preserve the complete source frame; no watermark cropping or erasing.
            filters=f'[0:v]split=2[bg][fg];[bg]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,gblur=sigma=30,eq=brightness=-0.13[back];[fg]scale=1920:1080:force_original_aspect_ratio=decrease[front];[back][front]overlay=(W-w)/2:(H-h)/2,fps={fps},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v]'
            if shot['kind']=='video' and shot['origin_id'] not in labeled_sources:
                original=asset_map[shot['asset_id']]
                year=re.search(r'\b(?:19|20)\d{2}\b',original.get('archive_date',''))
                label=('LOCATION CONTEXT' if original.get('role')=='context' else 'ARCHIVE'+(' '+year[0] if year else ''))
                (clips/f'{i+1:03}-archive.txt').write_text(label,encoding='utf-8')
                filters=filters.removesuffix('[v]')+f",drawtext=fontfile=render-font.ttf:textfile='timeline_media/{i+1:03}-archive.txt':fontcolor=white:fontsize=23:x=60:y=55:box=1:boxcolor=black@0.45:boxborderw=9:enable='lt(t,2)'[v]"
                labeled_sources.add(shot['origin_id'])
            args+=['-filter_complex',filters,'-map','[v]','-frames:v',str(shot['frames']),'-an','-c:v','libx264','-preset','fast','-crf','18','-threads','2',str(target)]
            command(args,cwd=out)
        stream=next(s for s in probe(target)['streams'] if s['codec_type']=='video')
        if int(stream['nb_frames'])!=shot['frames']:
            raise QualityBlocked('Picture clip frame count mismatch.')
        LOG.info('Rendered shot %s/%s',i+1,len(shots))
    save(out/'edit_plan.json',shots)
    concat=out/'picture_list.txt'
    concat.write_text('\n'.join("file '"+str(Path(s['rendered_file']).as_posix()).replace("'","'\\''")+"'" for s in shots),encoding='utf-8')
    picture=out/'picture.mp4'
    command(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(concat),'-c','copy',str(picture)])
    picture_audit=command(['ffmpeg','-hide_banner','-xerror','-i',str(picture),'-vf','blackdetect=d=0.25:pix_th=0.04','-an','-f','null','-'])
    if 'black_start:' in picture_audit.stderr:raise QualityBlocked('Blank picture detected before captions could obscure it.')
    final=out/'FameFiles_local_review.mp4'
    style="subtitles=captions.srt:force_style='FontName=Arial,FontSize=20,PrimaryColour=&HFFFFFF,OutlineColour=&H202020,Outline=1.5,Shadow=0,MarginV=32'"
    command(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(picture),'-i',str(out/'narration.wav'),'-map','0:v','-map','1:a','-vf',style,'-af','loudnorm=I=-16:TP=-2:LRA=7','-c:v','libx264','-preset','fast','-crf','18','-threads','2','-c:a','aac','-b:a','192k','-t',str(timing['duration']),'-movflags','+faststart',str(final)],cwd=out)
    # Decode the entire actual export. Do not infer a pass from successful encoding.
    audit=command(['ffmpeg','-hide_banner','-xerror','-i',str(final),'-vf','blackdetect=d=0.25:pix_th=0.04','-af','volumedetect','-f','null','-'])
    (out/'quality_check.log').write_text(audit.stderr,encoding='utf-8')
    measured=duration(final)
    if measured<cfg['minimum_seconds'] or measured>cfg['maximum_seconds'] or 'black_start:' in audit.stderr:
        raise QualityBlocked('Final duration or blank-picture check failed.')
    save(out/'quality_report.json',{'status':'local_review_ready','duration_seconds':measured,'full_decode':'pass','blank_intervals':0,'video_originals':len({s['origin_id'] for s in shots if s['kind']=='video'}),'picture_clips':len(shots),'automatic_publication':False,'human_review':'Review picture, narration and rights before publishing; automated screening is not a guarantee.'})
    command(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(final),'-vf',f'fps=1/{max(1,measured/12)},scale=480:-1,tile=4x3','-frames:v','1',str(out/'contact_sheet.png')])
    return final
