"""恢复任务的持久化与归属隔离；不保存原始输入或账号凭据。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.image_task_service import ImageTaskService


CONTEXT = {'submitted_message_id': 'submitted', 'input_asset_ids': ['file-input'],
           'input_image_hashes': ['a' * 64], 'observed_evidence': {
               'version': 1, 'conversation_id': 'original-conversation', 'tool_ids': ['image-result'],
               'tool_calls': {'image-call': 'image.generate'},
               'asset_ids_by_message': {'image-result': ['file-output']}, 'failed_message_ids': []}}


class DeliveryCheckpointTaskTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'tasks.json'
        self.service = ImageTaskService(self.path)
        self.service._log_call = Mock()

    def callback(self, owner='owner'):
        factory = getattr(self.service, 'make_recovery_checkpoint', None)
        self.assertTrue(callable(factory), '同步请求需要原owner绑定的恢复检查点')
        return factory({'id': owner}, task_id='call-123', mode='edit', model='gpt-image-2', base_url='https://example.test')

    def event(self, **kw):
        return {'phase': 'pending', 'index': 1, 'total': 1, 'conversation_id': 'original-conversation',
                'resume_account_ref': 'b' * 64, 'delivery_context': copy.deepcopy(CONTEXT), **kw}

    def test_checkpoint_persists_identity_and_observations_without_public_exposure(self):
        task_id = self.callback()(self.event())
        self.assertTrue(task_id)
        restored = ImageTaskService(self.path)
        public = restored.list_tasks({'id': 'owner'}, [task_id])['items'][0]
        self.assertEqual('original-conversation', public['conversation_id'])
        self.assertNotIn('resume_account_ref', public)
        self.assertNotIn('delivery_context', public)
        saved = json.loads(self.path.read_text())['tasks'][0]
        self.assertEqual(CONTEXT, saved['delivery_context'])
        self.assertEqual('b' * 64, saved['resume_account_ref'])
        self.assertEqual('queued', public['status'])
        self.assertTrue(public['recovering'])
        self.assertEqual([], restored.list_tasks({'id': 'other'}, [task_id])['items'])

    def test_each_index_keeps_its_own_account_conversation_and_artifact(self):
        cb = self.callback()
        first = cb(self.event(total=2))
        second_context = copy.deepcopy(CONTEXT)
        second_context['submitted_message_id'] = 'submitted-two'
        second_context['observed_evidence']['conversation_id'] = 'second-conversation'
        second = cb(self.event(index=2, total=2, conversation_id='second-conversation',
                               resume_account_ref='c' * 64, delivery_context=second_context))
        self.assertNotEqual(first, second)
        records = json.loads(self.path.read_text())['tasks']
        self.assertEqual(2, len(records))
        self.assertEqual({'original-conversation', 'second-conversation'}, {t['conversation_id'] for t in records})

    def test_checkpoint_cannot_replace_existing_account_or_conversation(self):
        cb = self.callback()
        task_id = cb(self.event())
        with self.assertRaises(ValueError):
            cb(self.event(resume_account_ref='c' * 64))
        saved = json.loads(self.path.read_text())['tasks'][0]
        self.assertEqual(task_id, saved['id'])
        self.assertEqual('b' * 64, saved['resume_account_ref'])

    def test_unbounded_or_secret_evidence_is_rejected_before_persisting(self):
        for field, value in [('token', 'synthetic-secret'), ('tool_ids', ['x'] * 257)]:
            with self.subTest(field=field):
                event = self.event()
                event['delivery_context']['observed_evidence'][field] = value
                with self.assertRaises(ValueError):
                    self.callback()(event)
        self.assertFalse(self.path.exists())

    def test_recovery_success_stores_url_and_does_not_log_or_settle_another_generation(self):
        cb = self.callback()
        task_id = cb(self.event())
        data = [{'url': 'https://example.test/final.png', 'b64_json': 'synthetic-large-image'}]
        cb(self.event(phase='success', data=data))
        public = self.service.list_tasks({'id': 'owner'}, [task_id])['items'][0]
        self.assertEqual('success', public['status'])
        self.assertEqual([{'url': 'https://example.test/final.png'}], public['data'])
        self.assertNotIn('synthetic-large-image', self.path.read_text())
        self.service._log_call.assert_not_called()

    def test_reloaded_checkpoint_only_resumes_original_account_without_generation(self):
        task_id = self.callback()(self.event())
        restored = ImageTaskService(self.path)
        restored._log_call = Mock()
        restored.generation_handler = Mock(side_effect=AssertionError('cannot regenerate'))
        accounts, backend = Mock(), Mock()
        accounts.acquire_image_resume_token.return_value = 'synthetic-token'
        accounts.refresh_access_token.return_value = 'synthetic-token'
        from services.protocol.conversation import ImageOutput
        with patch('services.image_task_service.account_service', accounts), \
             patch('services.openai_backend_api.OpenAIBackendAPI', return_value=backend), \
             patch('services.protocol.conversation.stream_image_outputs', return_value=iter([
                 ImageOutput(kind='result', model='gpt-image-2', index=1, total=1,
                             data=[{'url': 'https://example.test/final.png'}])])) as stream:
            restored._run_resume_poll('owner:' + task_id, 'original-conversation', 120,
                                      {'id': 'owner'}, 'edit', 'gpt-image-2')
        accounts.acquire_image_resume_token.assert_called_once_with('b' * 64, conversation_id='original-conversation')
        accounts.release_image_slot.assert_called_once_with('synthetic-token', resume=True)
        accounts.mark_image_result.assert_not_called()
        self.assertEqual(CONTEXT, stream.call_args.args[1].delivery_context)
        restored.generation_handler.assert_not_called()


if __name__ == '__main__':
    unittest.main()
