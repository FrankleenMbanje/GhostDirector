from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import shutil
import sys
import os
import time
from pathlib import Path

from filelock import FileLock, Timeout

from .ai import LocalAI, ensure_server
from .common import LOG, OUTPUT, PACKAGE, ROOT, RUNTIME, QualityBlocked, browser_executable, now, save, settings, setup_logging, slug, space_check


def doctor() -> dict:
    problems = []
    for binary in ['ffmpeg', 'ffprobe', 'node']:
        if not shutil.which(binary):
            problems.append(f'Missing {binary}')
    modules=['bs4','ddgs','PIL','edge_tts'] if settings().get('voice_provider')=='edge' else ['kokoro_onnx','soundfile','espeakng_loader','bs4','ddgs','PIL']
    for module in modules:
        if importlib.util.find_spec(module) is None:
            problems.append(f'Missing Python package {module}')
    paths=[PACKAGE/'motion/node_modules/@remotion/cli/remotion-cli.js']
    if settings().get('voice_provider')!='edge':paths += [RUNTIME/'voice/kokoro-v1.0.onnx',RUNTIME/'voice/voices-v1.0.bin']
    for path in paths:
        if not path.exists():
            problems.append('Not installed: ' + str(path.relative_to(ROOT)))
    if not (ROOT/'assets/fonts/Montserrat-Bold.ttf').is_file():
        problems.append('Missing thumbnail font: assets/fonts/Montserrat-Bold.ttf')
    try:
        browser_executable()
    except QualityBlocked as error:
        problems.append(str(error))
    try:
        space_check()
    except QualityBlocked as error:
        problems.append(str(error))
    try:
        LocalAI().ready()
    except Exception as error:
        problems.append('Local AI: ' + str(error)[:250])
    return {'ready': not problems, 'problems': problems, 'timezone': settings()['timezone'], 'daily_start': '14:00', 'minimum_seconds': settings()['minimum_seconds'], 'paid_api_calls': False, 'uploads_enabled': False, 'checked_at': now().isoformat()}


def state(status, stage, message, **extra):
    value = {'status': status, 'stage': stage, 'message': message, 'updated_at': now().isoformat(), **extra}
    save(OUTPUT/'status.json', value)
    LOG.info('%s: %s', stage, message)


def credits(assets, out):
    lines=['THE FAME FILES — SOURCE CREDITS', '', 'Archive material illustrates the subject; it does not depict the current event unless explicitly documented.', 'Keep these credits with the published video description. No music or paid image-generation service is used.', '']
    for a in assets:
        lines.extend([a['title'], 'Creator: '+a['creator'], 'Source: '+a['source_page'], 'License: '+a['license']+' '+a.get('license_url',''), 'Changes: selected excerpt or photograph; fit/resize, original narration, captions and graphics. Publisher marks were not erased.', ''])
    lines.extend(['Original motion graphics: The Fame Files, rendered locally with Remotion.', 'Narration: '+settings()['voice']+' via '+settings().get('voice_provider','kokoro')+'. No paid voice API key is used.', 'Typography: Montserrat (existing local font); retain the SIL Open Font License when distributing the font itself.'])
    (out/'MEDIA_CREDITS.txt').write_text('\n'.join(lines),encoding='utf-8')


def cached_research(topic,out):
    from .research import allowed,domain,validate_facts
    research=json.loads((out/'research.json').read_text(encoding='utf-8'))
    sources=json.loads((out/'research_sources.json').read_text(encoding='utf-8'))
    cfg=settings()
    if research.get('editorial_curated') is not True or research['topic']['fingerprint']!=topic['fingerprint']:
        raise QualityBlocked('Research checkpoint lacks current editorial provenance.')
    if len(sources)<cfg['minimum_articles'] or len({domain(s['url']) for s in sources})<cfg['minimum_domains'] or any(not allowed(s['url']) or not s.get('text') for s in sources):
        raise QualityBlocked('Research checkpoint lacks its complete original source evidence.')
    facts=validate_facts(research['facts'],sources)
    used={e['source_id'] for f in facts for e in f['evidence']}
    if len(facts)!=len(research['facts']) or len(facts)<cfg['minimum_facts'] or len(used)<4 or len({domain(s['url']) for s in sources if s['id'] in used})<3:
        raise QualityBlocked('Research checkpoint does not pass exact evidence and independent-source checks.')
    return research


def daily(research_only=False,continue_research=False,editorial_package=None):
    setup_logging()
    OUTPUT.mkdir(parents=True,exist_ok=True)
    try:
        with FileLock(str(OUTPUT/'.daily.lock'),timeout=0):
            day=now().date().isoformat()
            receipt=OUTPUT/'runs'/day/'complete.json'
            if receipt.exists() and not research_only:
                previous=json.loads(receipt.read_text())
                if not Path(previous['video']).is_file():raise QualityBlocked('Today has a production receipt but its video is missing. Restore or review that project before creating a duplicate.')
                state('complete','Already complete','Today already has a local review video.',**previous)
                return 0
            ensure_server()
            state('setting_up','Checking local AI','Finishing the one-time model setup if necessary. Daily runs use the downloaded model.')
            LocalAI().install_if_missing()
            health=doctor()
            save(OUTPUT/'doctor.json',health)
            if not health['ready']:
                raise QualityBlocked('; '.join(health['problems']))
            from .research import discover, investigate, write_script
            from .media import acquire
            from .voice import narrate
            from .edit import render
            from .thumbnail import create_variants
            ai=LocalAI()
            history_path=OUTPUT/'history.json'
            history=json.loads(history_path.read_text()) if history_path.exists() else []
            state('running','Researching','Searching current celebrity reporting and comparing stories.')
            bundle=None
            if editorial_package:
                from .editorial import load_package
                bundle=load_package(editorial_package)
            selected=None
            if continue_research:
                marker=json.loads((OUTPUT/'runs'/day/'research_ready.json').read_text(encoding='utf-8'))
                if marker.get('github_run_id')!=os.environ.get('GITHUB_RUN_ID'):
                    raise QualityBlocked('Research continuation belongs to another GitHub run.')
                selected=marker['fingerprint']
                candidates=json.loads((OUTPUT/'runs'/day/'trend_candidates.json').read_text(encoding='utf-8'))
                if not any(t['fingerprint']==selected for t in candidates):raise QualityBlocked('Research continuation topic is missing.')
                candidates.sort(key=lambda t:t['fingerprint']!=selected)
            else:
                candidates=[bundle['topic']] if bundle else discover(ai,[x['title'] for x in history])
                save(OUTPUT/'runs'/day/'trend_candidates.json',candidates)
            failures=[]
            for topic in candidates:
                if any(x.get('fingerprint')==topic['fingerprint'] for x in history):
                    continue
                out=OUTPUT/'runs'/day/slug(topic['headline'])
                out.mkdir(parents=True,exist_ok=True)
                try:
                    state('running','Researching',topic['headline'],project=str(out))
                    if continue_research and topic['fingerprint']==selected:
                        research=cached_research(topic,out)
                    elif bundle:
                        from .editorial import prepare_research
                        research=prepare_research(bundle,out)
                    else:
                        research=investigate(ai,topic,out)
                    if research_only:
                        save(OUTPUT/'runs'/day/'research_ready.json',{'fingerprint':topic['fingerprint'],'github_run_id':os.environ.get('GITHUB_RUN_ID')})
                        state('research_complete','Research saved','Supported sources and facts saved; no video was produced.',project=str(out))
                        return 0
                    state('running','Checking visuals','Finding multiple clean licensed video originals and genuine photographs.',project=str(out))
                    assets=acquire(ai,topic,out,research)
                    state('running','Writing','Building a substantive long-form explanation from the documented facts.',project=str(out))
                    if bundle:
                        from .editorial import prepared_script
                        script=prepared_script(bundle,research,out)
                    else:
                        script=write_script(ai,research,out)
                    from .geography import prepare_maps
                    prepare_maps(ai,research,script,out,preferred_places=bundle.get('map_requests') if bundle else None)
                    state('running','Narrating','Generating the approved voice and measuring its real running time.',project=str(out))
                    timing=narrate(script,out)
                    state('running','Editing','Assembling varied footage, original graphics and timed captions.',project=str(out))
                    final=render(script,assets,timing,out)
                    state('running','Thumbnails','Composing three clean thumbnail options locally.',project=str(out))
                    photos=[a for a in assets if a['kind']=='photo']
                    if bundle and bundle.get('thumbnail_preferences'):
                        preferred=bundle['thumbnail_preferences']
                        photos.sort(key=lambda a:preferred.index(a['origin_id']) if a['origin_id'] in preferred else len(preferred))
                    thumbnails=create_variants(script,Path(photos[0]['path']),out/'thumbnails')
                    credits(assets,out)
                    result={'title':script['title'],'video':str(final),'thumbnails':[str(x) for x in thumbnails],'project':str(out),'duration_seconds':timing['duration'],'fingerprint':topic['fingerprint'],'review_required':True,'uploaded':False}
                    save(receipt,result)
                    history.append({'date':day,'title':script['title'],'fingerprint':topic['fingerprint'],'project':str(out)})
                    save(history_path,history)
                    state('complete','Ready for review','Local video and three thumbnails are ready.',**result)
                    return 0
                except Exception as error:
                    LOG.exception('Candidate did not pass production')
                    failures.append({'topic':topic['headline'],'reason':str(error)[:1200],'exception':type(error).__name__})
                    save(out/'blocked.json',failures[-1])
                    save(OUTPUT/'runs'/day/'candidate_failures.json',failures)
                    space_check()
            raise QualityBlocked('No researched topic passed all quality gates. See candidate_failures.json; no filler video or upload was produced.')
    except Timeout:
        LOG.info('A local production run is already active. Duplicate run skipped.')
        return 0
    except Exception as error:
        state('blocked','Needs attention',str(error)[:1800])
        LOG.exception('Daily production blocked')
        return 2


def main():
    parser=argparse.ArgumentParser(description='Free local Fame Files production; uploads remain disabled.')
    parser.add_argument('--daily',action='store_true')
    parser.add_argument('--doctor',action='store_true')
    parser.add_argument('--research-only',action='store_true')
    parser.add_argument('--continue-research',action='store_true')
    parser.add_argument('--editorial-package',help='Explicit dated package inside editorial/; scheduled runs research automatically.')
    parser.add_argument('--dashboard',action='store_true')
    parser.add_argument('--schedule-loop',action='store_true')
    args=parser.parse_args()
    if args.schedule_loop:
        return schedule_loop()
    if args.dashboard:
        from .dashboard import serve
        serve()
        return 0
    if args.doctor:
        result=doctor();save(OUTPUT/'doctor.json',result);print(json.dumps(result,indent=2));return 0 if result['ready'] else 2
    if args.daily or args.research_only:
        return daily(args.research_only,args.continue_research,args.editorial_package)
    parser.print_help()
    return 0


def schedule_loop():
    """Per-user sign-in service. One production attempt per date, with late-start catch-up."""
    setup_logging()
    try:
        with FileLock(str(OUTPUT/'.scheduler.lock'),timeout=0):
            while True:
                current=now()
                save(OUTPUT/'scheduler_heartbeat.json',{'pid':os.getpid(),'checked_at':current.isoformat(),'daily_start':'14:00','mode':'per-user sign-in','requires_awake_signed_in':True})
                day=current.date().isoformat()
                attempted=OUTPUT/'runs'/day/'scheduled_attempt.json'
                if current.hour>=settings()['daily_hour'] and not attempted.exists():
                    try:
                        ensure_server()
                        health=doctor()
                        save(OUTPUT/'doctor.json',health)
                        if health['ready']:
                            save(attempted,{'started_at':current.isoformat()})
                            daily()
                        else:
                            state('setting_up','Waiting for setup','The 2pm job is waiting for the local model or another required component. See doctor.json.')
                    except Exception as error:
                        state('blocked','Setup needs attention',str(error)[:1000])
                time.sleep(60)
    except Timeout:
        LOG.info('A local scheduler is already running.')
        return 0


if __name__=='__main__':
    sys.exit(main())
