"""固定额度周期及离线保守恢复的回归验证，不使用真实账号。"""
import importlib.util
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from services.image_quota_ledger import ImageQuotaLedger


class FixedQuotaCycleTests(unittest.TestCase):
    def setUp(self):
        self.ledger = ImageQuotaLedger()
        self.addCleanup(self.ledger.close)

    def exhaust_with_pending(self):
        self.ledger.observe('account', cap=None, remaining=3, reset_at=200, now=100)
        for index in range(3):
            key = f'g-{index}'
            self.assertTrue(self.ledger.reserve('account', key))
            self.ledger.submit(key)
            self.ledger.finish(key, success=index != 0)

    def test_refresh_does_not_push_the_current_cycle_deadline(self):
        self.exhaust_with_pending()
        self.ledger.observe('account', cap=None, remaining=3, reset_at=250, now=150)
        view = self.ledger.view('account')
        self.assertEqual(200, view['reset_at'])
        self.assertEqual((0, 1, 1), (view['available'], view['uncertain'], view['cycle']))

    def test_continuous_refresh_can_reach_a_new_cycle(self):
        self.exhaust_with_pending()
        for now in (120, 140, 160, 180, 201):
            self.ledger.observe('account', cap=None, remaining=3, reset_at=now + 100, now=now)
        view = self.ledger.view('account')
        self.assertEqual((3, 0, 2), (view['available'], view['uncertain'], view['cycle']))
        self.assertTrue(self.ledger.finish('g-0', success=True))
        self.assertFalse(self.ledger.finish('g-0', success=True))
        self.assertEqual(3, self.ledger.view('account')['available'])

    def test_zero_snapshot_never_refills_an_expired_cycle(self):
        self.exhaust_with_pending()
        self.ledger.observe('account', cap=None, remaining=0, reset_at=400, now=201)
        view = self.ledger.view('account')
        self.assertEqual((0, 1), (view['available'], view['cycle']))

    def test_missing_deadline_can_be_initialized(self):
        self.ledger.observe('account', cap=None, remaining=3, now=100)
        self.ledger.observe('account', cap=None, remaining=3, reset_at=200, now=110)
        self.assertEqual(200, self.ledger.view('account')['reset_at'])


class OfflineQuotaRecoveryTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ.get('QUOTA_RECOVERY_MODULE',
                    str(Path(__file__).resolve().parents[1] / 'scripts/recover_stalled_image_quota.py')))
        spec = importlib.util.spec_from_file_location('quota_recovery', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'quota.sqlite3'
        self.accounts = self.root / 'accounts.json'
        self.now = time.time()
        self.ref = 'a' * 64
        self.other = 'b' * 64
        ledger = ImageQuotaLedger(self.database)
        for ref in (self.ref, self.other):
            ledger.observe(ref, cap=None, remaining=3, reset_at=self.now + 86400, now=self.now)
            for index in range(3):
                key = f'{ref}-{index}'
                ledger.reserve(ref, key)
                ledger.submit(key)
                ledger.finish(key, success=index != 0)
        ledger.close()
        self.row = {'management_id': self.ref, 'email': 'fixture@example.test', 'status': '正常',
                    'type': 'edu_plus', 'quota': 1,
                    'limits_progress': [{'feature_name': 'image_gen', 'remaining': 120}]}
        self.write_accounts()

    def write_accounts(self):
        self.accounts.write_text(json.dumps([self.row]))

    def recover(self, apply=False):
        return self.module.recover(self.accounts, self.database, [self.ref],
                                   backup_path=self.root / 'before.sqlite3', apply=apply, now=self.now)

    def snapshot(self):
        with sqlite3.connect(self.database) as db:
            return (db.execute('SELECT * FROM image_budgets ORDER BY account').fetchall(),
                    db.execute('SELECT * FROM image_leases ORDER BY id').fetchall())

    def test_preview_does_not_write(self):
        before = self.snapshot()
        report = self.recover()
        self.assertEqual(1, report['eligible'])
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / 'before.sqlite3').exists())

    def test_recovery_preserves_pending_other_accounts_and_duplicate_safety(self):
        before = self.snapshot()
        report = self.recover(apply=True)
        after = self.snapshot()
        self.assertEqual(1, report['restored'])
        self.assertEqual(119, report['entries'][0]['available_after'])
        self.assertEqual(before[1], after[1])
        self.assertEqual(before[0][1], after[0][1])
        self.assertTrue((self.root / 'before.sqlite3').is_file())
        self.assertEqual(0, self.recover(apply=True)['restored'])
        ledger = ImageQuotaLedger(self.database)
        self.addCleanup(ledger.close)
        self.assertTrue(ledger.finish(f'{self.ref}-0', success=True))
        self.assertEqual(119, ledger.view(self.ref)['available'])

    def test_all_limit_states_and_disabled_accounts_are_preserved(self):
        before = self.snapshot()
        for state in ('cooldown', 'awaiting_check', 'probe_ready', 'probing'):
            self.row['image_limit'] = {'state': state}
            self.write_accounts()
            self.assertEqual(0, self.recover(apply=True)['restored'])
        self.row.pop('image_limit')
        for status in ('限流', '异常', '禁用'):
            self.row['status'] = status
            self.write_accounts()
            self.assertEqual(0, self.recover(apply=True)['restored'])
        self.assertEqual(before, self.snapshot())

    def test_zero_or_stale_upstream_snapshot_is_not_recovered(self):
        self.row['limits_progress'][0]['remaining'] = 0
        self.write_accounts()
        self.assertEqual(0, self.recover(apply=True)['restored'])
        self.row['limits_progress'][0]['remaining'] = 120
        self.write_accounts()
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE image_budgets SET observed_at=? WHERE account=?', (self.now - 901, self.ref))
        self.assertEqual(0, self.recover(apply=True)['restored'])

    def test_running_owner_and_submitted_generation_block_recovery(self):
        owner = ImageQuotaLedger(self.database)
        try:
            with self.assertRaisesRegex(RuntimeError, 'owner'):
                self.recover(apply=True)
        finally:
            owner.close()
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE image_leases SET state='submitted' WHERE id=?", (f'{self.ref}-0',))
        self.assertEqual(0, self.recover(apply=True)['restored'])


if __name__ == '__main__':
    unittest.main()
