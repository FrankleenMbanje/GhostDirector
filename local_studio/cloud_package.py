"""Package review outputs only, never publish to YouTube or expose source secrets."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import requests

from .common import OUTPUT, ROOT, QualityBlocked


def public_research(value):
    if isinstance(value,dict):
        return {k:public_research(v) for k,v in value.items() if k not in {'quote','summary'}}
    if isinstance(value,list):return [public_research(v) for v in value]
    return value


def package():
    destination = ROOT/'output/cloud-download'
    selected = [OUTPUT/name for name in ['status.json', 'daily.log', 'doctor.json','model-server.log','ai_smoke.json']]
    allowed = {'.json', '.txt', '.srt', '.png', '.jpg'}
    for source in (OUTPUT/'runs').rglob('*'):
        if source.is_file() and source.suffix in allowed and 'timeline_media' not in source.parts:
            selected.append(source)
        elif source.name == 'FameFiles_local_review.mp4':
            selected.append(source)
    selected = list(dict.fromkeys(p for p in selected if p.is_file()))
    total = sum(p.stat().st_size for p in selected)
    # Do not silently exceed this workflow's portion of the free storage allowance.
    # The account-wide shared storage and budget must also be checked before activation.
    if total > 400*1024**2:
        raise QualityBlocked('Review package exceeds 400 MiB; no cloud artifact stored.')
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    token = os.environ.get('GH_TOKEN', '')
    if repository != 'FrankleenMbanje/GhostDirector' or not token:
        raise QualityBlocked('Cloud packaging requires the intended GitHub Actions repository.')
    used = 0
    page = 1
    while True:
        response = requests.get(f'https://api.github.com/repos/{repository}/actions/artifacts',
            params={'per_page':100, 'page':page},
            headers={'Authorization':'Bearer '+token, 'Accept':'application/vnd.github+json'}, timeout=30)
        response.raise_for_status()
        artifacts = response.json()['artifacts']
        used += sum(a['size_in_bytes'] for a in artifacts if not a.get('expired'))
        if len(artifacts) < 100:
            break
        page += 1
    if used+total > 450*1024**2:
        raise QualityBlocked('Repository artifacts would exceed 450 MiB. Existing files preserved; no new artifact stored.')
    for source in selected:
        target = destination/source.relative_to(OUTPUT)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix=='.json':
            data=json.loads(source.read_text(encoding='utf-8-sig'))
            if source.name=='research_sources.json':
                data=[{k:s[k] for k in ['id','url','title','domain','retrieved_at'] if k in s} for s in data]
            target.write_text(json.dumps(public_research(data),indent=2,ensure_ascii=False),encoding='utf-8')
        else:shutil.copyfile(source, target)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        state = json.loads((OUTPUT/'status.json').read_text()) if (OUTPUT/'status.json').exists() else {}
        with open(summary, 'a', encoding='utf-8') as handle:
            handle.write('## Fame Files review\n\n'+state.get('stage','Setup incomplete')+'\n\n'+state.get('message','See job logs.')+'\n\nNo stock footage. Cloud uploads are unlisted only. Download expires after one day.\n')


if __name__ == '__main__':
    package()
