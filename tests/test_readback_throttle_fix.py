"""读取限流、已提交请求及冷却门禁的闭环回归，全部使用隔离账号。"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from services import image_quota
from services.account_service import config
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import conversation
from utils.helper import UpstreamHTTPError
from test_sub2api_full_import import make_service
from tests import test_delivery_timeout_recovery as delivery_fixture
from tests import test_image_final_contract as images
from tests import test_image_generation_stream as stream_fixture

TOKEN = 'readback-fixture'
CID = 'fixture-readback-conversation'


def account_service():
    service = make_service([{'access_token': TOKEN, 'type': 'edu', 'quota': 10, 'status': '正常'},
                         {'access_token': 'healthy-fixture', 'type': 'edu', 'quota': 10, 'status': '正常'}])[0]
    service.get_account(TOKEN)
    return service


def quota_evidence(source='assistant_final'):
    now = time.time()
    return image_quota.ImageQuotaEvidence('合成限流', source, CID, '', 'fixture-event', now, now, now + 60)


class ReadbackClassifierTests(unittest.TestCase):
    def classify(self, error):
        method = getattr(image_quota, 'image_readback_throttle_evidence_from_http', None)
        self.assertTrue(callable(method), '会话读回429必须保留独立的临时限流证据')
        return method(error)

    def test_readback_429_preserves_retry_after_and_conversation(self):
        error = UpstreamHTTPError('/backend-api/conversations/' + CID, 429,
                                  {'detail': 'private marker'}, retry_after=45)
        evidence = self.classify(error)
        self.assertEqual(CID, evidence.conversation_id)
        self.assertEqual('upstream_http_conversation_throttle', evidence.source)
        self.assertAlmostEqual(error.retry_at, evidence.restore_at, places=2)
        self.assertNotIn('private marker', evidence.reason)

    def test_missing_retry_after_has_bounded_local_backoff(self):
        evidence = self.classify(UpstreamHTTPError('/backend-api/conversation/' + CID, 429, {}))
        self.assertIsNone(evidence.restore_at)
        self.assertGreater(evidence.next_check_at, time.time())
        self.assertLessEqual(evidence.next_check_at, time.time() + 31)

    def test_other_statuses_and_endpoints_do_not_freeze_image_accounts(self):
        for path, status in [('/backend-api/conversation/' + CID, 403),
                             ('/backend-api/conversation/' + CID, 404),
                             ('/backend-api/conversation/' + CID, 503),
                             ('/backend-api/me', 429), ('image_download', 429),
                             ('/backend-api/conversation/../me', 429)]:
            with self.subTest(path=path, status=status):
                self.assertIsNone(self.classify(UpstreamHTTPError(path, status, {})))

    def test_readback_limit_has_request_rate_limited_public_code(self):
        error = conversation.image_quota_error(image_quota.ImageQuotaExceeded(quota_evidence('upstream_http_conversation_throttle').record()))
        self.assertEqual(429, error.status_code)
        self.assertEqual('image_request_rate_limited', error.code)


class ReadbackIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.service = account_service()
        self.addCleanup(self.service._image_ledger.close)
        self.enterContext(patch('services.account_service.account_service', self.service))
        self.enterContext(patch.object(conversation, 'account_service', self.service))

    def test_direct_readback_marks_account_and_raises_rate_limit(self):
        response = Mock(status_code=429, text='private', headers={'Retry-After': '60'})
        response.json.return_value = {'detail': 'private'}
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend.access_token = TOKEN
        backend.base_url = 'https://chatgpt.com'
        backend.image_conversation_id = CID
        backend._image_quota_checks = True
        backend.session = Mock(headers={})
        backend.session.get.return_value = response
        with self.assertRaises(image_quota.ImageQuotaExceeded):
            backend._get_conversation(CID)
        row = self.service.get_account(TOKEN)
        self.assertEqual('cooldown', row['image_limit']['state'])
        self.assertEqual(CID, backend.image_conversation_id)
        self.assertNotIn(TOKEN, self.service._list_ready_candidate_tokens())

    def test_delivery_readback_429_stops_after_one_get(self):
        harness = delivery_fixture.DeliveryTimeoutRecoveryTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        backend = harness.backend(images.revisions(1)[:-1])
        backend._image_delivery_deadline = harness.clock.now + 8
        backend.access_token = TOKEN
        backend._get_conversation.side_effect = UpstreamHTTPError('/backend-api/conversations/' + images.CID, 429, {})
        with self.assertRaises(image_quota.ImageQuotaExceeded):
            harness.run_result(backend)
        self.assertEqual(1, backend._get_conversation.call_count)
        harness.assert_no_generation(backend)

    def test_stream_parallel_readback_429_marks_account_without_replay(self):
        harness = stream_fixture.ImageGenerationStreamTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        server = harness.server([stream_fixture.event(stream_fixture.user_message())], statuses={
            '/backend-api/conversations/' + stream_fixture.CONVERSATION_ID: 429})
        backend = harness.backend(server)
        backend.access_token = TOKEN
        with self.assertRaises(image_quota.ImageQuotaExceeded):
            list(harness.stream(backend))
        self.assertEqual(1, server.post_count)
        self.assertEqual(1, len(server.get_paths))
        self.assertEqual('cooldown', self.service.get_account(TOKEN)['image_limit']['state'])

    def test_submitted_generation_is_not_refunded_when_readback_is_limited(self):
        backend = Mock(access_token=TOKEN, image_conversation_id=CID)
        backend._image_generation_committed = True
        backend._image_recovery_started = False
        error = image_quota.ImageQuotaExceeded(quota_evidence('upstream_http_conversation_throttle').record())
        self.service.get_available_access_token = Mock(return_value=TOKEN)
        self.service.mark_image_result = Mock()
        with patch.object(conversation, 'OpenAIBackendAPI', return_value=backend), patch.object(
                conversation, 'stream_image_outputs', side_effect=error):
            with self.assertRaises(conversation.ImageGenerationError):
                conversation._generate_single_image_impl(conversation.ConversationRequest(
                    model='gpt-image-2.6-sunburst', prompt='synthetic', image_transport='conversation'), 1, 1)
        self.assertFalse(self.service.mark_image_result.call_args.kwargs['not_consumed'])


class CooldownGateTests(unittest.TestCase):
    def setUp(self):
        self.service = account_service()
        self.addCleanup(self.service._image_ledger.close)
        self.enterContext(patch('services.account_service.browser_sentinel_enabled', return_value=False))
        self.enterContext(patch.object(type(config), 'image_account_concurrency', new_callable=PropertyMock, return_value=3))

    def test_already_reserved_request_cannot_submit_after_cooldown(self):
        service = self.service
        selected = service._acquire_next_candidate_token(generation_id='old')
        service.mark_image_quota_limit(selected, quota_evidence())
        with self.assertRaises(image_quota.ImageQuotaExceeded):
            service.assert_image_submission_allowed(selected, 'old')
        self.assertNotIn(selected, service._list_ready_candidate_tokens())

    def test_late_success_does_not_clear_another_requests_cooldown(self):
        service = self.service
        selected = service._acquire_next_candidate_token(generation_id='old')
        service.assert_image_submission_allowed(selected, 'old')
        service.mark_image_quota_limit(selected, quota_evidence())
        service.mark_image_result(selected, True, generation_id='old')
        self.assertEqual('cooldown', service.get_account(selected)['image_limit']['state'])
        self.assertNotIn(selected, service._list_ready_candidate_tokens())

    def test_probe_permission_cannot_be_used_by_another_reserved_generation(self):
        service = self.service
        service.mark_image_quota_limit(TOKEN, quota_evidence())
        service._accounts[TOKEN]['image_limit'].update(state='probe_ready', reported_quota=10, restore_at=time.time()-1)
        selected = service._acquire_next_candidate_token(excluded_tokens={'healthy-fixture'}, generation_id='probe')
        # 一个遗留的合法额度预占不能借用新探测的身份，必须核对generation_id。
        service._image_ledger.reserve(service._accounts[selected]['management_id'], 'stale')
        with self.assertRaises(image_quota.ImageQuotaExceeded):
            service.assert_image_submission_allowed(selected, 'stale')
        service.assert_image_submission_allowed(selected, 'probe')

    def test_stale_result_cannot_complete_or_remove_active_probe(self):
        service = self.service
        service.mark_image_quota_limit(TOKEN, quota_evidence())
        service._accounts[TOKEN]['image_limit'].update(state='probe_ready', reported_quota=10, restore_at=time.time()-1)
        selected = service._acquire_next_candidate_token(excluded_tokens={'healthy-fixture'}, generation_id='probe')
        service._image_ledger.reserve(service._accounts[selected]['management_id'], 'stale')
        service.mark_image_result(selected, True, generation_id='stale')
        self.assertEqual('probing', service.get_account(selected)['image_limit']['state'])
        service.assert_image_submission_allowed(selected, 'probe')


class LateResultAndResumeTests(unittest.TestCase):
    setUp = CooldownGateTests.setUp

    def test_manual_rate_limit_is_not_cleared_by_late_generation_success(self):
        service = self.service
        selected = service._acquire_next_candidate_token(generation_id='old')
        service.assert_image_submission_allowed(selected, 'old')
        service.update_account(selected, {'status': '限流'}, quiet=True)
        service.mark_image_result(selected, True, generation_id='old')
        self.assertEqual('限流', service.get_account(selected)['status'])
        self.assertNotIn(selected, service._list_ready_candidate_tokens())

    def test_manual_rate_limit_is_not_cleared_by_late_resume_success(self):
        service = self.service
        selected = service._acquire_next_candidate_token(generation_id='old')
        service.assert_image_submission_allowed(selected, 'old')
        service.bind_image_generation('old', CID)
        service.mark_image_result(selected, False, generation_id='old')
        service.update_account(selected, {'status': '限流'}, quiet=True)
        self.assertTrue(service.complete_image_generation_from_resume(selected, CID))
        self.assertEqual('限流', service.get_account(selected)['status'])
        self.assertNotIn(selected, service._list_ready_candidate_tokens())

    def test_readback_cooldown_blocks_other_conversation_resume(self):
        service = self.service
        service.mark_image_quota_limit(TOKEN, quota_evidence('upstream_http_conversation_throttle'))
        with self.assertRaises(image_quota.ImageQuotaExceeded):
            service.acquire_image_resume_token(service.image_account_ref(TOKEN), wait_timeout_secs=0,
                                               conversation_id='another-existing-conversation')
