import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from local_studio.common import QualityBlocked
from local_studio.publish import CHANNEL, service, upload_body, verified_unlisted


class CloudPublishTests(unittest.TestCase):
    def test_local_production_cannot_publish_even_with_a_switch(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS':'false', 'FAME_PUBLISH_UNLISTED':'true'}):
            with self.assertRaises(QualityBlocked):
                service()

    def test_insert_is_unlisted_and_preserves_credits_without_public_schedule(self):
        Path('output').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir='output') as directory:
            project=Path(directory)
            self.assertTrue(project.resolve().is_relative_to(Path('output').resolve()))
            (project/'research_sources.json').write_text(json.dumps([{'url':'https://apnews.com/example'}]))
            (project/'media_ledger.json').write_text(json.dumps([{'creator':'Creator','title':'Photo','license':'CC BY 4.0','license_url':'https://creativecommons.org/licenses/by/4.0/','source_page':'https://commons.wikimedia.org/wiki/File:Example.jpg'}]))
            body=upload_body({'title':'A factual update','privacy':'public'}, project, 'fixture-marker')
            self.assertEqual(body['status']['privacyStatus'],'unlisted')
            self.assertNotIn('publishAt',body['status'])
            for text in ['Creator','CC BY 4.0','https://apnews.com/example','fixture-marker']:
                self.assertIn(text,body['snippet']['description'])

    def test_other_channel_public_visibility_and_release_schedule_fail(self):
        youtube=Mock()
        for channel,status in [(CHANNEL,{'privacyStatus':'public'}),('wrong-channel',{'privacyStatus':'unlisted'}),(CHANNEL,{'privacyStatus':'unlisted','publishAt':'2030-01-01T00:00:00Z'}),(CHANNEL,{'privacyStatus':'unlisted','uploadStatus':'rejected'})]:
            youtube.videos.return_value.list.return_value.execute.return_value={'items':[{'snippet':{'channelId':channel},'status':status}]}
            with self.assertRaises(QualityBlocked):
                verified_unlisted(youtube,'fixture-id')
        youtube.videos.return_value.insert.assert_not_called()

    def test_success_requires_returned_unlisted_status_on_the_right_channel(self):
        youtube=Mock()
        youtube.videos.return_value.list.return_value.execute.return_value={'items':[{'snippet':{'channelId':CHANNEL},'status':{'privacyStatus':'unlisted','uploadStatus':'processed'}}]}
        self.assertEqual(verified_unlisted(youtube,'fixture-id'),'processed')


if __name__=='__main__':
    unittest.main()
