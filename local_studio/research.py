from __future__ import annotations

import hashlib
import json
import re
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
            if len(found)>=12:break
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
        for index in range(12):
            for feed in feeds:
                if index<len(feed) and feed[index]['url'] not in seen:
                    item=feed[index]
                    results.append({'id':len(results),**item});seen.add(item['url'])
                if len(results)>=48:break
            if len(results)>=48:break
    if len(results) < 5:
        raise QualityBlocked('Not enough fresh, dated reporting from trusted news sources.')
    ranking=[{k:r[k] for k in ['id','title','date']}|{'summary':r['summary'][:160]} for r in results[:36]]
    answer = ai.json('Today is ' + now().isoformat() + '. Rank 4 DISTINCT fresh celebrity stories with substantial news value and corroboration. Prefer stories covered by multiple independent publishers, not gossip, unsupported allegations or trivial updates. Avoid these already produced topics: ' + json.dumps(excluded[-30:]) + '. Return {"topics":[{"subject":"celebrity full name","headline":"specific factual story","news_ids":[integers]}]}. Every news_id must be in this evidence. Do not change dates or invent people.\n' + json.dumps(ranking), tokens=1800)
    topics = []
    for topic in answer.get('topics', []):
        ids = topic.get('news_ids', [])
        selected = [r for r in results if r['id'] in ids]
        if not selected or not topic.get('subject') or not topic.get('headline'):
            continue
        topic['discovery'] = selected
        topic['fingerprint'] = hashlib.sha256(normalized(topic['headline']).encode()).hexdigest()
        topics.append(topic)
    if not topics:
        raise QualityBlocked('No grounded celebrity topic selected from current reporting.')
    return topics[:settings()['candidate_attempts']]


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
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form']):
            tag.decompose()
        article = soup.find('article') or soup.find('main') or soup
        paragraphs = [p.get_text(' ', strip=True) for p in article.find_all('p')]
        text = '\n'.join(p for p in paragraphs if len(p) > 60)
        if len(text.split()) < 180:
            return None
        return {'url': url, 'domain': domain(url), 'title': title, 'text': text[:18000], 'retrieved_at': now().isoformat()}
    except (requests.RequestException, ValueError):
        return None


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
            if sid in source_map and 20 <= len(quote) <= 220 and normalized(quote) in normalized(source_map[sid]['text']):
                good.append({'source_id': sid, 'quote': quote})
        key = normalized(claim)
        tokens=set(key.split())
        near_duplicate=any(len(tokens & set(old.split())) / max(1,len(tokens | set(old.split()))) > .82 for old in seen)
        if claim and good and key not in seen and not near_duplicate:
            seen.add(key)
            result.append({'id': len(result) + 1, 'claim': claim, 'evidence': good})
    return result


def investigate(ai, topic: dict, out) -> dict:
    urls = [x['url'] for x in topic['discovery']]
    for query in [topic['headline'], topic['subject'] + ' ' + topic['headline'] + ' facts timeline official']:
        try:
            urls += [x.get('href', '') for x in DDGS(timeout=20).text(query, max_results=16)]
        except Exception as error:
            LOG.warning('Follow-up research failed: %s', type(error).__name__)
    sources = []
    for url in dict.fromkeys(urls):
        article = fetch_article(url)
        if article and not any(normalized(article['text'][:350]) == normalized(x['text'][:350]) for x in sources):
            article['id'] = len(sources) + 1
            sources.append(article)
        if len(sources) >= 10:
            break
    cfg = settings()
    if len(sources) < cfg['minimum_articles'] or len({s['domain'] for s in sources}) < cfg['minimum_domains']:
        raise QualityBlocked('Research lacks six readable articles across three independent domains.')
    save(out / 'research_sources.json', sources)
    # Bound context to fit the 16GB PC; retain the complete retrieved articles locally.
    raw_facts=[];unknowns=[]
    for start in range(0,len(sources),2):
        evidence=[{'id':s['id'],'title':s['title'],'text':s['text'][:4800]} for s in sources[start:start+2]]
        answer=ai.json('Research '+topic['headline']+'. Extract 8-12 DISTINCT supported facts from this source batch, including reported background and chronology. Do not speculate. Return {"facts":[{"claim":"concise fact","evidence":[{"source_id":integer,"quote":"exact 20-220 character substring"}]}],"unknowns":[strings]}. Quotes must be exact; do not invent facts to reach a count.\n'+json.dumps(evidence),tokens=2400)
        raw_facts.extend(answer.get('facts',[]));unknowns.extend(answer.get('unknowns',[]))
    facts = validate_facts(raw_facts, sources)
    if len(facts) < cfg['minimum_facts']:
        raise QualityBlocked(f'Only {len(facts)} facts had exact supporting evidence; at least {cfg["minimum_facts"]} required.')
    used_sources={e['source_id'] for fact in facts for e in fact['evidence']}
    if len(used_sources)<4 or len({s['domain'] for s in sources if s['id'] in used_sources})<3:
        raise QualityBlocked('Supported facts do not draw on enough independent reporting.')
    research = {'topic': topic, 'facts': facts, 'unknowns': list(dict.fromkeys(unknowns)), 'source_count': len(sources), 'domains': sorted({s['domain'] for s in sources})}
    save(out / 'research.json', research)
    return research


def write_script(ai, research: dict, out) -> dict:
    facts = research['facts']
    plan = ai.json('Plan an original 5-7 minute celebrity news explainer from these supported facts. Return {"title":"accurate title","thumbnail_words":["2-4 words","different 2-4 words","different 2-4 words"],"chapters":[{"heading":"short heading","fact_ids":[integers],"graphic_headline":"maximum 7 factual words","graphic_detail":"maximum 14 factual words","graphic_kind":"statement or timeline or comparison","graphic_points":[{"label":"maximum 3 factual words","detail":"maximum 6 factual words"}]}]}. Make exactly six chronological, non-repetitive chapters, including the update, supported context, implications grounded in evidence, and unknowns. Vary graphic forms: timelines only for established chronology, comparison only for supported differences. For a statement leave graphic_points empty; otherwise use 2-3 strictly factual points. Do not invent a dramatic hook or an answer not in the facts.\n' + json.dumps(research), tokens=3000)
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
        body = ai.json('Write this chapter as original spoken narration, 140-165 words. Use only the supplied facts. No greetings, channel slogans, teasers, padding, repeated conclusions, invented quotes or claims. Attribute allegations and preserve unknowns. Return {"narration":"text", "fact_ids":[ids actually used]}.\nChapter: ' + json.dumps(chapter) + '\nSupported facts: ' + json.dumps(selected) + '\nPrevious chapter summaries (do not repeat): ' + json.dumps(previous), tokens=1300)
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
