import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from local_studio.geography import prepare_maps

class GeographicLocatorTests(unittest.TestCase):
    def test_jsonv2_category_and_legacy_class_both_create_grounded_locator(self):
        for classification in [{'category':'boundary'},{'class':'boundary'}]:
            ai=Mock();ai.json.return_value={'places':[{'name':'Arizona','fact_id':1}]}
            research={'facts':[{'id':1,'claim':'The Arizona concert was canceled.'}]}
            script={'chapters':[{'fact_ids':[1],'graphic_kind':'statement'}]}
            row={**classification,'lat':'34.395342','lon':'-111.763275','osm_type':'relation','osm_id':162018,'address':{'state':'Arizona','country_code':'us'}}
            response=Mock();response.json.return_value=[row]
            with patch('local_studio.geography.OUTPUT',Path('nonexistent-map-fixture')),patch('local_studio.geography.save'),patch('local_studio.geography.time.sleep'),patch('local_studio.geography.requests.get',return_value=response):
                result=prepare_maps(ai,research,script,Path('fixture'))
            self.assertEqual(len(result),1)
            self.assertEqual(script['chapters'][0]['graphic_kind'],'map')
            self.assertEqual(result[0]['fact_id'],1)
            self.assertAlmostEqual(result[0]['longitude'],-111.763275)

    def test_place_absent_from_supported_fact_is_never_geocoded(self):
        ai=Mock();ai.json.return_value={'places':[{'name':'Arizona','fact_id':1}]}
        with patch('local_studio.geography.OUTPUT',Path('nonexistent-map-fixture')),patch('local_studio.geography.save'),patch('local_studio.geography.requests.get') as request:
            result=prepare_maps(ai,{'facts':[{'id':1,'claim':'The singer confirmed the cancellation.'}]},{'chapters':[]},Path('fixture'))
        self.assertEqual(result,[])
        request.assert_not_called()

if __name__=='__main__':unittest.main()
