import unittest
from datetime import timedelta
from unittest.mock import patch

from local_studio.ai import LocalAI
from local_studio.common import QualityBlocked, now, settings
from local_studio.edit import plan_shots, validate_mix
from local_studio.media import accepted_license, usable_subject
from local_studio.research import allowed, fresh, validate_facts, feed_news, discover
from email.utils import format_datetime
from unittest.mock import Mock
from local_studio.voice import phrases, wrap, align_source_words, merge_orphan_cues


class ProductionPolicyTests(unittest.TestCase):
    def test_cpu_inference_allows_prompt_evaluation_but_keeps_total_budget(self):
        import json
        response=Mock()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        response.iter_lines.return_value=[json.dumps({'message':{'content':'{"ok":true}'},'done':True}).encode()]
        with patch('local_studio.ai.requests.post',return_value=response) as post, patch('local_studio.ai.time.monotonic',side_effect=[0,0,301,301,302]):
            self.assertEqual(LocalAI().json('A research prompt',tokens=20),{'ok':True})
        self.assertEqual(post.call_args.kwargs['timeout'],(15,900))
        response.iter_lines.return_value=[b'{"message":{"content":"{}"},"done":true}']
        with patch('local_studio.ai.requests.post',return_value=response), patch('local_studio.ai.time.monotonic',side_effect=[0,1201,1201,1201,1201]):
            with self.assertRaisesRegex(QualityBlocked,'twenty-minute'):
                LocalAI().json('A stalled request',tokens=20)

    def test_corroborated_roster_supplies_multiple_topics_without_single_model_choice(self):
        rows=[('Charli XCX cancels Arizona concert','https://variety.com/charli'),('Charli XCX loses voice','https://billboard.com/charli'),('Charli XCX show canceled','https://deadline.com/charli'),('Taylor Swift announces album','https://variety.com/taylor'),('Taylor Swift album update','https://billboard.com/taylor'),('Julia Stiles recalls tribute','https://variety.com/julia'),('Julia Stiles tribute interview','https://deadline.com/julia')]
        feed=[{'title':title,'url':url,'date':now().isoformat(),'summary':''} for title,url in rows]
        ai=Mock();ai.json.side_effect=AssertionError('Corroborated roster should not require model ranking')
        with patch('local_studio.research.DDGS') as search,patch('local_studio.research.feed_news',return_value=feed):
            search.return_value.news.return_value=[]
            topics=discover(ai,[])
        self.assertEqual(topics[0]['subject'],'Charli XCX')
        self.assertEqual(len(topics),3)
        self.assertEqual(len(topics[0]['discovery']),3)

    def test_ranked_topics_do_not_duplicate_a_story_or_accept_unrelated_source_ids(self):
        rows=[('Jane Doe announces a new film','https://variety.com/jane'),('John Roe talks about his career','https://billboard.com/john')]
        rows += [('Other Celebrity '+str(i),'https://deadline.com/other-'+str(i)) for i in range(4)]
        feed=[{'title':title,'url':url,'date':now().isoformat(),'summary':''} for title,url in rows]
        ai=Mock();ai.json.return_value={'topics':[{'subject':'Jane Doe','headline':'Invented dramatic claim','news_ids':[0,1]},{'subject':'Jane Doe','news_ids':[0]},{'subject':'John Roe','news_ids':[1]}]}
        with patch('local_studio.research.DDGS') as search,patch('local_studio.research.feed_news',return_value=feed):
            search.return_value.news.return_value=[]
            topics=discover(ai,[])
        self.assertEqual(len(topics),2)
        self.assertEqual(topics[0]['headline'],rows[0][0])
        self.assertEqual([s['url'] for s in topics[0]['discovery']],[rows[0][1]])

    def test_publisher_feeds_reject_stale_and_untrusted_links(self):
        date=format_datetime(now())
        old=format_datetime(now()-timedelta(days=6))
        xml=f'<rss><channel><item><title>Fresh celebrity update</title><link>https://variety.com/example</link><pubDate>{date}</pubDate></item><item><title>Old</title><link>https://variety.com/old</link><pubDate>{old}</pubDate></item><item><title>Untrusted</title><link>https://evil.test/a</link><pubDate>{date}</pubDate></item></channel></rss>'
        response=Mock();response.content=xml.encode()
        with patch('local_studio.research.requests.get',return_value=response):
            self.assertEqual([x['title'] for x in feed_news('https://variety.com/feed/')],['Fresh celebrity update'])

    def test_discovery_recovers_when_search_returns_no_dated_news(self):
        feed=[{'title':'Example celebrity story '+str(i),'url':'https://variety.com/story-'+str(i),'date':now().isoformat(),'summary':'An attributed update.'} for i in range(6)]
        ai=Mock();ai.json.return_value={'topics':[{'subject':'Example celebrity','headline':'A sourced update','news_ids':[0]}]}
        with patch('local_studio.research.DDGS') as search,patch('local_studio.research.feed_news',return_value=feed):
            search.return_value.news.return_value=[]
            topics=discover(ai,[])
        self.assertEqual(topics[0]['discovery'][0]['url'],feed[0]['url'])

    def test_stock_is_rejected_even_with_enough_genuine_sources(self):
        shots=[{'kind':'video','role':'subject','origin_id':str(i),'source_start':0,'duration':75} for i in range(4)]
        shots.append({'kind':'video','role':'context','origin_id':'stock','source_start':0,'duration':5})
        with self.assertRaisesRegex(QualityBlocked,'stock'):
            validate_mix(shots,305)

    def test_caption_alignment_preserves_hyphens_punctuation_and_real_timing(self):
        words=[{'start':0,'end':.4,'word':'Low'},{'start':.4,'end':.9,'word':'security'},{'start':1,'end':1.5,'word':'prison'}]
        aligned=align_source_words(words,'Low-security prison.')
        self.assertEqual([w['word'] for w in aligned],['Low-security','prison.'])
        self.assertEqual(aligned[0]['end'],.9)
        self.assertEqual(align_source_words(words,'Different unsupported words.'),words)
        cues=merge_orphan_cues([{'start':0,'end':3,'text':'Visuals that belong to the'},{'start':3.01,'end':3.6,'text':'subject.'}])
        self.assertEqual(len(cues),1)
        self.assertEqual(cues[0]['text'],'Visuals that belong to the subject.')

    def test_watermark_uncertainty_and_nonfinite_identity_never_pass(self):
        good={'subject_present':True,'identity_confidence':.95,'publisher_or_stock_brand_visible':False,'faces_framed_well':True,'clear_usable_picture':True,'uncertain':False}
        self.assertTrue(usable_subject(good))
        for change in [{'publisher_or_stock_brand_visible':True},{'publisher_or_stock_brand_visible':'false'},{'uncertain':True},{'identity_confidence':float('nan')},{'identity_confidence':.4}]:
            self.assertFalse(usable_subject({**good,**change}))

    def test_context_cannot_replace_genuine_celebrity_video_sources(self):
        shots=[{'kind':'video','role':'context','origin_id':str(i),'source_start':0,'duration':80} for i in range(3)]
        with self.assertRaises(QualityBlocked):validate_mix(shots,300)

    def test_paid_or_remote_ai_is_rejected_before_network(self):
        cfg=settings();cfg['ollama_url']='https://api.example.com'
        with patch('local_studio.ai.settings',return_value=cfg),patch('requests.post') as request:
            with self.assertRaises(QualityBlocked):LocalAI()
            request.assert_not_called()
        cfg=settings();cfg['model']='qwen3-vl:cloud'
        with patch('local_studio.ai.settings',return_value=cfg):
            with self.assertRaises(QualityBlocked):LocalAI()

    def test_unknown_old_and_future_news_cannot_be_called_fresh(self):
        self.assertTrue(fresh(now().isoformat()))
        self.assertFalse(fresh((now()-timedelta(days=4)).isoformat()))
        self.assertFalse(fresh((now()+timedelta(days=2)).isoformat()))
        self.assertFalse(fresh('yesterday'))

    def test_research_cannot_fetch_local_or_untrusted_urls(self):
        self.assertTrue(allowed('https://apnews.com/article/story'))
        for url in ['http://apnews.com/article/story','https://apnews.com.evil.test/a','http://127.0.0.1:11434/api/tags','file:///C:/Users/frank/.env']:
            self.assertFalse(allowed(url))

    def test_fabricated_quotes_and_source_ids_fail_grounding(self):
        sources=[{'id':1,'text':'The agency confirmed the transfer on Friday afternoon.'}]
        facts=[{'claim':'A move was confirmed.','evidence':[{'source_id':1,'quote':'The agency confirmed the transfer'}]}, {'claim':'It was a reward.','evidence':[{'source_id':1,'quote':'The agency gave him special treatment'}]}, {'claim':'A third source confirmed it.','evidence':[{'source_id':9,'quote':'The agency confirmed the transfer'}]}]
        result=validate_facts(facts,sources)
        self.assertEqual([x['claim'] for x in result],['A move was confirmed.'])

    def test_reference_style_artist_credit_keeps_original_creator_link(self):
        from local_studio.media import creator_credit
        self.assertEqual(creator_credit('<a href="https://www.flickr.com/photos/example/">[1]</a>'),'https://www.flickr.com/photos/example/')
        self.assertEqual(creator_credit('<a href="//commons.wikimedia.org/wiki/User:Photographer">Photographer</a>'),'Photographer')
        self.assertEqual(creator_credit('<a href="javascript:bad">[1]</a>'),'[1]')

    def test_wikimedia_downloads_use_canonical_cacheable_urls(self):
        from local_studio.media import canonical_media_url
        original='https://upload.wikimedia.org/wikipedia/commons/a/ab/Photo.jpg'
        self.assertEqual(canonical_media_url(original+'?utm_source=commons&utm_campaign=imageinfo'),original)
        self.assertEqual(canonical_media_url(original+'?download=1&utm_source=commons'),original+'?download=1')
        other='https://example.com/file.jpg?utm_source=required'
        self.assertEqual(canonical_media_url(other),other)

    def test_quote_support_recovers_source_punctuation_without_changing_words(self):
        from local_studio.research import literal_support
        source='Jane Doe announced a sold-out concert, in Nashville, on Wednesday.'
        quote='Jane Doe announced a sold out concert in Nashville on Wednesday.'
        self.assertEqual(literal_support(quote,source),source.rstrip('.'))
        self.assertIsNone(literal_support('He announced the next concert on Wednesday.','She announced the next concert on Wednesday.'))
        self.assertIsNone(literal_support('The jury found the defendant guilty.','The jury found the defendant not guilty.'))
        self.assertIsNone(literal_support('The price was 1,500 dollars.','The price was 1.500 dollars.'))
        self.assertIsNone(literal_support('The ticket price was 15 dollars.','The ticket price was 1.5 dollars.'))

    def test_fact_floor_is_checked_after_curation_of_late_additions(self):
        from pathlib import Path
        from local_studio.research import investigate
        sources=[{'id':i,'domain':d,'url':'https://'+d+'/story','title':'Jane Doe','text':'Jane Doe confirmed the exact supported detail in this report.'} for i,d in enumerate(['variety.com','bbc.com','people.com','deadline.com','billboard.com','rollingstone.com'],1)]
        facts=[{'id':i,'claim':'Distinct supported detail number '+str(i),'evidence':[{'source_id':(i%6)+1,'quote':sources[0]['text']}]} for i in range(1,19)]
        ai=Mock();ai.json.return_value={'facts':facts,'unknowns':[]}
        topic={'subject':'Jane Doe','headline':'Jane Doe current update'}
        with patch('local_studio.research.collect_sources',return_value=sources),patch('local_studio.research.save'),patch('local_studio.research.curate_facts',side_effect=[facts[:15],facts[:15]]) as curate,patch('local_studio.research.additional_facts',return_value=[facts[15]]):
            with self.assertRaisesRegex(QualityBlocked,'Only 15 facts'):
                investigate(ai,topic,Path('fixture'))
        self.assertEqual(curate.call_count,2)

    def test_research_reserves_room_for_relevant_background(self):
        from local_studio.research import collect_sources
        current=[f'https://variety.com/current-{i}' for i in range(10)]
        background=[f'https://bbc.com/background-{i}' for i in range(4)]
        topic={'subject':'Jane Doe','headline':'Jane Doe current event','discovery':[{'url':u} for u in current]}
        def article(url):
            return {'url':url,'title':'Jane Doe','text':url+' Jane Doe '+('Reported detail. '*30)}
        with patch('local_studio.research.DDGS') as search,patch('local_studio.research.fetch_article',side_effect=article):
            search.return_value.text.side_effect=lambda query,**kwargs:[{'href':u} for u in background] if 'career' in query else []
            sources=collect_sources(topic)
        self.assertEqual([s['url'] for s in sources],current[:6]+background)
        self.assertEqual([s['id'] for s in sources],list(range(1,11)))
        self.assertEqual(sum(s['purpose']=='reported background' for s in sources),4)

    def test_failed_fact_batch_does_not_discard_other_grounded_sources_or_lower_floor(self):
        from pathlib import Path
        from local_studio.research import investigate
        domains=['variety.com','deadline.com','billboard.com','bbc.com','people.com']
        articles=[];batches=[]
        for i in range(10):
            facts=[{'claim':f'Jane Doe confirmed entry token{i}_{n}.','evidence':[{'source_id':i+1,'quote':f'Jane Doe confirmed entry token{i}_{n}.'}]} for n in range(4)]
            articles.append({'url':f'https://{domains[i%5]}/story-{i}','domain':domains[i%5],'title':'Jane Doe source '+str(i),'text':' '.join(f['claim'] for f in facts)})
            if i%2==0:batches.append({'facts':[],'unknowns':[]})
            batches[-1]['facts'].extend(facts)
        topic={'subject':'Jane Doe','headline':'Jane Doe sourced update','discovery':[{'url':a['url']} for a in articles]}
        with patch('local_studio.research.save'),patch('local_studio.research.curate_facts',side_effect=lambda ai,topic,sources,facts:facts),patch('local_studio.research.DDGS') as search,patch('local_studio.research.fetch_article',side_effect=lambda url:dict(next(a for a in articles if a['url']==url))):
            search.return_value.text.return_value=[]
            ai=Mock();ai.json.side_effect=[batches[0],QualityBlocked('repeat limit'),*batches[2:]]
            result=investigate(ai,topic,Path('fixture'))
            self.assertEqual(len(result['facts']),32)
            ai.json.side_effect=[batches[0],*[QualityBlocked('repeat limit') for _ in range(4)],{'facts':[]},{'facts':[]}]
            with self.assertRaisesRegex(QualityBlocked,'at least 16'):investigate(ai,topic,Path('fixture'))

    def test_targeted_facts_can_only_cite_literal_supplied_excerpt_ids(self):
        from local_studio.research import additional_facts
        sources=[{'id':1,'text':'Jane Doe opened a new exhibition on Monday. The exhibition includes forty original instruments.'}]
        ai=Mock();ai.json.side_effect=[{'facts':[{'claim':'The exhibition includes forty original instruments.','excerpt_ids':[2]},{'claim':'Invented missing citation','excerpt_ids':[9999]}]},{'supported_ids':[1]}]
        result=additional_facts(ai,{'subject':'Jane Doe','headline':'Jane Doe opens exhibition'},sources,[])
        self.assertEqual(len(result),1)
        self.assertIn(result[0]['evidence'][0]['quote'],sources[0]['text'])
        self.assertEqual(result[0]['evidence'][0]['source_id'],1)

    def test_outline_repairs_missing_fact_coverage_before_narration_and_keeps_editorial_gate(self):
        from pathlib import Path
        from copy import deepcopy
        from local_studio.research import write_script
        plan={'title':'A sourced celebrity update','thumbnail_words':['LATEST UPDATE','THE FACTS','WHAT NEXT'],'chapters':[{'heading':'Chapter '+str(i),'fact_ids':[i*3+n for n in [1,2,3]],'graphic_headline':'Reported update','graphic_detail':'Based on cited reporting','graphic_kind':'statement','graphic_points':[]} for i in range(6)]}
        bad=deepcopy(plan)
        for chapter in bad['chapters']:chapter['fact_ids']=[1]
        research={'facts':[{'id':i,'claim':'Supported entry '+str(i)} for i in range(1,19)]}
        ai=Mock();ai.json.side_effect=[bad,plan,{'accurate':False,'unsupported_implications':['unsupported packaging']}]
        with self.assertRaisesRegex(QualityBlocked,'unsupported claim'):write_script(ai,research,Path('fixture'))
        self.assertEqual(ai.json.call_count,3)
        self.assertIn('previous outline covered only 1',ai.json.call_args_list[1].args[0])
        ai.json.side_effect=[bad,bad]
        with self.assertRaisesRegex(QualityBlocked,'sixteen distinct'):write_script(ai,research,Path('fixture'))

    def test_publication_dates_are_distinct_from_retrieval_dates(self):
        from bs4 import BeautifulSoup
        from local_studio.research import publication_date
        page=BeautifulSoup('<meta property="article:published_time" content="2026-05-20T09:00:00Z">','html.parser')
        self.assertEqual(publication_date(page,'https://variety.com/example'),'2026-05-20')
        self.assertEqual(publication_date(BeautifulSoup('','html.parser'),'https://variety.com/2026/05/story/'),'2026-05')
        self.assertIsNone(publication_date(BeautifulSoup('','html.parser'),'https://variety.com/undated'))

    def test_editorial_curation_keeps_original_quotes_and_rejects_invented_fact_ids(self):
        from local_studio.research import curate_facts
        source={'id':1,'url':'https://variety.com/2026/05/story','title':'Jane Doe announcement','published_at':'2026-05-20','text':'Jane Doe plans an exhibition for June 2026.'}
        facts=[{'id':1,'claim':'Jane Doe will open an exhibition.','evidence':[{'source_id':1,'quote':source['text']}]}]
        ai=Mock();ai.json.return_value={'facts':[{'claim':'In May 2026, Jane Doe announced a planned exhibition.','support_fact_ids':[1]},{'claim':'An invented update','support_fact_ids':[999]}]}
        result=curate_facts(ai,{'headline':'Jane Doe update'},[source],facts)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['evidence'],facts[0]['evidence'])
        self.assertIn('historical reporting',ai.json.call_args.args[0])

    def test_same_run_research_cache_requires_complete_original_evidence(self):
        import json
        from pathlib import Path
        from copy import deepcopy
        from local_studio.__main__ import cached_research
        facts=[{'id':i+1,'claim':f'Jane Doe confirmed entry token{i}.','evidence':[{'source_id':i%6+1,'quote':f'Jane Doe confirmed entry token{i}.'}]} for i in range(16)]
        sources=[{'id':i+1,'url':'https://'+['variety.com','deadline.com','bbc.com'][i%3]+'/story-'+str(i),'text':'Source '+str(i)+' '+' '.join(f['claim'] for f in facts)} for i in range(6)]
        research={'editorial_curated':True,'topic':{'fingerprint':'verified'},'facts':facts}
        cfg=settings()
        with patch.object(Path,'read_text',side_effect=[json.dumps(research),json.dumps(sources)]),patch('local_studio.__main__.settings',return_value=cfg):
            self.assertEqual(cached_research({'fingerprint':'verified'},Path('fixture'))['facts'],facts)
        redacted=[{k:v for k,v in source.items() if k!='text'} for source in sources]
        with patch.object(Path,'read_text',side_effect=[json.dumps(research),json.dumps(redacted)]),patch('local_studio.__main__.settings',return_value=cfg):
            with self.assertRaisesRegex(QualityBlocked,'complete original'):cached_research({'fingerprint':'verified'},Path('fixture'))
        broken=deepcopy(research)
        broken['facts'][0]['evidence'][0]['quote']='Invented evidence not in a source'
        with patch.object(Path,'read_text',side_effect=[json.dumps(broken),json.dumps(sources)]),patch('local_studio.__main__.settings',return_value=cfg):
            with self.assertRaisesRegex(QualityBlocked,'exact evidence'):cached_research({'fingerprint':'verified'},Path('fixture'))

    def test_permission_is_not_inferred_from_short_duration(self):
        self.assertTrue(accepted_license('CC BY 4.0'))
        self.assertTrue(accepted_license('Public domain'))
        for name in ['Standard YouTube License','All rights reserved','Fair use','7 second rule','Unknown','Editorial use only']:
            self.assertFalse(accepted_license(name))

    def test_renaming_one_interview_does_not_create_diversity(self):
        shots=[{'kind':'video','origin_id':'same-interview','source_start':i*100,'duration':100} for i in range(3)]
        with self.assertRaises(QualityBlocked):validate_mix(shots,300)

    def test_repeated_source_sections_and_dominant_original_are_rejected(self):
        shots=[{'kind':'video','origin_id':str(i),'source_start':0,'duration':80} for i in range(3)]
        shots += [{'kind':'video','origin_id':'0','source_start':5,'duration':30}]
        with self.assertRaises(QualityBlocked):validate_mix(shots,300)

    def test_planner_is_gapless_and_does_not_loop_or_overuse_sources(self):
        assets=[{'id':str(i),'origin_id':'original-'+str(i),'kind':'video','duration':240,'path':'fixture.mp4'} for i in range(4)]
        assets += [{'id':'p'+str(i),'origin_id':'photo-'+str(i),'kind':'photo','path':'fixture.jpg'} for i in range(6)]
        timing={'duration':330,'chapters':[{'index':i,'start':i*55,'end':(i+1)*55} for i in range(6)]}
        shots=plan_shots(assets,{},timing)
        self.assertEqual(shots[0]['start_frame'],0)
        self.assertEqual(sum(x['frames'] for x in shots),9900)
        for first,second in zip(shots,shots[1:]):
            self.assertEqual(first['start_frame']+first['frames'],second['start_frame'])
        validate_mix(shots,330)
        self.assertGreaterEqual(len({s['origin_id'] for s in shots if s['kind']=='video'}),3)

    def test_insufficient_footage_does_not_become_a_five_minute_loop(self):
        assets=[{'id':str(i),'origin_id':str(i),'kind':'video','duration':12,'path':'fixture.mp4'} for i in range(3)]
        timing={'duration':330,'chapters':[{'index':i,'start':i*55,'end':(i+1)*55} for i in range(6)]}
        with self.assertRaises(QualityBlocked):plan_shots(assets,{},timing)

    def test_approved_photo_led_edit_has_diverse_gapless_visuals(self):
        assets=[{'id':str(i),'origin_id':'photo-'+str(i),'kind':'photo','path':'fixture.jpg'} for i in range(8)]
        timing={'duration':330,'chapters':[{'index':i,'start':i*55,'end':(i+1)*55} for i in range(6)]}
        shots=plan_shots(assets,{},timing)
        self.assertEqual(sum(s['frames'] for s in shots),9900)
        self.assertTrue(all(s['mode']=='photo_led' for s in shots))
        visible=set()
        for shot in shots:
            current={shot['origin_id']}
            if shot.get('second_photo_origin'):current.add(shot['second_photo_origin'])
            self.assertFalse(visible & current)
            visible=current if shot['kind']!='graphic' else set()
        validate_mix(shots,330)
        cfg=settings();cfg['allow_photo_led_edit']=False
        with patch('local_studio.edit.settings',return_value=cfg):
            with self.assertRaises(QualityBlocked):plan_shots(assets,{},timing)

    def test_offline_caption_chunks_remain_readable(self):
        text='A long explanation should remain readable, with natural pauses and complete facts instead of dozens of tiny words flashing across the screen.'
        chunks=phrases(text)
        self.assertEqual(' '.join(chunks),text)
        self.assertTrue(all(len(x)<=68 and len(x.split())<=12 for x in chunks))
        self.assertTrue(all(len(wrap(x).splitlines())<=2 for x in chunks))


if __name__=='__main__':unittest.main()
