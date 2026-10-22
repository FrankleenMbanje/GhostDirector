from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

from .common import LOG, QualityBlocked, now, save, settings

NEWS_DOMAINS = {'apnews.com', 'reuters.com', 'bbc.com', 'bbc.co.uk', 'abcnews.com', 'abcnews.go.com', 'nbcnews.com', 'cbsnews.com', 'cnn.com', 'people.com', 'variety.com', 'hollywoodreporter.com', 'billboard.com', 'ew.com', 'deadline.com', 'rollingstone.com', 'theguardian.com', 'cnbc.com', 'npr.org', 'bop.gov', 'justice.gov'}
NEWS_DOMAINS.update({'parade.com','grammy.com','oscars.org','universalmusic.com','warnermusic.com','sonymusic.com'})


def name_words(text):
    plain=''.join(c for c in unicodedata.normalize('NFKD',text.casefold()) if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+',' ',plain).strip()


def mentions(subject,text):
    return ' '+name_words(subject)+' ' in ' '+name_words(text)+' '


def reporting_priority(item,results):
    stop={'the','a','an','and','with','from','about','to','of','for','in','on','at','as','by','new','says','show','star','after','is','it','you','his','her','he','she'}
    words=set(name_words(item['title']).split())-stop
    corroboration=set()
    for other in results:
        terms=set(name_words(other['title']).split())-stop
        if domain(other['url'])!=domain(item['url']) and len(words&terms)/max(1,len(words|terms))>=.2:
            corroboration.add(domain(other['url']))
    business=any(w in name_words(item['title']).split() for w in ['licensing','signs','executives','producer','industry','deal'])
    return len(corroboration)-.25*business

NEWS_FEEDS = ['https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml',
              'https://variety.com/feed/', 'https://deadline.com/feed/',
              'https://www.billboard.com/feed/', 'https://www.rollingstone.com/feed/',
              'https://www.theguardian.com/culture/rss']


def feed_news(url):
    """Publisher RSS fallback when search providers throttle or return undated news."""
    found=[]
    try:
        response=requests.get(url,timeout=(10,20),headers={'User-Agent':'FameFilesLocalStudio/1.0 (news research)'})
        response.raise_for_status()
        if len(response.content)>3_000_000:return []
        root=ET.fromstring(response.content)
        for item in root.findall('.//item'):
            link=item.findtext('link','').strip()
            try:date=parsedate_to_datetime(item.findtext('pubDate','')).isoformat()
            except (TypeError,ValueError,IndexError):continue
            title=item.findtext('title','').strip()
            if allowed(link) and fresh(date) and title:
                found.append({'title':title,'url':link,'date':date,'summary':BeautifulSoup(item.findtext('description',''),'html.parser').get_text(' ',strip=True)[:300]})
            if len(found)>=30:break
    except (requests.RequestException,ET.ParseError):
        LOG.warning('Publisher feed unavailable: %s',urlparse(url).hostname)
    return found


def domain(url):
    return urlparse(url).hostname.lower().removeprefix('www.') if urlparse(url).hostname else ''


def allowed(url):
    d = domain(url)
    return urlparse(url).scheme == 'https' and any(d == x or d.endswith('.' + x) for x in NEWS_DOMAINS)


def normalized(text):
    return re.sub(r'\s+', ' ', text).strip().casefold().replace('\u2019', "'").replace('\u201c', '"').replace('\u201d', '"')


def fresh(value):
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        age = now() - date
        return timedelta(hours=-2) <= age <= timedelta(hours=settings()['freshness_hours'])
    except (TypeError, ValueError):
        return False


def discover(ai, excluded: list[str]) -> list[dict]:
    results = []
    seen = set()
    for query in ['celebrity music actor entertainment news', 'Hollywood celebrity breaking news', 'music artist singer latest news']:
        try:
            found = DDGS(timeout=20).news(query, timelimit='d', max_results=18)
            for item in found:
                url = item.get('url', '')
                if allowed(url) and url not in seen and fresh(item.get('date')):
                    results.append({'id': len(results), 'title': item['title'], 'url': url, 'date': item['date'], 'summary': item.get('body', '')[:450]})
                    seen.add(url)
        except Exception as error:
            LOG.warning('News discovery query failed: %s', type(error).__name__)
    if len(results)<12:
        with ThreadPoolExecutor(max_workers=6) as pool:
            feeds=list(pool.map(feed_news,NEWS_FEEDS))
        # Interleave publishers so a single large feed cannot fill the context.
        for index in range(30):
            for feed in feeds:
                if index<len(feed) and feed[index]['url'] not in seen:
                    item=feed[index]
                    results.append({'id':len(results),**item});seen.add(item['url'])
                if len(results)>=160:break
            if len(results)>=160:break
    if len(results) < 5:
        raise QualityBlocked('Not enough fresh, dated reporting from trusted news sources.')
    ranked=sorted(results,key=lambda r:reporting_priority(r,results),reverse=True)
    ranking=[{k:r[k] for k in ['id','title','date']}|{'summary':r['summary'][:160]} for r in ranked[:36]]
    # A stable editorial roster supplies grounded fallbacks when a small local model returns only one topic.
    roster=['Charli XCX','Taylor Swift','Beyoncé','Beyonce','Rihanna','Drake','Kendrick Lamar','Kanye West','Sean Combs','Diddy','Nicki Minaj','Cardi B','Ariana Grande','Selena Gomez','Justin Bieber','Billie Eilish','Sabrina Carpenter','Olivia Rodrigo','Lady Gaga','Miley Cyrus','Britney Spears','Madonna','Shakira','Bad Bunny','Harry Styles','Adele','Dua Lipa','The Weeknd','Bruno Mars','Travis Scott','Megan Thee Stallion','Doja Cat','SZA','Usher','Nick Jonas','Joe Jonas','Demi Lovato','Jennifer Lopez','Ben Affleck','Jennifer Aniston','Angelina Jolie','Brad Pitt','Leonardo DiCaprio','Tom Cruise','Zendaya','Tom Holland','Robert Pattinson','Kristen Stewart','Ryan Reynolds','Blake Lively','Kim Kardashian','Kylie Jenner','Kendall Jenner','Khloé Kardashian','Kris Jenner','Julia Stiles','Anne Hathaway','Scarlett Johansson','Florence Pugh','Margot Robbie','Johnny Depp','Amber Heard','Will Smith','Jada Pinkett Smith','Denzel Washington','Jamie Foxx','Kevin Hart','Dwayne Johnson','Timothée Chalamet','Jenna Ortega','Sydney Sweeney','Millie Bobby Brown','Paul McCartney','Ringo Starr','Elton John','Bruce Springsteen','Dolly Parton','Cher','Christina Applegate','Christina Aguilera','Meghan Markle','Prince Harry','Prince William','Kate Middleton','David Beckham','Victoria Beckham','Travis Kelce','Heath Ledger']
    drafts=[]
    for name in roster:
        rows=[r for r in ranked if mentions(name,r['title']+' '+r['summary'])]
        if len({domain(r['url']) for r in rows})>=2:
            drafts.append({'subject':name,'news_ids':[r['id'] for r in rows]})
    if len(drafts)<3:
        schema={'type':'object','additionalProperties':False,'required':['topics'],'properties':{'topics':{'type':'array','minItems':3,'maxItems':6,'items':{'type':'object','additionalProperties':False,'required':['subject','news_ids'],'properties':{'subject':{'type':'string','minLength':3,'maxLength':80},'news_ids':{'type':'array','minItems':1,'maxItems':6,'items':{'type':'integer','enum':[r['id'] for r in ranking]}}}}}}}
        answer = ai.json('Today is ' + now().isoformat() + '. Rank six DISTINCT fresh celebrity stories. Prioritize widely followed people and stories corroborated by multiple publishers. Prefer substantive updates, not narrow production announcements, signing deals or industry promotions. Never repeat the same event under a second person\'s name. Avoid these already produced topics: ' + json.dumps(excluded[-30:]) + '. Return {"topics":[{"subject":"celebrity full name appearing in evidence","news_ids":[integers]}]}. Every news_id must be in this evidence. Do not invent people.\n' + json.dumps(ranking), tokens=2200,schema=schema)
        drafts.extend(answer.get('topics',[]))
    def topic_priority(topic):
        rows=[r for r in ranked if r['id'] in topic.get('news_ids',[]) and mentions(topic.get('subject',''),r['title']+' '+r['summary'])]
        return (len({domain(r['url']) for r in rows}),max((reporting_priority(r,results) for r in rows),default=-1))
    drafts.sort(key=topic_priority,reverse=True)
    topics = []
    selected_urls=set();fingerprints=set()
    for topic in drafts:
        ids = topic.get('news_ids', [])
        subject=topic.get('subject','')
        if not isinstance(subject,str) or not subject.strip() or not isinstance(ids,list):continue
        selected = [r for r in ranked if r['id'] in ids and mentions(subject,r['title']+' '+r['summary'])]
        if not selected:
            continue
        headline=selected[0]['title']
        selected += [r for r in ranked if r not in selected and mentions(subject,r['title']+' '+r['summary'])]
        fingerprint=hashlib.sha256(normalized(headline).encode()).hexdigest()
        urls={r['url'] for r in selected}
        if fingerprint in fingerprints or urls&selected_urls:continue
        fingerprints.add(fingerprint);selected_urls.update(urls)
        topic['headline']=headline
        topic['discovery'] = selected
        topic['fingerprint'] = fingerprint
        topics.append(topic)
    if not topics:
        raise QualityBlocked('No grounded celebrity topic selected from current reporting.')
    return topics[:settings()['candidate_attempts']]


def publication_date(soup,url):
    for tag in soup.find_all('meta'):
        key=(tag.get('property') or tag.get('name') or '').lower()
        if key in {'article:published_time','datepublished','pubdate','parsely-pub-date'}:
            value=tag.get('content','')
            try:return datetime.fromisoformat(value.replace('Z','+00:00')).date().isoformat()
            except ValueError:pass
    tag=soup.find('time',datetime=True)
    if tag:
        try:return datetime.fromisoformat(tag['datetime'].replace('Z','+00:00')).date().isoformat()
        except ValueError:pass
    match=re.search(r'/((?:19|20)\d{2})/(0?[1-9]|1[0-2])(?:/(0?[1-9]|[12]\d|3[01]))?/',url)
    if match:
        year,month,day=match.groups()
        if day:
            try:return datetime(int(year),int(month),int(day)).date().isoformat()
            except ValueError:return None
        return f'{int(year):04}-{int(month):02}'
    return None


def fetch_article(url: str) -> dict | None:
    if not allowed(url):
        return None
    try:
        with requests.get(url, timeout=(10, 25), stream=True, headers={'User-Agent': 'FameFilesLocalStudio/1.0 (research; no publishing)'}) as response:
            response.raise_for_status()
            if not allowed(response.url) or 'text/html' not in response.headers.get('Content-Type', ''):
                return None
            blocks = []
            size = 0
            for block in response.iter_content(65536):
                size += len(block)
                if size > 3_000_000:
                    return None
                blocks.append(block)
        soup = BeautifulSoup(b''.join(blocks), 'html.parser')
        published=publication_date(soup,url)
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form']):
            tag.decompose()
        article = soup.find('article') or soup.find('main') or soup
        paragraphs = [p.get_text(' ', strip=True) for p in article.find_all('p')]
        text = '\n'.join(p for p in paragraphs if len(p) > 60)
        if len(text.split()) < 180:
            return None
        return {'url': url, 'domain': domain(url), 'title': title, 'text': text[:18000], 'retrieved_at': now().isoformat(),'published_at':published}
    except (requests.RequestException, ValueError):
        return None


def literal_support(quote, text):
    """Recover the source's own punctuation, with exact whole-word support."""
    words=re.findall(r'\w+',quote)
    if not words:return None
    match=re.search(r'(?<!\w)'+r'\W+'.join(re.escape(w) for w in words)+r'(?!\w)',text,re.IGNORECASE)
    if not match:return None
    # Decimal and thousands separators can change numeric meaning.
    numbers=lambda value:re.findall(r'\d+(?:[.,]\d+)*',value)
    if numbers(quote)!=numbers(match.group(0)):return None
    # Keep exact copied quotes intact; otherwise store the actual matched source text.
    found=quote if quote in text else match.group(0)
    return found if 20<=len(found)<=220 else None


def validate_facts(facts: list, sources: list) -> list:
    source_map = {s['id']: s for s in sources}
    result = []
    seen = set()
    for item in facts:
        claim = item.get('claim', '').strip()
        evidence = item.get('evidence', [])
        good = []
        for support in evidence:
            sid = support.get('source_id')
            quote = support.get('quote', '')
            supported=literal_support(quote,source_map[sid]['text']) if sid in source_map and isinstance(quote,str) and 20<=len(quote)<=220 else None
            if supported:
                good.append({'source_id':sid,'quote':supported,**({'published_at':source_map[sid]['published_at']} if source_map[sid].get('published_at') else {})})
        key = normalized(claim)
        tokens=set(key.split())
        near_duplicate=any(len(tokens & set(old.split())) / max(1,len(tokens | set(old.split()))) > .82 for old in seen)
        if claim and good and key not in seen and not near_duplicate:
            seen.add(key)
            result.append({'id': len(result) + 1, 'claim': claim, 'evidence': good})
    return result


def source_excerpts(sources):
    rows=[]
    for source in sources:
        owned=[];seen=set()
        for paragraph in source['text'][:10000].splitlines():
            words=paragraph.split();window=[]
            def emit():
                quote=literal_support(' '.join(window),source['text']) if window else None
                if quote and normalized(quote) not in seen:
                    seen.add(normalized(quote));owned.append({'source_id':source['id'],'published_at':source.get('published_at'),'quote':quote,'context':paragraph[:110]})
            for word in words:
                if window and len(' '.join(window+[word]))>210:
                    emit();window=window[-2:]
                window.append(word)
            emit()
        if len(owned)>20:
            indices=set(range(6))
            indices.update(6+round(i*(len(owned)-7)/13) for i in range(14))
            owned=[owned[i] for i in sorted(indices)]
        rows.extend(owned)
    for i,row in enumerate(rows,1):row['id']=i
    return rows


def extract_batch(ai,topic,sources):
    excerpts=source_excerpts(sources)
    if not excerpts:return {'facts':[],'unknowns':[]}
    lookup={e['id']:e for e in excerpts}
    schema={'type':'object','additionalProperties':False,'required':['facts','unknowns'],'properties':{
        'facts':{'type':'array','maxItems':12,'items':{'type':'object','additionalProperties':False,'required':['claim','excerpt_ids'],'properties':{'claim':{'type':'string','maxLength':300},'excerpt_ids':{'type':'array','minItems':1,'maxItems':2,'items':{'type':'integer','enum':list(lookup)}}}}},
        'unknowns':{'type':'array','maxItems':4,'items':{'type':'string','maxLength':140}}}}
    metadata=[{k:s.get(k) for k in ['id','title','url','published_at']} for s in sources]
    answer=ai.json('Today is '+now().date().isoformat()+'. Build a detailed factual brief about '+topic['headline']+'. Extract 8-12 distinct claims if this evidence supports them, not a three-point summary. Include relevant biography, dated milestones, each separate performer/song pairing, collaborations and specific actions. Keep only facts about the named subject or this event; omit generic medical or industry explainers. Use a concise original paraphrase for each claim. Cite supplied excerpt_ids instead of retyping quotations. Each claim must be fully supported by its selected excerpt(s). Preserve attribution of personal statements and opinions. Treat older announcements as historical reporting; do not present their dates as still upcoming. Do not include song lyrics. Return fewer facts or an empty array when evidence is insufficient; never invent to fill a count. Context fields help identify speakers but are not additional claim licenses. Source metadata: '+json.dumps(metadata)+' Owned excerpts: '+json.dumps(excerpts),tokens=1900,schema=schema)
    raw=[]
    for item in answer.get('facts',[]):
        evidence=[]
        cited=item.get('excerpt_ids',[])
        if not cited or any(type(i) is not int or i not in lookup for i in cited):continue
        for identity in dict.fromkeys(cited):
            row=lookup[identity];evidence.append({'source_id':row['source_id'],'quote':row['quote']})
        raw.append({'claim':item.get('claim',''),'evidence':evidence})
    facts=validate_facts(raw,sources)
    LOG.info('Excerpt extraction: %s proposed claims, %s with original source evidence.',len(answer.get('facts',[])),len(facts))
    return {'facts':facts,'unknowns':answer.get('unknowns',[])}


def curate_facts(ai,topic,sources,facts):
    if not facts:return []
    source_map={s['id']:s for s in sources};kept=[]
    for start in range(0,len(facts),10):
        batch=facts[start:start+10];ids={f['id'] for f in batch};previous={f['id'] for f in kept}
        records=[]
        for fact in batch:
            evidence=[]
            for support in fact['evidence']:
                source=source_map[support['source_id']];position=source['text'].find(support['quote'])
                evidence.append({**support,'published_at':source.get('published_at'),'context':source['text'][max(0,position-140):position] if position>=0 else ''})
            records.append({'id':fact['id'],'claim':fact['claim'],'evidence':evidence})
        schema={'type':'object','additionalProperties':False,'required':['decisions'],'properties':{'decisions':{'type':'array','minItems':len(batch),'maxItems':len(batch),'items':{'type':'object','additionalProperties':False,'required':['fact_id','status','revised_claim','duplicate_of'],'properties':{'fact_id':{'type':'integer','enum':sorted(ids)},'status':{'type':'string','enum':['keep','duplicate','unsupported']},'revised_claim':{'type':'string','maxLength':300},'duplicate_of':{'type':'integer','enum':sorted({0}|ids|previous)}}}}}}
        prompt=('Today is '+now().date().isoformat()+'. Review EACH supplied fact about '+topic['headline']+'. This is not a summary: retain every distinct relevant claim with support. Different performers, songs, dates, actions and career milestones are separate facts even when related. Mark duplicate only when it says the same thing as a kept fact; give that fact ID. Mark unsupported for a claim not established by its quote/context, an invented inference, or unrelated background. Preserve attribution of personal statements and critical opinions. Treat older articles as historical reporting: correct future-tense announcements using their publication dates; do not assume an announced event happened without later evidence. revised_claim should be empty when the original is accurate, otherwise correct only tense or attribution. Do not add new information or song lyrics. Return one decision per supplied fact ID. Previously kept claims: '+json.dumps([{'id':f['id'],'claim':f['claim']} for f in kept])+' Facts and source context: '+json.dumps(records))
        decisions=None
        for attempt in range(2):
            answer=ai.json(prompt,tokens=2000,schema=schema)
            rows=answer.get('decisions',[])
            if len(rows)==len(batch) and all(isinstance(r,dict) and type(r.get('fact_id')) is int and type(r.get('duplicate_of')) is int for r in rows) and {r.get('fact_id') for r in rows}==ids:
                decisions={r['fact_id']:r for r in rows};break
            prompt+='\nThe previous response did not review every required ID exactly once. Required IDs: '+json.dumps(sorted(ids))
        if decisions is None:raise QualityBlocked('Editorial review omitted or invented fact decisions.')
        retained=previous|{i for i,d in decisions.items() if d.get('status')=='keep'}
        for fact in batch:
            decision=decisions[fact['id']];status=decision.get('status')
            if status=='keep':
                if decision.get('duplicate_of')!=0:raise QualityBlocked('Editorial keep decision has a conflicting duplicate reference.')
                kept.append({**fact,'claim':decision.get('revised_claim','').strip() or fact['claim']})
            elif status=='duplicate':
                target=decision.get('duplicate_of')
                if target not in retained or target==fact['id']:raise QualityBlocked('Editorial duplicate does not point to a retained fact.')
            elif status!='unsupported':raise QualityBlocked('Unknown editorial fact decision.')
        LOG.info('Editorial decisions: reviewed %s/%s claims; %s retained.',min(start+10,len(facts)),len(facts),len(kept))
    result=validate_facts(kept,sources)
    LOG.info('Editorial fact curation: %s extracted claims, %s distinct dated claims.',len(facts),len(result))
    return result


def additional_facts(ai,topic,sources,existing):
    """Use source-owned excerpt IDs so a small model need not retype exact quotations."""
    previous=[normalized(f['claim']) for f in existing]
    used_quotes={normalized(e['quote']) for f in existing for e in f.get('evidence',[])}
    pools=[]
    for source in sources:
        excerpts=[]
        for sentence in re.findall(r'[^.!?]+[.!?]?',source['text']):
            sentence=sentence.strip()
            for start in range(0,len(sentence),190):
                quote=sentence[start:start+210].strip()
                if len(quote)<20 or normalized(quote) in used_quotes:continue
                words=set(normalized(quote).split())
                overlap=max((len(words&set(old.split()))/max(1,len(words|set(old.split()))) for old in previous),default=0)
                excerpts.append({'source_id':source['id'],'published_at':source.get('published_at'),'quote':quote,'novelty':1-overlap+.35*mentions(topic['subject'],quote)})
        pools.append(sorted(excerpts,key=lambda x:x['novelty'],reverse=True)[:8])
    excerpts=[]
    for i in range(8):
        for pool in pools:
            if i<len(pool):excerpts.append(pool[i])
            if len(excerpts)>=32:break
        if len(excerpts)>=32:break
    if not excerpts:return []
    for i,row in enumerate(excerpts):row['id']=i+1
    schema={'type':'object','additionalProperties':False,'required':['facts'],'properties':{'facts':{'type':'array','maxItems':10,'items':{'type':'object','additionalProperties':False,'required':['claim','excerpt_ids'],'properties':{'claim':{'type':'string','maxLength':350},'excerpt_ids':{'type':'array','minItems':1,'maxItems':2,'items':{'type':'integer','enum':[r['id'] for r in excerpts]}}}}}}}
    answer=ai.json('Today is '+now().date().isoformat()+'. Respect source dates and avoid presenting old plans as current future events. Find up to ten NEW distinct supported facts relevant to '+topic['subject']+' and this story: '+topic['headline']+'. Existing claims must not be repeated: '+json.dumps([f['claim'] for f in existing])+'. Use the supplied excerpt IDs as evidence. Do not retype quotes. Do not invent details to reach a count. Background is allowed only when it explains this current story; omit unrelated personal-life details. Attribute personal statements and critical opinions. Ignore advertising or unrelated people. Return {"facts":[{"claim":"concise supported fact","excerpt_ids":[integers]}]}. Excerpts: '+json.dumps([{k:r[k] for k in ['id','source_id','published_at','quote']} for r in excerpts]),tokens=1600,schema=schema)
    lookup={r['id']:r for r in excerpts};raw=[]
    for item in answer.get('facts',[]):
        evidence=[{'source_id':lookup[i]['source_id'],'quote':lookup[i]['quote']} for i in item.get('excerpt_ids',[]) if isinstance(i,int) and i in lookup]
        raw.append({'claim':item.get('claim',''),'evidence':evidence})
    checked=validate_facts(raw,sources)
    if not checked:return []
    schema={'type':'object','additionalProperties':False,'required':['supported_ids'],'properties':{'supported_ids':{'type':'array','maxItems':10,'items':{'type':'integer','enum':[f['id'] for f in checked]}}}}
    review=ai.json('Review each proposed fact against its exact evidence and the existing facts. Keep only claims fully supported by their own quoted evidence and genuinely new in meaning. Reject speculation, mismatched details, opinions presented as objective facts, and paraphrases of existing facts. Return {"supported_ids":[fact IDs]}. Existing facts: '+json.dumps([f['claim'] for f in existing])+' Proposed facts and evidence: '+json.dumps(checked),tokens=600,schema=schema)
    return [f for f in checked if f['id'] in review.get('supported_ids',[])]


def collect_sources(topic):
    """Reserve background slots instead of filling the brief with one event."""
    current=[x['url'] for x in topic['discovery']]
    background=[]
    groups=[(current,[topic['subject']+' '+ ' '.join(topic['headline'].split()[:10]),
                      topic['subject']+' latest news',
                      topic['subject']+' site:theguardian.com OR site:people.com OR site:bbc.com']),
            (background,[topic['subject']+' career interview',
                         topic['subject']+' career profile site:theguardian.com OR site:bbc.com',
                         topic['subject']+' career achievements site:grammy.com'])]
    for urls,queries in groups:
        for query in queries:
            try:
                urls.extend(x.get('href','') for x in DDGS(timeout=20).text(query,max_results=16))
            except Exception as error:
                LOG.warning('Follow-up research failed: %s',type(error).__name__)
    sources=[];visited=set()
    def add(url,purpose):
        if url in visited:return
        visited.add(url)
        article=fetch_article(url)
        if article and mentions(topic['subject'],article['title']+' '+article['text'][:5000]) and not any(normalized(article['text'][:350])==normalized(x['text'][:350]) for x in sources):
            article.update(id=len(sources)+1,purpose=purpose)
            sources.append(article)
    current=list(dict.fromkeys(current));position=0
    while position<len(current) and len(sources)<6:
        add(current[position],'current reporting');position+=1
    for url in dict.fromkeys(background):
        if len(sources)>=10:break
        add(url,'reported background')
    # If searches yield no usable background, retain the remaining real reporting.
    for url in current[position:]:
        if len(sources)>=10:break
        add(url,'current reporting')
    LOG.info('Research source mix: %s current reports, %s background sources.',
             sum(s['purpose']=='current reporting' for s in sources),
             sum(s['purpose']=='reported background' for s in sources))
    return sources


def investigate(ai, topic: dict, out) -> dict:
    sources=collect_sources(topic)
    cfg = settings()
    save(out / 'research_sources.json', sources)
    LOG.info('Readable research: %s articles across %s domains for %s',len(sources),len({s['domain'] for s in sources}),topic['subject'])
    if len(sources) < cfg['minimum_articles'] or len({s['domain'] for s in sources}) < cfg['minimum_domains']:
        raise QualityBlocked(f'Research has {len(sources)} readable articles across {len({s["domain"] for s in sources})} domains; six articles and three domains are required.')
    # Bound context to fit the 16GB PC; retain the complete retrieved articles locally.
    raw_facts=[];unknowns=[]
    for start in range(0,len(sources),2):
        try:
            answer=extract_batch(ai,topic,sources[start:start+2])
        except QualityBlocked as error:
            LOG.warning('Source batch %s did not yield valid facts after bounded inference: %s',start//2+1,error)
            continue
        raw_facts.extend(answer.get('facts',[]));unknowns.extend(answer.get('unknowns',[]))
        save(out/'research_progress.json',{'batches_completed':start//2+1,'supported_facts':validate_facts(raw_facts,sources)})
    facts = curate_facts(ai,topic,sources,validate_facts(raw_facts,sources))
    raw_facts=list(facts)
    save(out/'research_progress.json',{'curated':True,'supported_facts':facts})
    targeted_added=False
    for attempt in range(2):
        if len(facts)>=cfg.get('target_facts',cfg['minimum_facts']):break
        try:
            additions=additional_facts(ai,topic,sources,facts)
            raw_facts.extend(additions)
            targeted_added=targeted_added or bool(additions)
        except QualityBlocked as error:
            LOG.warning('Targeted evidence pass unavailable: %s',error)
            break
        facts=validate_facts(raw_facts,sources)
        save(out/'research_progress.json',{'targeted_passes':attempt+1,'supported_facts':facts})
        LOG.info('Targeted evidence pass %s: %s supported facts.',attempt+1,len(facts))
    if targeted_added and len(facts)>=cfg['minimum_facts']:
        # Later additions must not inflate the floor with paraphrases of old facts.
        facts=curate_facts(ai,topic,sources,facts)
        save(out/'research_progress.json',{'final_curated':True,'supported_facts':facts})
    if len(facts) < cfg['minimum_facts']:
        raise QualityBlocked(f'Only {len(facts)} facts had exact supporting evidence; at least {cfg["minimum_facts"]} required.')
    used_sources={e['source_id'] for fact in facts for e in fact['evidence']}
    if len(used_sources)<4 or len({s['domain'] for s in sources if s['id'] in used_sources})<3:
        raise QualityBlocked('Supported facts do not draw on enough independent reporting.')
    research = {'topic': topic, 'facts': facts, 'unknowns': list(dict.fromkeys(unknowns)), 'editorial_curated':True,'source_count': len(sources), 'domains': sorted({s['domain'] for s in sources})}
    save(out / 'research.json', research)
    return research


def write_script(ai, research: dict, out) -> dict:
    facts = research['facts']
    prompt = ('Plan an original 5-7 minute celebrity news explainer from these supported facts. Return {"title":"accurate title","thumbnail_words":["2-4 words","different 2-4 words","different 2-4 words"],"chapters":[{"heading":"short heading","fact_ids":[integers],"graphic_headline":"maximum 7 factual words","graphic_detail":"maximum 14 factual words","graphic_kind":"statement or timeline or comparison","graphic_points":[{"label":"maximum 3 factual words","detail":"maximum 6 factual words"}]}]}. Make exactly six chronological, non-repetitive chapters. Across them use at least 16 DISTINCT supplied fact IDs; allocate 2-5 facts per chapter and avoid repeating the same fact. Include the update, supported context, implications grounded in evidence, and unknowns. Vary graphic forms: timelines only for established chronology, comparison only for supported differences. For a statement leave graphic_points empty; otherwise use 2-3 strictly factual points. Do not invent a dramatic hook or an answer not in the facts.\n' + json.dumps(research))
    ids={f['id'] for f in facts}
    text=lambda maximum:{'type':'string','maxLength':maximum}
    point={'type':'object','additionalProperties':False,'required':['label','detail'],'properties':{'label':text(40),'detail':text(80)}}
    chapter={'type':'object','additionalProperties':False,'required':['heading','fact_ids','graphic_headline','graphic_detail','graphic_kind','graphic_points'],'properties':{'heading':text(70),'fact_ids':{'type':'array','minItems':2,'maxItems':5,'items':{'type':'integer','enum':sorted(ids)}},'graphic_headline':text(100),'graphic_detail':text(160),'graphic_kind':{'type':'string','enum':['statement','timeline','comparison']},'graphic_points':{'type':'array','maxItems':3,'items':point}}}
    schema={'type':'object','additionalProperties':False,'required':['title','thumbnail_words','chapters'],'properties':{'title':{'type':'string','minLength':10,'maxLength':100},'thumbnail_words':{'type':'array','minItems':3,'maxItems':3,'items':text(32)},'chapters':{'type':'array','minItems':6,'maxItems':6,'items':chapter}}}
    for attempt in range(2):
        plan=ai.json(prompt,tokens=3000,schema=schema)
        used={i for c in plan.get('chapters',[]) for i in c.get('fact_ids',[]) if isinstance(i,int) and i in ids}
        if len(plan.get('chapters',[]))==6 and len(used)>=16:break
        if attempt:raise QualityBlocked('The outline does not cover sixteen distinct supported facts in six chapters.')
        prompt+='\nRevise the outline. The previous outline covered only '+str(len(used))+' valid distinct fact IDs. Use at least 16 of '+json.dumps(sorted(ids))+' across exactly six chapters. Previous outline: '+json.dumps(plan)
    if len(plan.get('chapters', [])) != 6:
        raise QualityBlocked('Script outline must contain six substantive chapters.')
    copies=plan.get('thumbnail_words',[])
    if len(copies)!=3 or any(not isinstance(x,str) or not 1<=len(x.split())<=4 or len(x)>32 for x in copies):
        raise QualityBlocked('Thumbnail concepts must contain three short, readable options.')
    if not isinstance(plan.get('title'),str) or not 10<=len(plan['title'])<=100:
        raise QualityBlocked('Title is missing or too long.')
    for chapter in plan['chapters']:
        if not 1<=len(chapter.get('graphic_headline','').split())<=8 or len(chapter.get('graphic_detail','').split())>18:
            raise QualityBlocked('Graphic copy is too dense to read clearly.')
        if chapter.get('graphic_kind','statement') not in {'statement','timeline','comparison'}:
            raise QualityBlocked('Unknown graphic layout.')
        if any(len(p.get('label','').split())>3 or len(p.get('detail','').split())>6 for p in chapter.get('graphic_points',[])):
            raise QualityBlocked('Timeline or comparison labels are too dense.')
    packaging=ai.json('Check the factual implications of this proposed title, thumbnail copy and graphic copy against these facts. Return {"accurate":boolean,"unsupported_implications":[strings]}. Reject invented motives, guilty verdicts, special treatment, fake quotes or dramatic claims absent from evidence. Packaging: '+json.dumps(plan)+'\nFacts: '+json.dumps(facts),tokens=1000)
    if packaging.get('accurate') is not True or packaging.get('unsupported_implications'):
        raise QualityBlocked('Title, thumbnail or graphic copy implies an unsupported claim.')
    used_ids = set()
    previous = []
    for chapter in plan['chapters']:
        selected = [x for x in facts if x['id'] in chapter.get('fact_ids', [])]
        if not selected:
            raise QualityBlocked('Chapter lacks supported facts.')
        body = ai.json('Write this chapter as original spoken narration, 145-170 words. Use only the supplied facts. No greetings, channel slogans, teasers, padding, repeated conclusions, invented quotes or claims. Attribute allegations and preserve unknowns. Return {"narration":"text", "fact_ids":[ids actually used]}.\nChapter: ' + json.dumps(chapter) + '\nSupported facts: ' + json.dumps(selected) + '\nPrevious chapter summaries (do not repeat): ' + json.dumps(previous), tokens=1300)
        text = body.get('narration', '').strip()
        ids = body.get('fact_ids', [])
        if not 115 <= len(text.split()) <= 195 or not ids or not set(ids).issubset({x['id'] for x in selected}):
            raise QualityBlocked('Narration failed its substance or citation checks.')
        review = ai.json('Check every factual claim in this narration against the evidence. Return {"supported":boolean,"unsupported_claims":[strings],"speculative":boolean,"repetitive":boolean}. Fail invented numbers, causal explanations or allegations. Narration: ' + text + '\nEvidence: ' + json.dumps(selected), tokens=900)
        if review.get('supported') is not True or review.get('speculative') is not False or review.get('unsupported_claims') or review.get('repetitive') is not False:
            raise QualityBlocked('Automated editorial review rejected a chapter; no filler fallback.')
        chapter['narration'] = text
        chapter['fact_ids'] = ids
        used_ids.update(ids)
        previous.append({'heading': chapter['heading'], 'facts': ids})
    if sum(len(c['narration'].split()) for c in plan['chapters']) < settings()['minimum_words'] or len(used_ids) < 16:
        raise QualityBlocked('Script is too short or repeats too few facts for long form.')
    plan['research_topic'] = research['topic']
    save(out / 'script.json', plan)
    (out / 'script.txt').write_text('\n\n'.join(c['narration'] for c in plan['chapters']), encoding='utf-8')
    return plan
