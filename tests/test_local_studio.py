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

    def test_wikimedia_downloads_use_canonical_cacheable_urls(self):
        from local_studio.media import canonical_media_url
        original='https://upload.wikimedia.org/wikipedia/commons/a/ab/Photo.jpg'
        self.assertEqual(canonical_media_url(original+'?utm_source=commons&utm_campaign=imageinfo'),original)
        self.assertEqual(canonical_media_url(original+'?download=1&utm_source=commons'),original+'?download=1')
        other='https://example.com/file.jpg?utm_source=required'
        self.assertEqual(canonical_media_url(other),other)

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
