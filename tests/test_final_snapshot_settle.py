"""终态快照落库时序回归：假时钟、合成产物、全 mock 后端。"""
import base64
import copy
import json
import unittest
from unittest.mock import Mock, patch

from services.config import config
from services.image_quota import ImageQuotaExceeded
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest, ImageGenerationError
from tests.test_container_terminal_delivery import container_document
from tests.test_terminal_image_integration import CID, call, history, png


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if not isinstance(seconds, (int, float)) or not 0 < seconds <= 0.5:
            raise AssertionError('snapshot wait must be positive and bounded by 0.5 seconds')
        self.sleeps.append(seconds)
        self.now += seconds


def native_document():
    return {'conversation_id': CID, 'async_status': 4, 'messages': history()}


def pending_document(document):
    result = copy.deepcopy(document)
    result['async_status'] = 3
    result['messages'][-1]['status'] = 'in_progress'
    result['messages'][-1]['end_turn'] = False
    result['messages'][-1]['metadata']['is_complete'] = False
    return result


class FinalSnapshotSettleTests(unittest.TestCase):
    def setup_backend(self, live, snapshots, *, consume_get_timeout=False):
        self.clock = FakeClock()
        self.get_started = []
        self.consume_get_timeout = consume_get_timeout
        self.snapshots = list(snapshots)
        self.backend = Mock()
        self.backend.access_token = 'synthetic-settle-account'
        self.backend._image_submitted_message_id = live['messages'][0]['id']
        cid = live['conversation_id']
        self.backend.stream_conversation.return_value = iter([
            *(json.dumps({'conversation_id': cid, 'message': item}) for item in live['messages']), '[DONE]'])
        self.backend._resolve_image_urls.return_value = ['https://example.test/final.png']
        self.backend.download_image_bytes.return_value = [png()]
        self.backend.download_final_sandbox_images.return_value = [png()]

        def readback(conversation_id, **kwargs):
            self.get_started.append((self.clock.now, conversation_id, dict(kwargs)))
            if len(self.get_started) > 25:
                raise AssertionError('unbounded snapshot loop')
            if self.consume_get_timeout:
                # 让每次 GET 耗尽所分配的预算，验证后续 GET 不能重新获得完整超时。
                self.clock.now += kwargs.get('timeout_secs', 3.0)
            value = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)

        self.backend._get_conversation.side_effect = readback
        return self.backend

    def run_pipeline(self):
        with patch.object(conversation.time, 'monotonic', self.clock.monotonic), \
             patch.object(conversation.time, 'sleep', self.clock.sleep), \
             patch.object(conversation, 'account_service', Mock()), \
             patch.object(conversation, '_remove_image_conversation_later'), \
             patch.object(conversation, 'logger', Mock()), \
             patch.object(config, 'data', {**config.data, 'image_require_reference_postprocess': False}):
            return list(conversation.stream_image_outputs(self.backend, ConversationRequest(
                model='gpt-image-2', prompt='synthetic settle test', images=['synthetic reference'],
                response_format='b64_json', image_transport='conversation')))

    def assert_only_original_gets(self, cid):
        for started, requested_id, kwargs in self.get_started:
            self.assertEqual(cid, requested_id)
            self.assertIn('timeout_secs', kwargs)
            self.assertGreater(kwargs['timeout_secs'], 0)
            self.assertLessEqual(kwargs['timeout_secs'], 3.0)
            self.assertLessEqual(kwargs['timeout_secs'], 108.0 - started + 1e-9)
        self.backend.stream_conversation.assert_called_once()
        self.backend._post_image_conversation.assert_not_called()
        self.backend._start_image_generation.assert_not_called()
        self.backend.resolve_conversation_image_urls.assert_not_called()

    def assert_no_image_downloads(self):
        self.backend.download_final_sandbox_images.assert_not_called()
        self.backend.download_image_bytes.assert_not_called()
        self.backend._resolve_image_urls.assert_not_called()

    def assert_delivered(self, outputs, source):
        results = [item for item in outputs if item.kind == 'result']
        self.assertEqual(1, len(results))
        self.assertEqual(png(), base64.b64decode(results[0].data[0]['b64_json']))
        self.assertEqual(source, results[0].execution['result_source'])

    def test_async_three_then_four_recovers_native_png_from_same_conversation(self):
        complete = native_document()
        self.setup_backend(complete, [pending_document(complete), complete])
        self.assert_delivered(self.run_pipeline(), 'image_tool_terminal')
        self.assertEqual(2, len(self.get_started))
        self.assertEqual([0.5], self.clock.sleeps)
        self.assert_only_original_gets(CID)
        self.backend.download_image_bytes.assert_called_once()
        self.backend.download_final_sandbox_images.assert_not_called()

    def test_async_three_then_four_recovers_container_png_with_result_message_id(self):
        complete = container_document();complete['async_status'] = 4
        self.setup_backend(complete, [pending_document(complete), complete])
        self.assert_delivered(self.run_pipeline(), 'container_terminal')
        self.assertEqual(2, len(self.get_started))
        self.assert_only_original_gets(complete['conversation_id'])
        self.backend.download_final_sandbox_images.assert_called_once_with(
            complete['conversation_id'], 'container-result', ['/mnt/data/final.png'])
        self.backend.download_image_bytes.assert_not_called()

    def test_missing_final_message_retries_until_complete_snapshot(self):
        complete = native_document();before = copy.deepcopy(complete)
        before['messages'].pop()
        self.setup_backend(complete, [before, complete])
        self.assert_delivered(self.run_pipeline(), 'image_tool_terminal')
        self.assertEqual(2, len(self.get_started))
        self.assert_only_original_gets(CID)

    def test_unfinished_tool_retries_even_when_final_is_complete(self):
        complete = container_document();complete['async_status'] = 4
        before = copy.deepcopy(complete);before['messages'][3]['status'] = 'in_progress'
        self.setup_backend(complete, [before, complete])
        self.assert_delivered(self.run_pipeline(), 'container_terminal')
        self.assertEqual(2, len(self.get_started))
        self.assert_only_original_gets(complete['conversation_id'])

    def test_missing_observed_tool_call_retries_instead_of_using_old_image(self):
        complete = container_document();complete['async_status'] = 4
        before = copy.deepcopy(complete);before['messages'].pop(2)
        self.setup_backend(complete, [before, complete])
        self.assert_delivered(self.run_pipeline(), 'container_terminal')
        self.assertEqual(2, len(self.get_started))
        self.assert_only_original_gets(complete['conversation_id'])

    def test_exhausted_eight_second_budget_rejects_without_download_or_post(self):
        complete = native_document()
        self.setup_backend(complete, [pending_document(complete)], consume_get_timeout=True)
        with self.assertRaises(ImageGenerationError):
            self.run_pipeline()
        self.assertGreater(len(self.get_started), 1)
        self.assertAlmostEqual(108.0, self.clock.now)
        self.assert_only_original_gets(CID)
        self.assertLess(self.get_started[-1][2]['timeout_secs'], 3.0)
        self.assert_no_image_downloads()

    def test_transient_curl_timeout_retries_original_conversation_then_delivers(self):
        from curl_cffi.requests.exceptions import Timeout

        complete = native_document()
        self.setup_backend(complete, [Timeout('synthetic timeout', code=28), complete],
                           consume_get_timeout=True)
        self.assert_delivered(self.run_pipeline(), 'image_tool_terminal')
        self.assertEqual(2, len(self.get_started))
        self.assertEqual([0.5], self.clock.sleeps)
        self.assertLessEqual(self.clock.now, 108.0)
        self.assert_only_original_gets(CID)
        self.backend.download_image_bytes.assert_called_once()
        self.backend.download_final_sandbox_images.assert_not_called()

    def test_persistent_curl_timeout_exhausts_shared_budget_without_download(self):
        from curl_cffi.requests.exceptions import Timeout

        complete = native_document()
        self.setup_backend(complete, [Timeout('synthetic timeout', code=28)], consume_get_timeout=True)
        with self.assertRaises(ImageGenerationError):
            self.run_pipeline()
        self.assertGreater(len(self.get_started), 1)
        self.assertAlmostEqual(108.0, self.clock.now)
        self.assert_only_original_gets(CID)
        self.assertLess(self.get_started[-1][2]['timeout_secs'], 3.0)
        self.assert_no_image_downloads()

    def test_wrong_conversation_or_user_is_rejected_without_retry(self):
        for mutation in ('conversation', 'user'):
            with self.subTest(mutation=mutation):
                complete = native_document();wrong = copy.deepcopy(complete)
                if mutation == 'conversation':
                    wrong['conversation_id'] = 'different-conversation'
                else:
                    wrong['messages'][0]['id'] = 'different-user'
                self.setup_backend(complete, [wrong, complete])
                with self.assertRaises(ImageGenerationError):
                    self.run_pipeline()
                self.assertEqual(1, len(self.get_started))
                self.assertEqual([], self.clock.sleeps)
                self.assert_no_image_downloads()

    def test_ambiguous_completed_artifact_is_not_retried(self):
        complete = container_document('/mnt/data/a.png\n/mnt/data/b.png');complete['async_status'] = 4
        self.setup_backend(complete, [complete])
        with self.assertRaises(ImageGenerationError):
            self.run_pipeline()
        self.assertEqual(1, len(self.get_started))
        self.assertEqual([], self.clock.sleeps)
        self.assert_no_image_downloads()

    def test_unknown_async_status_is_rejected_without_retry(self):
        complete = native_document();unknown = copy.deepcopy(complete);unknown['async_status'] = 17
        self.setup_backend(complete, [unknown, complete])
        with self.assertRaises(ImageGenerationError):
            self.run_pipeline()
        self.assertEqual(1, len(self.get_started))
        self.assertEqual([], self.clock.sleeps)
        self.assert_no_image_downloads()

    def test_quota_exception_is_propagated_unchanged(self):
        complete = native_document()
        quota = ImageQuotaExceeded({'reason': 'synthetic quota', 'restore_at': 1793200000, 'conversation_id': CID})
        self.setup_backend(complete, [quota])
        with self.assertRaises(ImageQuotaExceeded) as raised:
            self.run_pipeline()
        self.assertIs(quota, raised.exception)
        self.assertEqual(1, len(self.get_started))
        self.assertEqual([], self.clock.sleeps)
        self.assert_no_image_downloads()

    def test_new_tool_work_never_delivers_previous_image(self):
        complete = native_document();new_work = copy.deepcopy(complete)
        later = call('later-unfinished-call', 'container.exec');later['status'] = 'in_progress'
        new_work['messages'].insert(-1, later)
        self.setup_backend(complete, [pending_document(complete), new_work])
        with self.assertRaises(ImageGenerationError):
            self.run_pipeline()
        self.assertGreaterEqual(len(self.get_started), 2)
        self.assertLessEqual(self.clock.now, 108.0)
        self.assert_only_original_gets(CID)
        self.assert_no_image_downloads()


if __name__ == '__main__':
    unittest.main(verbosity=2)
