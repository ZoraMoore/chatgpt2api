"""资料刷新不得将被待确认占用的零预算误标为正常。"""
import copy
import time
import unittest
from unittest.mock import patch, PropertyMock
from services.account_service import AccountService, config
from services.image_quota_ledger import ImageQuotaLedger
from test_sub2api_full_import import make_service

TOKEN = 'budget-status-fixture'

class BudgetStatusConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('services.account_service.browser_sentinel_enabled', return_value=False))
        self.enterContext(patch.object(type(config), 'image_account_concurrency', new_callable=PropertyMock, return_value=3))
        self.enterContext(patch.object(type(config), 'auto_remove_rate_limited_accounts', new_callable=PropertyMock, return_value=False))
        self.service, self.storage = make_service([{'access_token': TOKEN, 'type': 'edu_plus', 'quota': 2, 'status': '正常'}])
        self.addCleanup(self.service._image_ledger.close)
        self.service.get_account(TOKEN)
        self.ref = self.service._accounts[TOKEN]['management_id']

    def pending(self, key='pending'):
        ledger = self.service._image_ledger
        self.assertTrue(ledger.reserve(self.ref, key))
        ledger.submit(key)
        ledger.finish(key, success=False)

    def exhausted(self):
        self.pending()
        ledger = self.service._image_ledger
        ledger.reserve(self.ref, 'success')
        ledger.submit('success')
        ledger.finish('success', success=True)
        self.assertEqual(0, ledger.view(self.ref)['available'])

    def refresh(self):
        return self.service.update_account(TOKEN, {'status': '正常', 'quota': 120,
            'limits_progress': [{'feature_name': 'image_gen', 'remaining': 120}]}, remote=True, quiet=True)

    def test_profile_refresh_cannot_mark_pending_exhausted_budget_normal(self):
        self.exhausted()
        before = self.service._image_ledger.view(self.ref)
        row = self.refresh()
        self.assertEqual(('限流', 0), (row['status'], row['image_quota_available']))
        self.assertEqual([], self.service._list_ready_candidate_tokens())
        after = self.service._image_ledger.view(self.ref)
        for field in ('cycle', 'balance', 'used', 'uncertain'):
            self.assertEqual(before[field], after[field])

    def test_last_reserved_request_is_not_blocked_by_zero_display(self):
        self.pending()
        selected = self.service._acquire_next_candidate_token(generation_id='last', wait_timeout_secs=0)
        row = self.refresh()
        self.assertEqual(('正常', 0, 1), (row['status'], row['image_quota_available'], row['image_quota_reserved']))
        self.service.assert_image_submission_allowed(selected, 'last')
        self.service.mark_image_result(selected, True, generation_id='last')
        self.assertEqual('限流', self.service.get_account(TOKEN)['status'])

    def test_inflight_refresh_does_not_prevent_remaining_success_from_recovering_capacity(self):
        for key in ('a', 'b'):
            selected = self.service._acquire_next_candidate_token(generation_id=key, wait_timeout_secs=0)
            self.service.assert_image_submission_allowed(selected, key)
        self.assertEqual('正常', self.refresh()['status'])
        self.service.mark_image_result(TOKEN, False, generation_id='a', not_consumed=True)
        self.service.mark_image_result(TOKEN, True, generation_id='b')
        self.assertEqual(('正常', 1), (self.service.get_account(TOKEN)['status'], self.service.get_account(TOKEN)['image_quota_available']))

    def test_failed_request_using_last_capacity_marks_budget_blocked(self):
        self.pending()
        selected = self.service._acquire_next_candidate_token(generation_id='last', wait_timeout_secs=0)
        self.service.assert_image_submission_allowed(selected, 'last')
        self.service.mark_image_result(selected, False, generation_id='last')
        self.assertEqual(('限流', 0), (self.service.get_account(TOKEN)['status'], self.service.get_account(TOKEN)['image_quota_available']))

    def test_startup_reconciles_existing_normal_zero_without_changing_ledger(self):
        self.exhausted()
        ledger = self.service._image_ledger
        before = ledger.view(self.ref)
        saved = copy.deepcopy(list(self.service._accounts.values()))
        storage = copy.deepcopy(self.storage)
        storage.load_accounts.return_value = saved
        restarted = AccountService(storage, image_ledger=ledger)
        self.assertEqual('限流', restarted.get_account(TOKEN)['status'])
        self.assertEqual(before, ledger.view(self.ref))

    def test_background_refresh_keeps_cooldown_and_disabled_states(self):
        now = time.time()
        self.service.update_account(TOKEN, {'image_limit': {'state': 'cooldown', 'event_id': 'a'*64,
            'source': 'assistant_final', 'restore_at': now+3600, 'next_check_at': now+3600}}, quiet=True)
        self.assertEqual('cooldown', self.refresh()['image_limit']['state'])
        self.service.update_account(TOKEN, {'status': '禁用'}, quiet=True)
        self.assertEqual('禁用', self.refresh()['status'])

    def test_confirmed_unconsumed_pending_releases_only_local_budget_block(self):
        self.exhausted()
        self.refresh()
        self.service.mark_image_result(TOKEN, False, generation_id='pending', not_consumed=True)
        row = self.service.get_account(TOKEN)
        self.assertEqual(('正常', 1), (row['status'], row['image_quota_available']))
        self.assertEqual([TOKEN], self.service._list_ready_candidate_tokens())

    def test_fresh_cycle_positive_budget_can_restore_normal(self):
        self.exhausted()
        self.refresh()
        ledger = self.service._image_ledger
        ledger.observe(self.ref, cap=None, remaining=0, reset_at=time.time()-2)
        # 使用明确独立的新周期证据，不因一次读取429或旧pending补额。
        ledger.observe(self.ref, cap=None, remaining=4, reset_at=time.time()+3600)
        row = self.service.update_account(TOKEN, {'status': '正常'}, remote=True, quiet=True)
        self.assertEqual(('正常', 4), (row['status'], row['image_quota_available']))

if __name__ == '__main__':
    unittest.main()
