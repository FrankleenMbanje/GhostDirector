import json
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from local_studio.ai import LocalAI
from local_studio.common import QualityBlocked


class LocalInferenceTests(unittest.TestCase):
    def response(self, chunks):
        response=Mock()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        response.iter_lines.return_value=[json.dumps(c).encode() for c in chunks]
        return response

    def test_streamed_text_uses_small_text_model_with_thinking_off(self):
        response=self.response([{'message':{'content':'{"ok":'}},{'message':{'content':'true}'},'done':True}])
        with patch('local_studio.ai.requests.post',return_value=response) as post:
            self.assertEqual(LocalAI().json('Return an object'),{'ok':True})
        payload=post.call_args.kwargs['json']
        self.assertEqual(payload['model'],'qwen2.5:3b')
        self.assertIs(payload['think'],False)
        self.assertIs(payload['stream'],True)
        self.assertEqual(post.call_args.kwargs['timeout'],(15,180))

    def test_images_use_explicit_instruct_vision_model(self):
        response=self.response([{'message':{'content':'{"ok":true}'},'done':True}])
        with patch('local_studio.ai.requests.post',return_value=response) as post,patch.object(Path,'read_bytes',return_value=b'fixture'):
            LocalAI().json('Inspect fixture',[Path('fixture.png')])
        self.assertEqual(post.call_args.kwargs['json']['model'],'qwen3-vl:4b-instruct')

    def test_incomplete_or_truncated_stream_is_never_accepted(self):
        for final in [{'message':{'content':'{"ok":true}'}},{'message':{'content':'{"ok":true}'},'done':True,'done_reason':'length'}]:
            with patch('local_studio.ai.requests.post',return_value=self.response([final])):
                with self.assertRaises(QualityBlocked):LocalAI().json('Fixture')


if __name__=='__main__':unittest.main()
