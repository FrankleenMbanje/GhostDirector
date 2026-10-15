"""Offline tests for license selection, attribution and selected-file integrity."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from pipeline.licensed_media import commons_record, credit, fingerprint, rights_issues

ROOT = Path(__file__).resolve().parent

def page(license_name='CC BY 2.0', license_url='https://creativecommons.org/licenses/by/2.0/'):
    return {'title':'File:Taylor Swift at an event.jpg','imageinfo':[{
        'url':'https://upload.wikimedia.org/wikipedia/commons/a/a1/photo.jpg',
        'descriptionurl':'https://commons.wikimedia.org/wiki/File:Taylor_Swift_at_an_event.jpg',
        'mime':'image/jpeg','width':1600,'height':2000,
        'extmetadata':{k:{'value':v} for k,v in {
            'LicenseShortName':license_name,'LicenseUrl':license_url,
            'Artist':'<a>Photographer</a>','License':'cc-by-2.0','Copyrighted':'True',
            'ImageDescription':'Taylor Swift at a public event'}.items()}}]}

class LicensedMediaTests(unittest.TestCase):
    def test_accepts_specific_by_license_and_creator(self):
        r=commons_record(page(),'Taylor Swift')
        self.assertEqual(r['creator'],'Photographer')
        self.assertIn('CC BY 2.0',credit(r))
        self.assertIn('Changes:',credit(r))

    def test_unknown_license_is_not_reusable(self):
        self.assertIsNone(commons_record(page('Unknown',''),'Taylor Swift'))

    def test_noncommercial_and_no_derivatives_are_rejected(self):
        for license_name in ('CC BY-NC 4.0','CC BY-ND 4.0','CC BY-SA 4.0'):
            self.assertIsNone(commons_record(page(license_name),'Taylor Swift'))

    def test_license_name_without_matching_license_url_is_rejected(self):
        self.assertIsNone(commons_record(page(license_url='https://example.com/'),'Taylor Swift'))

    def test_mismatched_subject_is_rejected(self):
        self.assertIsNone(commons_record(page(),'Blake Lively'))

    def test_description_mention_does_not_identify_the_subject(self):
        p=page();p['title']='File:Another performer singing.webm'
        self.assertIsNone(commons_record(p,'Taylor Swift'))

    def test_license_url_without_final_slash_is_normalized(self):
        r=commons_record(page(license_url='https://creativecommons.org/licenses/by/2.0'),'Taylor Swift')
        self.assertEqual(r['license_url'],'https://creativecommons.org/licenses/by/2.0/')

    def test_mismatched_license_version_is_rejected(self):
        self.assertIsNone(commons_record(page(license_url='https://creativecommons.org/licenses/by/4.0/'),'Taylor Swift'))

    def test_media_on_untrusted_host_is_rejected(self):
        p=page(); p['imageinfo'][0]['url']='https://example.com/photo.jpg'
        self.assertIsNone(commons_record(p,'Taylor Swift'))

    def test_public_domain_requires_explicit_file_evidence(self):
        p=page('Public domain','')
        self.assertIsNone(commons_record(p,'Taylor Swift'))
        p['imageinfo'][0]['extmetadata']['Copyrighted']['value']='False'
        self.assertIsNotNone(commons_record(p,'Taylor Swift'))

    def test_changed_bytes_missing_credits_and_paired_asset_are_blocked(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'output') as td:
            root=Path(td); path=root/'photo.jpg'; path.write_bytes(b'verified image')
            r=commons_record(page(),'Taylor Swift')
            r.update(local_path=str(path),sha256=fingerprint(path))
            (root/'media_rights.json').write_text(json.dumps({'complete':True,'assets':[r]}))
            scene=SimpleNamespace(scene_number=1,photo_path=str(path),video_path=None,broll_video_path=None)
            script=SimpleNamespace(scenes=[scene])
            self.assertFalse(rights_issues(script,root,credit(r)))
            self.assertTrue(any('attribution' in x for x in rights_issues(script,root,'')))
            path.write_bytes(b'different image')
            self.assertTrue(any('changed' in x for x in rights_issues(script,root,credit(r))))
            scene.broll_video_path=str(root/'unverified.mp4')
            self.assertTrue(any('lacks license' in x for x in rights_issues(script,root,credit(r))))

    def test_missing_manifest_blocks(self):
        self.assertTrue(rights_issues(SimpleNamespace(scenes=[]),ROOT/'output/nonexistent-rights-test',''))

    def test_packaging_preserves_license_credits(self):
        from pipeline.trending_short import _override_metadata
        script=SimpleNamespace(title='Taylor Swift music ownership',hook='Taylor Swift owns her original recordings.',description='',tags=[])
        credits=credit(commons_record(page(),'Taylor Swift'))
        meta={'media_policy':'licensed_subjects','media_credits':credits,'tags':[]}
        packaged=_override_metadata(meta,script,{'title':'Taylor Swift music ownership','source':'Official statement'})
        self.assertIn(credits,packaged['description'])
        self.assertEqual(packaged['media_policy'],'licensed_subjects')

if __name__=='__main__': unittest.main()
