"""Source-grounded area locators; no invented travel or live-event imagery."""
import json
import time

import requests

from .common import LOG,OUTPUT,QualityBlocked,save
from .research import mentions


def prepare_maps(ai,research,script,out,preferred_places=None):
    try:
        answer={'places':preferred_places} if preferred_places is not None else ai.json('Select up to two useful geographic locations explicitly written in these supported claims. Prefer the city/state of the current update. Return {"places":[{"name":"exact written place name","fact_id":integer}]}. No guessed addresses, inferred travel or invented venues.\n'+json.dumps(research['facts']),tokens=500)
        facts={f['id']:f['claim'] for f in research['facts']}
        cache=OUTPUT/'geocodes.json'
        stored=json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else {}
        selected=[]
        for item in answer.get('places',[])[:2]:
            name=item.get('name','').strip();fact=facts.get(item.get('fact_id'),'')
            if not name or not mentions(name,fact):continue
            key=name.casefold()
            if key not in stored:
                # Public Nominatim requests are deliberately sequential, over one second apart.
                time.sleep(1.1)
                response=requests.get('https://nominatim.openstreetmap.org/search',params={'q':name,'format':'jsonv2','addressdetails':1,'limit':3},headers={'User-Agent':'FameFilesLocalStudio/1.0 (https://github.com/FrankleenMbanje/GhostDirector)'},timeout=25)
                response.raise_for_status();stored[key]=response.json();save(cache,stored)
            rows=[r for r in stored[key] if r.get('category',r.get('class')) in {'place','boundary'}]
            if not rows:continue
            # Reject ambiguous names when none of the returned administrative context is supported.
            if len(rows)>1:
                precise=[r for r in rows if any(mentions(v,fact) for k,v in r.get('address',{}).items() if k in {'state','country'} and v)]
                if len(precise)!=1:continue
                rows=precise
            row=rows[0]
            place={'name':name,'longitude':float(row['lon']),'latitude':float(row['lat']),'country':row.get('address',{}).get('country_code','').upper(),'fact_id':item['fact_id'],'source':'https://www.openstreetmap.org/'+row['osm_type']+'/'+str(row['osm_id'])}
            for chapter in script['chapters']:
                if item['fact_id'] in chapter['fact_ids'] and chapter.get('graphic_kind')!='map':
                    chapter.update(graphic_kind='map',graphic_headline=name,graphic_detail='Area locator',graphic_points=[],map_place=place)
                    selected.append(place);break
        save(out/'map_sources.json',selected)
        save(out/'script.json',script)
        return selected
    except (requests.RequestException,QualityBlocked,ValueError,KeyError,TypeError) as error:
        LOG.warning('Map lookup unavailable; retain factual timeline cards: %s',type(error).__name__)
        return []
