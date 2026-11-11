"""冷却到期后通过唯一真实业务请求恢复，不依赖网页初始化额度。"""
import copy
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from services.account_service import config
from services.image_quota import CHECK_RETRY, ImageQuotaEvidence, ImageQuotaExceeded
from test_sub2api_full_import import make_service


TOKEN = 'business-recovery-fixture'


class BusinessLimitRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('services.account_service.browser_sentinel_enabled', return_value=False))
        self.enterContext(patch.object(type(config), 'image_account_concurrency', new_callable=PropertyMock, return_value=3))
        self.enterContext(patch.object(type(config), 'auto_remove_rate_limited_accounts', new_callable=PropertyMock, return_value=False))
        self.service, self.storage = make_service([{'access_token': TOKEN, 'type': 'edu_plus',
            'quota': 10, 'status': '正常',
            'limits_progress': [{'feature_name': 'image_gen', 'remaining': 10}]}])
        self.addCleanup(self.service._image_ledger.close)
        self.service.list_accounts()[0]
        self.ref = self.service._accounts[TOKEN]['management_id']

    def limit(self, source='assistant_final', future=False, identity='event-one'):
        now = time.time()
        evidence = ImageQuotaEvidence('上游图像生成已达到上限', source, 'original-cid', identity,
            identity, now - 100, now, now + 3600 if future else now - 1)
        self.service.mark_image_quota_limit(TOKEN, evidence)

    def prepare(self):
        backend = Mock()
        backend.get_image_quota.return_value = {'quota': 120,
            'limits_progress': [{'feature_name': 'image_gen', 'remaining': 120}]}
        with patch('services.openai_backend_api.OpenAIBackendAPI', return_value=backend) as factory:
            self.service._refresh_image_limit(TOKEN)
            factory.assert_not_called()

    def begin_probe(self, key='probe'):
        token = self.service._acquire_next_candidate_token(generation_id=key, wait_timeout_secs=0)
        self.service.assert_image_submission_allowed(token, key)
        return token

    def test_expired_limit_opens_validation_without_upstream_quota_query(self):
        self.limit()
        self.prepare()
        row = self.service.list_accounts()[0]
        self.assertEqual('probe_ready', row['image_limit_state'])
        self.assertEqual('限流', row['status'])
        self.assertEqual(0, row['image_quota_available'])

    def test_future_limit_disabled_and_abnormal_accounts_stay_blocked(self):
        self.limit(future=True)
        self.prepare()
        self.assertEqual('cooldown', self.service.list_accounts()[0]['image_limit_state'])
        self.assertEqual([], self.service._list_ready_candidate_tokens())
        for status in ('禁用', '异常'):
            self.service.update_account(TOKEN, {'status': status}, quiet=True)
            self.prepare()
            self.assertEqual([], self.service._list_ready_candidate_tokens())

    def test_normal_selection_preflight_does_not_fetch_profile_for_owned_probe(self):
        self.limit()
        self.prepare()
        with patch.object(self.service, 'fetch_remote_info', side_effect=AssertionError('unexpected profile query')):
            selected = self.service.get_available_access_token(generation_id='probe')
        self.assertEqual(TOKEN, selected)
        self.service.release_image_slot(selected, generation_id='probe')

    def test_zero_ledger_can_reserve_only_one_validation_and_success_settles_once(self):
        self.service._image_ledger.observe(self.ref, cap=None, remaining=0)
        self.limit()
        self.prepare()
        def acquire(index):
            try:
                return self.service._acquire_next_candidate_token(generation_id=f'probe-{index}', wait_timeout_secs=0)
            except RuntimeError:
                return None
        with ThreadPoolExecutor(max_workers=12) as pool:
            selected = list(pool.map(acquire, range(12)))
        self.assertEqual(1, sum(token == TOKEN for token in selected))
        key = next(key for token, (event, key) in self.service._image_probe_leases.items() if token == TOKEN)
        self.service.assert_image_submission_allowed(TOKEN, key)
        self.service.mark_image_result(TOKEN, True, generation_id=key)
        row = self.service.list_accounts()[0]
        self.assertEqual(('正常', '', 9), (row['status'], row['image_limit_state'], row['image_quota_available']))
        self.service.mark_image_result(TOKEN, True, generation_id=key)
        self.assertEqual(9, self.service.list_accounts()[0]['image_quota_available'])
        self.assertEqual(1, self.service.list_accounts()[0]['success'])

    def test_failed_validation_waits_before_next_single_request(self):
        self.limit()
        self.prepare()
        self.begin_probe()
        self.service.mark_image_result(TOKEN, False, generation_id='probe')
        row = self.service.list_accounts()[0]
        self.assertEqual('awaiting_check', row['image_limit_state'])
        self.assertGreater((row['image_limit'] or {})['next_check_at'], time.time() + CHECK_RETRY - 2)
        self.assertEqual([], self.service._list_ready_candidate_tokens())

    def test_new_limit_cannot_be_cleared_by_previous_probe_success(self):
        self.limit()
        self.prepare()
        self.begin_probe()
        self.limit(future=True, identity='new-event')
        self.service.mark_image_result(TOKEN, True, generation_id='probe')
        self.assertEqual('cooldown', self.service.list_accounts()[0]['image_limit_state'])
        self.assertEqual(0, self.service.list_accounts()[0]['image_quota_available'])

    def test_readback_throttle_success_does_not_refill_or_reset_local_budget(self):
        self.service._image_ledger.reserve(self.ref, 'old-pending')
        self.service._image_ledger.submit('old-pending')
        self.service._image_ledger.finish('old-pending', success=False)
        self.service._image_ledger.observe(self.ref, cap=None, remaining=0)
        self.limit('upstream_http_conversation_throttle')
        self.prepare()
        before = self.service._image_ledger.view(self.ref)
        self.begin_probe()
        self.service.mark_image_result(TOKEN, True, generation_id='probe')
        after = self.service._image_ledger.view(self.ref)
        self.assertEqual(before['cycle'], after['cycle'])
        self.assertEqual(8, after['available'])
        self.assertEqual(1, after['uncertain'])

    def test_original_probe_resume_success_recovers_once(self):
        self.limit()
        self.prepare()
        self.begin_probe()
        self.service.bind_image_generation('probe', 'probe-cid')
        self.service.bind_image_probe_conversation(TOKEN, 'probe-cid', generation_id='probe')
        self.service.mark_image_result(TOKEN, False, generation_id='probe')
        self.assertTrue(self.service.complete_image_generation_from_resume(TOKEN, 'probe-cid'))
        self.assertTrue(self.service.complete_image_probe_from_resume(TOKEN, 'probe-cid'))
        self.assertEqual(9, self.service.list_accounts()[0]['image_quota_available'])
        self.assertFalse(self.service.complete_image_probe_from_resume(TOKEN, 'probe-cid'))

    def test_ordinary_resume_success_cannot_clear_a_different_limit(self):
        self.limit()
        self.prepare()
        self.begin_probe()
        self.service.bind_image_generation('probe', 'probe-cid')
        self.service.bind_image_probe_conversation(TOKEN, 'probe-cid', generation_id='probe')
        self.service.mark_image_result(TOKEN, False, generation_id='probe')
        self.limit(future=True, identity='new-event')
        self.assertTrue(self.service.complete_image_generation_from_resume(TOKEN, 'probe-cid'))
        self.assertFalse(self.service.complete_image_probe_from_resume(TOKEN, 'probe-cid'))
        self.assertEqual('cooldown', self.service.list_accounts()[0]['image_limit_state'])

    def test_restart_reopens_only_expired_probe_without_network(self):
        self.limit()
        self.prepare()
        saved = copy.deepcopy(self.storage.save_accounts.call_args.args[0])
        restarted, _ = make_service(saved)
        self.addCleanup(restarted._image_ledger.close)
        with patch('services.openai_backend_api.OpenAIBackendAPI') as factory:
            restarted._refresh_image_limit(TOKEN)
            factory.assert_not_called()
        self.assertEqual('probe_ready', restarted.list_accounts()[0]['image_limit_state'])

    def test_interrupted_probe_restart_keeps_backoff(self):
        self.limit()
        self.prepare()
        self.begin_probe()
        saved = copy.deepcopy(self.storage.save_accounts.call_args.args[0])
        restarted, _ = make_service(saved)
        self.addCleanup(restarted._image_ledger.close)
        with patch('services.openai_backend_api.OpenAIBackendAPI') as factory:
            restarted._refresh_image_limit(TOKEN)
            factory.assert_not_called()
        self.assertEqual('awaiting_check', restarted.list_accounts()[0]['image_limit_state'])
        self.assertEqual([], restarted._list_ready_candidate_tokens())

    def test_unknown_throttle_time_opens_only_after_thirty_second_backoff(self):
        now = time.time()
        evidence = ImageQuotaEvidence('读取请求暂时限流', 'upstream_http_conversation_throttle',
            'original-cid', 'unknown', 'unknown', now, now, None, 'unknown', now + 30)
        self.service.mark_image_quota_limit(TOKEN, evidence)
        self.prepare()
        self.assertEqual([], self.service._list_ready_candidate_tokens())
        with patch('services.account_service.time.time', return_value=now + 31):
            self.prepare()
        self.assertEqual('probe_ready', self.service.list_accounts()[0]['image_limit_state'])

    def test_previous_period_pending_is_retained_without_charging_recovered_cycle(self):
        ledger = self.service._image_ledger
        ledger.reserve(self.ref, 'old-pending')
        ledger.submit('old-pending')
        ledger.finish('old-pending', success=False)
        ledger.observe(self.ref, cap=None, remaining=0)
        self.limit()
        self.prepare()
        self.begin_probe()
        self.service.mark_image_result(TOKEN, True, generation_id='probe')
        view = ledger.view(self.ref)
        self.assertEqual((2, 1, 9), (view['cycle'], view['used'], view['available']))
        retained = ledger._db.execute("SELECT COUNT(*) FROM image_leases WHERE id='old-pending' AND state='pending'").fetchone()[0]
        self.assertEqual(1, retained)
        self.service.mark_image_result(TOKEN, True, generation_id='old-pending')
        self.assertEqual(9, ledger.view(self.ref)['available'])

    def test_disabling_account_during_probe_keeps_its_limit(self):
        self.limit()
        self.prepare()
        self.begin_probe()
        self.service.update_account(TOKEN, {'status': '禁用'}, quiet=True)
        self.service.mark_image_result(TOKEN, True, generation_id='probe')
        row = self.service.list_accounts()[0]
        self.assertEqual('禁用', row['status'])
        self.assertNotEqual('', row['image_limit_state'])

    def test_marking_account_abnormal_during_probe_keeps_its_limit(self):
        self.limit()
        self.prepare()
        self.begin_probe()
        self.service.update_account(TOKEN, {'status': '异常'}, quiet=True)
        self.service.mark_image_result(TOKEN, True, generation_id='probe')
        row = self.service.list_accounts()[0]
        self.assertEqual('异常', row['status'])
        self.assertNotEqual('', row['image_limit_state'])

    def test_validation_requires_a_generation_identity(self):
        self.limit()
        self.prepare()
        with self.assertRaisesRegex(RuntimeError, 'generation identity'):
            self.service._acquire_next_candidate_token(wait_timeout_secs=0)
        self.assertEqual('probe_ready', self.service.list_accounts()[0]['image_limit_state'])
        self.assertEqual(0, self.service.list_accounts()[0]['image_inflight'])

    def test_failed_ready_state_persistence_does_not_open_a_probe(self):
        self.limit()
        with patch.object(self.storage, 'save_accounts', side_effect=RuntimeError('fixture write failure')):
            with self.assertRaisesRegex(RuntimeError, 'write failure'):
                self.service._refresh_image_limit(TOKEN)
        self.assertEqual([], self.service._list_ready_candidate_tokens())

    def test_generation_pipeline_recovers_once_without_quota_preflight(self):
        from services.protocol import conversation
        from services.image_submission import guard_image_submission
        self.service._image_ledger.observe(self.ref, cap=None, remaining=0)
        self.limit()
        self.prepare()
        backend = SimpleNamespace(access_token=TOKEN, close=Mock(), image_conversation_id='')
        def stream(b, *_):
            guard_image_submission(b)
            b.image_conversation_id = 'probe-cid'
            self.service.bind_image_probe_conversation(TOKEN, 'probe-cid', generation_id=b._image_generation_id)
            yield conversation.ImageOutput(kind='result', model='gpt-image-2.6-sunburst', index=1, total=1,
                data=[{'url': 'https://example.test/result.png'}], conversation_id='probe-cid')
        request = conversation.ConversationRequest(model='gpt-image-2.6-sunburst', prompt='synthetic', image_transport='conversation')
        with patch.object(conversation, 'account_service', self.service), patch.object(
                conversation, 'OpenAIBackendAPI', return_value=backend) as factory, patch.object(
                conversation, 'stream_image_outputs', side_effect=stream) as generated, patch.object(
                self.service, 'fetch_remote_info', side_effect=AssertionError('unexpected quota preflight')):
            result = conversation._generate_single_image_impl(request, 1, 1)
        self.assertEqual(1, len(result))
        self.assertEqual(1, factory.call_count)
        self.assertEqual(1, generated.call_count)
        self.assertEqual(9, self.service.list_accounts()[0]['image_quota_available'])

    def test_generation_pipeline_rejection_returns_to_cooldown_without_replay(self):
        from services.protocol import conversation
        from services.image_submission import guard_image_submission
        self.limit()
        self.prepare()
        backend = SimpleNamespace(access_token=TOKEN, close=Mock(), image_conversation_id='')
        def stream(b, *_):
            guard_image_submission(b)
            now = time.time()
            evidence = ImageQuotaEvidence('图像生成已达到上限', 'assistant_final', 'probe-cid',
                'rejected', 'new-rejection', now, now, now + 1800)
            raise ImageQuotaExceeded(self.service.mark_image_quota_limit(TOKEN, evidence))
        request = conversation.ConversationRequest(model='gpt-image-2.6-sunburst', prompt='synthetic', image_transport='conversation')
        with patch.object(conversation, 'account_service', self.service), patch.object(
                conversation, 'OpenAIBackendAPI', return_value=backend) as factory, patch.object(
                conversation, 'stream_image_outputs', side_effect=stream) as generated:
            with self.assertRaises(conversation.ImageGenerationError):
                conversation._generate_single_image_impl(request, 1, 1)
        self.assertEqual(1, factory.call_count)
        self.assertEqual(1, generated.call_count)
        self.assertEqual('cooldown', self.service.list_accounts()[0]['image_limit_state'])


if __name__ == '__main__':
    unittest.main()
