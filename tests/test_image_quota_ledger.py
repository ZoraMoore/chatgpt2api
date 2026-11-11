import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
try:
    from services.image_quota_ledger import ImageQuotaLedger
except ImportError:
    ImageQuotaLedger = None


class ImageQuotaLedgerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ImageQuotaLedger, '需要持久化额度账本实现')
        self.ledger = ImageQuotaLedger()
        self.addCleanup(self.ledger.close)
        self.ledger.observe('account', cap=1000, remaining=37, reset_at=200, now=100)

    def test_migration_keeps_unknown_usage_and_current_balance(self):
        row = self.ledger.view('account')
        self.assertEqual((1000, None, 37), (row['cap'], row['used'], row['available']))

    def test_refresh_cannot_recredit_same_cycle(self):
        self.ledger.reserve('account', 'generation')
        self.ledger.submit('generation')
        self.ledger.finish('generation', success=True)
        self.ledger.observe('account', cap=1000, remaining=120, reset_at=200, now=120)
        self.assertEqual(36, self.ledger.view('account')['available'])

    def test_last_unit_can_only_be_reserved_once_concurrently(self):
        self.ledger.observe('single', cap=1000, remaining=1, reset_at=200, now=100)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(lambda i:self.ledger.reserve('single',str(i)),range(24)))
        self.assertEqual(1, sum(results))

    def test_uncertain_submit_is_not_refunded_and_resume_settles_once(self):
        self.ledger.reserve('account','g')
        self.ledger.submit('g')
        self.ledger.bind_conversation('g','conversation')
        self.ledger.finish('g',success=False)
        self.assertEqual((36,1),(self.ledger.view('account')['available'],self.ledger.view('account')['uncertain']))
        self.assertTrue(self.ledger.complete_conversation('account','conversation'))
        self.assertFalse(self.ledger.complete_conversation('account','conversation'))
        self.assertEqual(36,self.ledger.view('account')['available'])

    def test_unsubmitted_failure_refunds_reservation(self):
        self.ledger.reserve('account','g'); self.ledger.finish('g',success=False)
        self.assertEqual(37,self.ledger.view('account')['available'])

    def test_period_requires_expired_old_and_new_authoritative_deadline(self):
        self.ledger.observe('account',cap=1000,remaining=1000,reset_at=300,now=150)
        self.assertIsNone(self.ledger.view('account')['used'])
        self.ledger.observe('account',cap=1000,remaining=1000,reset_at=400,now=301)
        self.assertEqual((0,1000),(self.ledger.view('account')['used'],self.ledger.view('account')['available']))

    def test_restart_retains_uncertain_and_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'quota.sqlite3'
            ledger=ImageQuotaLedger(path)
            ledger.observe('a',cap=1000,remaining=10,reset_at=200,now=100)
            ledger.reserve('a','submitted'); ledger.submit('submitted')
            ledger.reserve('a','not-submitted'); ledger.close()
            ledger=ImageQuotaLedger(path)
            try:
                self.assertEqual((9,1),(ledger.view('a')['available'],ledger.view('a')['uncertain']))
                self.assertTrue(ledger.finish('submitted',success=True))
                self.assertFalse(ledger.finish('submitted',success=True))
                self.assertEqual(9,ledger.view('a')['available'])
            finally:ledger.close()

    def test_old_period_result_does_not_charge_new_cycle(self):
        self.ledger.reserve('account','old'); self.ledger.submit('old')
        self.ledger.observe('account',cap=1000,remaining=1000,reset_at=400,now=201)
        self.ledger.finish('old',success=True)
        self.assertEqual(1000,self.ledger.view('account')['available'])

    def test_second_writer_cannot_release_live_reservations(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'quota.sqlite3'
            first=ImageQuotaLedger(path)
            try:
                first.observe('a',cap=1000,remaining=1,reset_at=200,now=100)
                first.reserve('a','live')
                with self.assertRaisesRegex(RuntimeError,'already|owner|process'):
                    second=ImageQuotaLedger(path)
                    second.close()
                self.assertEqual(0,first.view('a')['available'])
            finally:first.close()

    def test_duplicate_id_cannot_move_between_accounts(self):
        self.ledger.observe('b',cap=1000,remaining=10,reset_at=200,now=100)
        self.assertTrue(self.ledger.reserve('account','g'))
        self.assertFalse(self.ledger.reserve('b','g'))

if __name__=='__main__':unittest.main()
