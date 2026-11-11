"""额度账本与账号调度交界的回归测试，不访问上游或实际账号文件。"""
import inspect
import time
import unittest
from unittest.mock import PropertyMock, patch

from services.account_service import config
from services.image_quota_ledger import ImageQuotaLedger
from test_sub2api_full_import import make_service
from image_quota_fixtures import seed_known_local_balance


class ImageQuotaLedgerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.ledger = ImageQuotaLedger()
        self.addCleanup(self.ledger.close)
        self.now = time.time()
        self.reset_at = self.now + 3600

    def test_first_authoritative_snapshot_can_initialize_unknown_zero(self):
        self.assertIn("authoritative", inspect.signature(self.ledger.observe).parameters,
                      "账本必须区分尚无远程快照的默认零与已确认的零额度")
        self.ledger.observe("fixture-account", cap=1000, remaining=0, now=self.now,
                            authoritative=False)
        self.ledger.observe("fixture-account", cap=1000, remaining=1000,
                            reset_at=self.reset_at, now=self.now + 1, authoritative=True)
        self.assertEqual(1000, self.ledger.view("fixture-account")["available"])
        self.assertIsNone(self.ledger.view("fixture-account")["used"])

    def test_unknown_existing_usage_is_not_cleared_by_higher_remote_balance(self):
        self.ledger.observe("fixture-account", cap=1000, remaining=37,
                            reset_at=self.reset_at, now=self.now)
        self.ledger.observe("fixture-account", cap=1000, remaining=1000,
                            reset_at=self.reset_at, now=self.now + 1)
        view = self.ledger.view("fixture-account")
        self.assertIsNone(view["used"])
        self.assertEqual(37, view["available"])

    def test_new_prolite_cap_also_clamps_unknown_existing_usage(self):
        self.ledger.observe("fixture-account", cap=None, remaining=5000,
                            reset_at=self.reset_at, now=self.now)
        self.ledger.observe("fixture-account", cap=1000, remaining=5000,
                            reset_at=self.reset_at, now=self.now + 1)
        view = self.ledger.view("fixture-account")
        self.assertEqual(1000, view["cap"])
        self.assertIsNone(view["used"])
        self.assertEqual(1000, view["available"])

    def test_late_previous_cycle_snapshot_cannot_rewind_or_reset_current_usage(self):
        old_reset = self.now - 1
        self.ledger.observe("fixture-account", cap=1000, remaining=37,
                            reset_at=old_reset, now=self.now - 10)
        self.ledger.observe("fixture-account", cap=1000, remaining=1000,
                            reset_at=self.reset_at, now=self.now)
        self.assertTrue(self.ledger.reserve("fixture-account", "fixture-generation"))
        self.ledger.submit("fixture-generation")
        self.ledger.finish("fixture-generation", success=True)
        current = self.ledger.view("fixture-account")
        self.assertEqual(1, current["used"])

        self.ledger.observe("fixture-account", cap=1000, remaining=37,
                            reset_at=old_reset, now=self.now + 1)
        stale_applied = self.ledger.view("fixture-account")
        self.assertEqual(self.reset_at, stale_applied["reset_at"])
        self.assertEqual(current["cycle"], stale_applied["cycle"])
        self.assertEqual(999, stale_applied["available"])

        self.ledger.observe("fixture-account", cap=1000, remaining=999,
                            reset_at=self.reset_at, now=self.now + 2)
        refreshed = self.ledger.view("fixture-account")
        self.assertEqual(current["cycle"], refreshed["cycle"])
        self.assertEqual(1, refreshed["used"])
        self.assertEqual(999, refreshed["available"])

    def test_later_authoritative_idle_snapshot_reconciles_conservative_inflight_debit(self):
        self.ledger.observe("fixture-account", cap=1000, remaining=10,
                            reset_at=self.reset_at, now=self.now)
        self.assertTrue(self.ledger.reserve("fixture-account", "fixture-generation"))
        self.ledger.submit("fixture-generation")
        # 上游已经计入本次生成；此时允许在途状态继续保守占用。
        self.ledger.observe("fixture-account", cap=1000, remaining=9,
                            reset_at=self.reset_at, now=self.now + 1)
        self.ledger.finish("fixture-generation", success=True)
        self.assertEqual(0, self.ledger.view("fixture-account")["uncertain"])
        self.ledger.observe("fixture-account", cap=1000, remaining=9,
                            reset_at=self.reset_at, now=self.now + 2)
        self.assertEqual(9, self.ledger.view("fixture-account")["available"])


class ImageQuotaAccountRegressionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("services.account_service.browser_sentinel_enabled", return_value=False))
        self.enterContext(patch.object(type(config), "image_account_concurrency", new_callable=PropertyMock, return_value=3))
        self.enterContext(patch.object(type(config), "auto_remove_rate_limited_accounts", new_callable=PropertyMock, return_value=False))

    def make_account_service(self, quota=10):
        service, storage = make_service([{
            "access_token": "fixture-account", "type": "self_serve_business_prolite", "quota": quota,
        }])
        self.addCleanup(service._image_ledger.close)
        if quota > 0:
            seed_known_local_balance(service, 'fixture-account', quota)
        return service, storage

    def start_generation(self, service, generation):
        token = service._acquire_next_candidate_token(generation_id=generation, wait_timeout_secs=0)
        service.assert_image_submission_allowed(token, generation)
        return token

    def test_account_listing_before_first_remote_refresh_does_not_freeze_zero(self):
        service, _ = self.make_account_service(quota=0)
        self.assertEqual(0, service.list_accounts()[0]["image_quota_available"])
        service.update_account("fixture-account", {
            "quota": 1000, "status": "正常",
            "limits_progress": [{"feature_name": "image_gen", "remaining": 1000,
                                 "reset_after": time.time() + 3600}],
        }, quiet=True, remote=True)
        self.assertEqual(1000, service.get_account("fixture-account")["image_quota_available"])
        selected = service._acquire_next_candidate_token(generation_id="fixture-first", wait_timeout_secs=0)
        service.release_image_slot(selected, generation_id="fixture-first")

    def test_resume_success_keeps_other_reservations_out_of_raw_balance(self):
        service, _ = self.make_account_service(quota=3)
        token = self.start_generation(service, "fixture-first")
        self.start_generation(service, "fixture-second")
        service.bind_image_generation("fixture-first", "fixture-conversation")
        service.mark_image_result(token, False, generation_id="fixture-first")
        self.assertTrue(service.complete_image_generation_from_resume(token, "fixture-conversation"))
        service.mark_image_result(token, True, generation_id="fixture-second")
        account = service.get_account(token)
        self.assertEqual("正常", account["status"])
        self.assertEqual(1, account["image_quota_available"])
        selected = service._acquire_next_candidate_token(generation_id="fixture-third", wait_timeout_secs=0)
        service.release_image_slot(selected, generation_id="fixture-third")

    def test_duplicate_success_does_not_release_other_generation_or_increment_success(self):
        service, _ = self.make_account_service(quota=10)
        token = self.start_generation(service, "fixture-first")
        self.start_generation(service, "fixture-second")
        service.mark_image_result(token, True, generation_id="fixture-first")
        first = service.list_accounts()[0]
        self.assertEqual(1, first["image_inflight"])
        self.assertEqual(1, first["success"])
        service.mark_image_result(token, True, generation_id="fixture-first")
        repeated = service.list_accounts()[0]
        self.assertEqual(first["image_inflight"], repeated["image_inflight"])
        self.assertEqual(first["success"], repeated["success"])
        self.assertEqual(first["image_quota_available"], repeated["image_quota_available"])
        service.mark_image_result(token, False, generation_id="fixture-second", not_consumed=True)

    def test_failed_probe_reservation_does_not_leave_permanent_probing_state(self):
        for outcome in (False, RuntimeError("fixture reservation storage failure")):
            with self.subTest(outcome=type(outcome).__name__):
                service, _ = self.make_account_service()
                service.get_account("fixture-account")
                now = time.time()
                with service._lock:
                    service._accounts["fixture-account"] = service._normalize_account({
                        **service._accounts["fixture-account"],
                        "image_limit": {
                            "state": "probe_ready", "event_id": "1" * 64,
                            "reason": "图片生成已达到上限", "source": "assistant_final",
                            "conversation_id": "fixture-limited", "message_id": "fixture-limit",
                            "occurred_at": now - 7200, "observed_at": now - 7200,
                            "restore_at": now - 3600, "next_check_at": 0, "reported_quota": 10,
                        },
                    })
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch.object(service._image_ledger, "reserve", **kwargs):
                    with self.assertRaises(RuntimeError):
                        service._acquire_next_candidate_token(generation_id="fixture-probe", wait_timeout_secs=0)
                listed = service.list_accounts()[0]
                self.assertNotEqual("probing", listed["image_limit_state"])
                self.assertEqual(0, listed["image_inflight"])
                self.assertFalse(service._owns_image_probe("fixture-account"))


class InitialQuotaManagementRegressionTests(unittest.TestCase):
    def test_editing_metadata_does_not_freeze_uninitialized_balance(self):
        for update in ({"image_priority": 5}, {"proxy": ""}, {"status": "正常"}):
            with self.subTest(update=update):
                service, _ = make_service([{"access_token": "new-unobserved", "type": "self_serve_business_prolite"}])
                service.list_accounts()
                service.update_account("new-unobserved", update, quiet=True)
                row = service.update_account("new-unobserved", {"quota": 1000, "status": "正常",
                    "limits_progress": [{"feature_name": "image_gen", "remaining": 1000,
                                         "reset_after": time.time()+3600}]}, quiet=True, remote=True)
                self.assertEqual(1000, row["image_quota_available"])


if __name__ == "__main__":
    unittest.main()
