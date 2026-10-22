"""Dated, committed editorial packages for an explicitly requested review run."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from .common import ROOT,QualityBlocked,now,save,settings
from .research import allowed,domain,fetch_article,fresh,mentions,normalized,validate_facts


def validate_script(script,facts):
    ids={f['id'] for f in facts};used=set()
    if not isinstance(script.get('title'),str) or not 10<=len(script['title'])<=100:
        raise QualityBlocked('Editorial title is missing or too long.')
    copies=script.get('thumbnail_words',[])
    if len(copies)!=3 or any(not isinstance(t,str) or not 1<=len(t.split())<=4 or len(t)>32 for t in copies):
        raise QualityBlocked('Editorial package needs three short thumbnail options.')
    chapters=script.get('chapters',[])
    if len(chapters)!=6:raise QualityBlocked('Editorial script needs six chapters.')
    for chapter in chapters:
        body=chapter.get('narration','');cited=chapter.get('fact_ids',[])
        if not isinstance(body,str) or not 115<=len(body.split())<=195:
            raise QualityBlocked('Editorial narration fails chapter length checks.')
        if not 2<=len(cited)<=5 or any(type(i) is not int or i not in ids for i in cited) or len(set(cited))!=len(cited):
            raise QualityBlocked('Editorial chapter has missing or invalid fact citations.')
        used.update(cited)
        if chapter.get('graphic_kind') not in {'statement','timeline','comparison'} or not 1<=len(chapter.get('graphic_headline','').split())<=8 or len(chapter.get('graphic_detail','').split())>18:
            raise QualityBlocked('Editorial graphic copy is invalid or too dense.')
        points=chapter.get('graphic_points',[])
        if len(points)>3 or any(len(p.get('label','').split())>3 or len(p.get('detail','').split())>6 for p in points):
            raise QualityBlocked('Editorial graphic labels are too dense.')
    if sum(len(c['narration'].split()) for c in chapters)<settings()['minimum_words'] or len(used)<settings()['minimum_facts']:
        raise QualityBlocked('Editorial script lacks substantive supported coverage.')
    return script


def load_package(path):
    target=(ROOT/path).resolve()
    if not target.is_relative_to((ROOT/'editorial').resolve()) or not target.is_file() or target.stat().st_size>2_000_000:
        raise QualityBlocked('Editorial packages must be small committed files inside editorial/.')
    data=json.loads(target.read_text(encoding='utf-8'))
    if data.get('date')!=now().date().isoformat():
        raise QualityBlocked('Editorial package is stale or dated for another day.')
    if data.get('prepared_by')!='Codex' or data.get('review_mode')!='source-reviewed-editorial':
        raise QualityBlocked('Editorial package lacks its declared review provenance.')
    topic=data['topic']
    if topic.get('fingerprint')!=hashlib.sha256(normalized(topic['headline']).encode()).hexdigest():
        raise QualityBlocked('Editorial topic fingerprint is invalid.')
    if [f.get('id') for f in data['facts']]!=list(range(1,len(data['facts'])+1)):
        raise QualityBlocked('Editorial facts need stable sequential identifiers.')
    visual_ids=data.get('visual_source_ids')
    if visual_ids is not None and (not isinstance(visual_ids,list) or not 6<=len(visual_ids)<=12 or len(set(visual_ids))!=len(visual_ids) or any(not isinstance(i,str) or not i.isdecimal() for i in visual_ids)):
        raise QualityBlocked('Editorial visual shortlist must contain six to twelve distinct Commons page IDs.')
    preferred=data.get('thumbnail_preferences',[])
    if not isinstance(preferred,list) or len(preferred)>5 or any(not isinstance(i,str) or len(i)>100 for i in preferred):
        raise QualityBlocked('Editorial thumbnail preferences are invalid.')
    places=data.get('map_requests')
    facts={f['id']:f['claim'] for f in data['facts']}
    if places is not None and (not isinstance(places,list) or len(places)>2 or any(not isinstance(p.get('name'),str) or not 1<=len(p['name'])<=80 or not mentions(p['name'],facts.get(p.get('fact_id'),'')) for p in places)):
        raise QualityBlocked('Editorial map requests must name places literally present in verified claims.')
    validate_script(data['script'],data['facts'])
    data['sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
    return data


def prepare_research(bundle,out):
    cfg=settings();sources=[];seen=set()
    ids=[s.get('id') for s in bundle['sources']]
    if any(type(i) is not int or i<1 for i in ids) or len(ids)!=len(set(ids)):
        raise QualityBlocked('Editorial source identifiers are invalid or duplicated.')
    for expected in bundle['sources']:
        url=expected['url']
        if url in seen or not allowed(url):raise QualityBlocked('Editorial sources contain duplicate or untrusted URLs.')
        seen.add(url)
        article=fetch_article(url)
        if article and mentions(bundle['topic']['subject'],article['title']+' '+article['text'][:5000]):
            article['id']=expected['id'];sources.append(article)
    save(out/'research_sources.json',sources)
    if len(sources)<cfg['minimum_articles'] or len({s['domain'] for s in sources})<cfg['minimum_domains']:
        raise QualityBlocked('Editorial sources fail article and domain checks.')
    if len({s['domain'] for s in sources if fresh(s.get('published_at') or '')})<2:
        raise QualityBlocked('Editorial story lacks fresh reporting from two independent publishers.')
    facts=validate_facts(bundle['facts'],sources)
    if len(facts)!=len(bundle['facts']) or [f['claim'] for f in facts]!=[f['claim'] for f in bundle['facts']] or len(facts)<cfg['minimum_facts']:
        raise QualityBlocked('Every prepared fact must retain its original supporting source excerpt.')
    if any(len(f['evidence'])!=len(original['evidence']) for f,original in zip(facts,bundle['facts'])):
        raise QualityBlocked('An editorial supporting excerpt no longer matches its source.')
    used={e['source_id'] for f in facts for e in f['evidence']}
    if len(used)<4 or len({s['domain'] for s in sources if s['id'] in used})<3:
        raise QualityBlocked('Editorial facts lack independent reporting.')
    validate_script(bundle['script'],facts)
    research={'topic':bundle['topic'],'facts':facts,'unknowns':bundle.get('unknowns',[]),'source_count':len(sources),'domains':sorted({s['domain'] for s in sources}),'editorial_curated':True,'editorial_package_sha256':bundle['sha256'],'editorial_prepared_by':bundle['prepared_by']}
    save(out/'research.json',research)
    return research


def prepared_script(bundle,research,out):
    if research.get('editorial_package_sha256')!=bundle['sha256']:
        raise QualityBlocked('Editorial continuation does not match its verified research package.')
    script=validate_script(bundle['script'],research['facts'])
    script['research_topic']=research['topic']
    save(out/'script.json',script)
    (out/'script.txt').write_text('\n\n'.join(c['narration'] for c in script['chapters']),encoding='utf-8')
    return script
