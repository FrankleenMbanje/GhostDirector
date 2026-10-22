import asyncio
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from local_studio.common import QualityBlocked,now
from local_studio.editorial import load_package,prepare_research,validate_script
from local_studio.research import normalized
from local_studio.voice import collect_edge_stream


def fixture():
    domains=['variety.com','bbc.com','people.com','deadline.com','rollingstone.com','billboard.com']
    text='Jane Doe confirmed a new exhibition will open in the city.'
    sources=[{'id':i,'url':'https://'+d+'/example','domain':d,'title':'Jane Doe update','text':text,'published_at':now().date().isoformat()} for i,d in enumerate(domains,1)]
    facts=[{'id':i,'claim':'Supported distinct detail number '+str(i),'evidence':[{'source_id':i%6+1,'quote':text}]} for i in range(1,25)]
    script={'title':'Jane Doe exhibition update','thumbnail_words':['NEW EXHIBITION','THE NEXT CHAPTER','JANE DOE'],'chapters':[{'heading':'Chapter '+str(i),'fact_ids':list(range(i*4+1,i*4+5)),'narration':'A supported sentence. '*45,'graphic_kind':'statement','graphic_headline':'EXHIBITION UPDATE','graphic_detail':'A documented current event','graphic_points':[]} for i in range(6)]}
    topic={'subject':'Jane Doe','headline':'Jane Doe exhibition update','fingerprint':hashlib.sha256(normalized('Jane Doe exhibition update').encode()).hexdigest()}
    return {'date':now().date().isoformat(),'prepared_by':'Codex','review_mode':'source-reviewed-editorial','topic':topic,'facts':facts,'sources':sources,'script':script,'sha256':'fixture-sha'}


class EditorialTests(unittest.TestCase):
    def test_loader_rejects_stale_or_outside_packages(self):
        bundle=fixture()
        with tempfile.TemporaryDirectory() as directory,patch('local_studio.editorial.ROOT',Path(directory)):
            target=Path(directory)/'editorial/today.json';target.parent.mkdir();target.write_text(json.dumps(bundle),encoding='utf-8')
            self.assertEqual(load_package('editorial/today.json')['script']['title'],bundle['script']['title'])
            with self.assertRaisesRegex(QualityBlocked,'inside editorial'):
                load_package('../other.json')
            bundle['map_requests']=[{'name':'Invented place','fact_id':1}];target.write_text(json.dumps(bundle),encoding='utf-8')
            with self.assertRaisesRegex(QualityBlocked,'map requests'):
                load_package('editorial/today.json')
            bundle.pop('map_requests')
            bundle['date']='2000-01-01';target.write_text(json.dumps(bundle),encoding='utf-8')
            with self.assertRaisesRegex(QualityBlocked,'stale'):
                load_package('editorial/today.json')

    def test_sources_are_refetched_and_every_excerpt_must_survive(self):
        bundle=fixture();by_url={s['url']:s for s in bundle['sources']}
        with patch('local_studio.editorial.fetch_article',side_effect=lambda u:copy.deepcopy(by_url[u])),patch('local_studio.editorial.save'):
            research=prepare_research(bundle,Path('fixture'))
            self.assertEqual(len(research['facts']),24)
            self.assertEqual(research['editorial_package_sha256'],'fixture-sha')
            bundle['facts'][0]['evidence'].append({'source_id':1,'quote':'This invented sentence is not in any source.'})
            with self.assertRaisesRegex(QualityBlocked,'excerpt no longer matches'):
                prepare_research(bundle,Path('fixture'))

    def test_script_rejects_unsupported_ids_and_short_narration(self):
        bundle=fixture();validate_script(bundle['script'],bundle['facts'])
        bundle['script']['chapters'][0]['fact_ids']=[999,1]
        with self.assertRaisesRegex(QualityBlocked,'citations'):
            validate_script(bundle['script'],bundle['facts'])
        bundle=fixture();bundle['script']['chapters'][0]['narration']='Too short.'
        with self.assertRaisesRegex(QualityBlocked,'length'):
            validate_script(bundle['script'],bundle['facts'])


class ArchivePaginationTests(unittest.TestCase):
    def test_crops_of_one_photo_do_not_count_as_different_originals(self):
        from local_studio.media import commons_original
        original='File:Dolly Parton - RCA Press Photo (1970).jpg'
        self.assertEqual(commons_original(original),commons_original('File:Dolly Parton - RCA Press Photo (1970) (cropped) 2.jpg'))
        self.assertEqual(commons_original(original),commons_original('File:Dolly Parton - RCA Press Photo (1970) (croppedsquare).jpg'))
        self.assertNotEqual(commons_original(original),commons_original('File:Dolly Parton 1983.jpg'))

    def test_later_photo_batches_retain_license_and_full_category_checks(self):
        from unittest.mock import Mock
        from local_studio.media import candidates
        def page(identity,marked=False):
            return {'pageid':identity,'title':'File:Portrait '+str(identity)+'.jpg','imageinfo':[{'url':'https://upload.wikimedia.org/example/'+str(identity)+'.jpg','size':1000,'width':2000,'height':1400,'descriptionurl':'https://commons.wikimedia.org/wiki/File:Portrait_'+str(identity)+'.jpg','extmetadata':{'LicenseShortName':{'value':'CC BY 2.0'},'Artist':{'value':'Photographer'},'Categories':{'value':'Images with watermarks' if marked else 'Portrait photographs'}}}]}
        first=Mock();first.json.return_value={'query':{'pages':{'1':page(1),'2':page(2,True)}},'continue':{'gsroffset':35}}
        second=Mock();second.json.return_value={'query':{'pages':{'3':page(3)}}}
        with patch('local_studio.media.requests.get',side_effect=[first,second]) as request:
            pool=candidates('Jane Doe','bitmap')
        self.assertEqual([a['id'] for a in pool],['1','3'])
        self.assertEqual(request.call_args_list[1].kwargs['params']['gsroffset'],35)


class CandidateSupplyTests(unittest.TestCase):
    def test_six_crops_of_one_photo_do_not_warrant_full_research(self):
        from local_studio.media import candidate_supply
        rows=[{'origin_id':'same-photo'} for _ in range(6)]
        with patch('local_studio.media.candidates',side_effect=[rows,[]]):
            result=candidate_supply({'subject':'Jane Doe','headline':'Jane Doe update'})
        self.assertFalse(result['eligible_to_research'])
        self.assertEqual(result['photo_original_candidates'],1)

    def test_candidate_count_is_not_a_completed_visual_screen(self):
        from local_studio.media import candidate_supply
        rows=[{'origin_id':str(i)} for i in range(6)]
        with patch('local_studio.media.candidates',return_value=rows) as archive:
            result=candidate_supply({'subject':'Jane Doe','headline':'Jane Doe update'})
        self.assertTrue(result['eligible_to_research'])
        self.assertFalse(result['visual_screening_complete'])
        self.assertIsNone(result['video_original_candidates'])
        self.assertEqual(archive.call_count,1)


class NarrationDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_stream_is_not_accepted_after_deadline(self):
        class Stalled:
            async def stream(self):
                yield {'type':'audio','data':b'partial'}
                await asyncio.sleep(1)
        with self.assertRaises(TimeoutError):
            await collect_edge_stream(Stalled(),timeout_seconds=.01)

    async def test_complete_stream_preserves_audio_and_word_timing(self):
        class Complete:
            async def stream(self):
                yield {'type':'audio','data':b'complete'}
                yield {'type':'WordBoundary','offset':0,'duration':10000000,'text':'A &amp; B'}
        parts,words=await collect_edge_stream(Complete())
        self.assertEqual(parts,[b'complete'])
        self.assertEqual(words,[{'start':0,'end':1,'word':'A & B'}])
