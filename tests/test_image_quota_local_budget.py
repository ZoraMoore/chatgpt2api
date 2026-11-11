"""ProLite本地周期额度不能被旧120张快照重新截断。"""
import tempfile
import sqlite3
import time
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from services.image_quota_ledger import ImageQuotaLedger
from services.account_service import config
from test_sub2api_full_import import make_service


class LocalBudgetLedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = ImageQuotaLedger()
        self.addCleanup(self.ledger.close)

    def enable(self, remaining=120):
        self.ledger.observe('a', cap=1000, remaining=remaining, reset_at=200, now=100)
        self.assertTrue(callable(getattr(self.ledger, 'enable_local_budget', None)),
                        '需要幂等迁移本地周期额度，不能只修改cap')
        return self.ledger.enable_local_budget('a')

    def test_old_120_becomes_1000_and_old_deduction_is_preserved(self):
        self.assertEqual(999, self.enable(119)['available'])
        self.assertIsNone(self.ledger.view('a')['used'])
        self.assertEqual(999, self.ledger.enable_local_budget('a')['available'])

    def test_refresh_does_not_clamp_or_recredit_local_budget(self):
        self.assertEqual(1000, self.enable()['available'])
        for n in range(121):
            generation = f'g-{n}'
            self.assertTrue(self.ledger.reserve('a', generation))
            self.ledger.submit(generation)
            self.ledger.finish(generation, success=True)
        self.ledger.observe('a', cap=1000, remaining=120, reset_at=200, now=150)
        self.assertEqual(879, self.ledger.view('a')['available'])

    def test_upstream_zero_blocks_without_erasing_local_balance(self):
        self.enable()
        self.ledger.observe('a', cap=1000, remaining=0, reset_at=200, now=150)
        self.assertEqual(0, self.ledger.view('a')['available'])
        self.assertFalse(self.ledger.reserve('a', 'blocked'))
        self.ledger.observe('a', cap=1000, remaining=120, reset_at=200, now=160)
        self.assertEqual(1000, self.ledger.view('a')['available'])

    def test_pending_is_retained_and_settled_once(self):
        self.enable()
        self.ledger.reserve('a', 'pending')
        self.ledger.submit('pending')
        self.ledger.bind_conversation('pending', 'conversation')
        self.ledger.finish('pending', success=False)
        self.assertEqual((999, 1), (self.ledger.view('a')['available'], self.ledger.view('a')['uncertain']))
        self.assertTrue(self.ledger.complete_conversation('a', 'conversation'))
        self.assertFalse(self.ledger.complete_conversation('a', 'conversation'))
        self.assertEqual(999, self.ledger.view('a')['available'])

    def test_existing_success_survives_migration_even_if_old_snapshot_recredited(self):
        self.ledger.observe('a', cap=None, remaining=120, reset_at=200, now=100)
        self.ledger.reserve('a', 'completed')
        self.ledger.submit('completed')
        self.ledger.finish('completed', success=True)
        self.ledger.observe('a', cap=None, remaining=120, reset_at=200, now=110)
        self.ledger.set_cap('a', 1000)
        self.assertTrue(callable(getattr(self.ledger, 'enable_local_budget', None)))
        self.assertEqual(999, self.ledger.enable_local_budget('a')['available'])

    def test_known_cycle_usage_is_preserved_and_new_cycle_gets_1000(self):
        self.enable()
        self.ledger.observe('a', cap=1000, remaining=120, reset_at=400, now=201)
        self.assertEqual((1000, 0), (self.ledger.view('a')['available'], self.ledger.view('a')['used']))
        self.ledger.reserve('a', 'used')
        self.ledger.submit('used')
        self.ledger.finish('used', success=True)
        self.assertEqual((999, 1), (self.ledger.enable_local_budget('a')['available'], self.ledger.view('a')['used']))

    def test_restart_does_not_repeat_migration(self):
        self.enable()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger.sqlite3'
            ledger = ImageQuotaLedger(path)
            ledger.observe('a', cap=1000, remaining=119, reset_at=200, now=100)
            ledger.enable_local_budget('a')
            ledger.reserve('a', 'completed')
            ledger.submit('completed')
            ledger.finish('completed', success=True)
            ledger.close()
            ledger = ImageQuotaLedger(path)
            try:
                self.assertEqual(998, ledger.enable_local_budget('a')['available'])
            finally:
                ledger.close()

    def test_old_schema_and_known_usage_migrate_without_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE image_budgets (account TEXT PRIMARY KEY,cycle INTEGER NOT NULL DEFAULT 1,cap INTEGER,used INTEGER,balance INTEGER NOT NULL,upstream INTEGER NOT NULL,reset_at REAL,observed_at REAL NOT NULL,initialized INTEGER NOT NULL DEFAULT 1)')
            db.execute("INSERT INTO image_budgets VALUES ('a',2,1000,999,1,120,200,100,1)")
            db.commit()
            db.close()
            ledger = ImageQuotaLedger(path)
            try:
                row = ledger.enable_local_budget('a')
                self.assertEqual((1, 999, 2), (row['available'], row['used'], row['cycle']))
                self.assertTrue(ledger.reserve('a', 'last'))
                self.assertFalse(ledger.reserve('a', 'overflow'))
                ledger.submit('last')
                ledger.finish('last', success=True)
                self.assertEqual(0, ledger.view('a')['available'])
            finally:
                ledger.close()


class LocalBudgetAccountTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('services.account_service.browser_sentinel_enabled', return_value=False))
        self.enterContext(patch.object(type(config), 'auto_remove_rate_limited_accounts', new_callable=PropertyMock, return_value=False))

    def service(self, quota=120, **extra):
        service, _ = make_service([{'access_token': 'pro', 'type': 'self_serve_business_prolite', 'quota': quota, **extra},
                                   {'access_token': 'edu', 'type': 'edu_plus', 'quota': 120}])
        self.addCleanup(service._image_ledger.close)
        return service

    def test_account_api_and_raw_scheduler_balance_agree(self):
        service = self.service(119)
        self.assertEqual(999, service.get_account('pro')['quota'])
        self.assertEqual(999, service._accounts['pro']['quota'])
        self.assertEqual(120, service.get_account('edu')['quota'])

    def test_refresh_does_not_replace_local_quota_with_120(self):
        service = self.service()
        service.get_account('pro')
        token = service._acquire_next_candidate_token(generation_id='generation', wait_timeout_secs=0)
        service.assert_image_submission_allowed(token, 'generation')
        service.mark_image_result(token, True, generation_id='generation')
        row = service.update_account('pro', {'quota': 120, 'status': '正常'}, quiet=True, remote=True)
        self.assertEqual(999, row['quota'])

    def test_legacy_result_without_generation_id_still_debits(self):
        service = self.service()
        token = service._acquire_next_candidate_token(wait_timeout_secs=0)
        service.mark_image_result(token, True)
        self.assertEqual(999, service.get_account('pro')['quota'])

    def test_true_cooldown_and_disabled_accounts_remain_unavailable(self):
        for status in ['异常', '禁用']:
            with self.subTest(status=status):
                service = self.service(status=status)
                self.assertEqual(0, service.get_account('pro')['quota'])
        service = self.service(image_limit={'state': 'cooldown', 'event_id': '1' * 64,
            'restore_at': time.time() + 3600, 'next_check_at': time.time() + 3600})
        self.assertEqual(0, service.get_account('pro')['quota'])
        self.assertEqual('edu', service._acquire_next_candidate_token(wait_timeout_secs=0))

