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

    def test_registry_stream_503_retries_but_unknown_model_fails(self):
        from unittest.mock import MagicMock,patch
        from local_studio.ai import LocalAI
        from local_studio.common import QualityBlocked
        def response(payload):
            item=MagicMock();item.__enter__.return_value=item
            item.iter_lines.return_value=[__import__('json').dumps(payload).encode()]
            return item
        with patch('local_studio.ai.space_check'),patch('local_studio.ai.ensure_server'),patch('local_studio.ai.time.sleep'),patch('local_studio.ai.requests.post',side_effect=[response({'error':'pull model manifest: 503: upstream connect error'}),response({'status':'success'})]) as post:
            LocalAI()._pull('qwen2.5:7b-instruct')
            self.assertEqual(post.call_count,2)
        with patch('local_studio.ai.space_check'),patch('local_studio.ai.requests.post',return_value=response({'error':'model manifest does not exist'})) as post:
            with self.assertRaisesRegex(QualityBlocked,'does not exist'):
                LocalAI()._pull('qwen2.5:7b-instruct')
            self.assertEqual(post.call_count,1)

    def test_streamed_text_uses_small_text_model_with_thinking_off(self):
        response=self.response([{'message':{'content':'{"ok":'}},{'message':{'content':'true}'},'done':True}])
        with patch('local_studio.ai.requests.post',return_value=response) as post:
            self.assertEqual(LocalAI().json('Return an object'),{'ok':True})
        payload=post.call_args.kwargs['json']
        self.assertEqual(payload['model'],'qwen2.5:7b-instruct')
        self.assertIs(payload['think'],False)
        self.assertIs(payload['stream'],True)
        self.assertEqual(post.call_args.kwargs['timeout'],(15,900))

    def test_images_use_explicit_instruct_vision_model(self):
        response=self.response([{'message':{'content':'{"ok":true}'},'done':True}])
        with patch('local_studio.ai.requests.post',return_value=response) as post,patch.object(Path,'read_bytes',return_value=b'fixture'):
            LocalAI().json('Inspect fixture',[Path('fixture.png')])
        self.assertEqual(post.call_args.kwargs['json']['model'],'qwen3-vl:4b-instruct')

    def test_repetition_abort_retries_once_with_bounded_schema(self):
        failed=self.response([{'error':'prediction aborted, token repeat limit reached'}])
        good=self.response([{'message':{'content':'{"ok":true}'},'done':True}])
        schema={'type':'object','properties':{'ok':{'type':'boolean'}}}
        with patch('local_studio.ai.requests.post',side_effect=[failed,good]) as post:
            self.assertEqual(LocalAI().json('Fixture',schema=schema),{'ok':True})
        self.assertEqual(post.call_count,2)
        self.assertEqual(post.call_args.kwargs['json']['format'],schema)
        self.assertGreater(post.call_args.kwargs['json']['options']['repeat_penalty'],1)
        with patch('local_studio.ai.requests.post',return_value=failed) as post:
            with self.assertRaises(QualityBlocked):LocalAI().json('Fixture')
        self.assertEqual(post.call_count,2)

    def test_incomplete_or_truncated_stream_is_never_accepted(self):
        for final in [{'message':{'content':'{"ok":true}'}},{'message':{'content':'{"ok":true}'},'done':True,'done_reason':'length'}]:
            with patch('local_studio.ai.requests.post',return_value=self.response([final])):
                with self.assertRaises(QualityBlocked):LocalAI().json('Fixture')


if __name__=='__main__':unittest.main()
