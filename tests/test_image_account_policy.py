"""账号优先级、额度预占和发送前拦截的行为约束。"""
import copy
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import PropertyMock, patch

from services.account_service import config
from services.image_quota import ImageQuotaEvidence, ImageQuotaExceeded
from test_sub2api_full_import import make_service
from image_quota_fixtures import seed_known_local_balance


def account(token, plan="edu", quota=20, **extra):
    return {"access_token": token, "type": plan, "quota": quota, "status": "正常", **extra}


def cooldown_evidence():
    now = time.time()
    return ImageQuotaEvidence(
        reason="图片生成已达到上限", source="assistant_final", conversation_id="fixture-conversation",
        message_id="fixture-limit", event_id="1" * 64, occurred_at=now,
        observed_at=now, restore_at=now + 3600,
    )


class ImageAccountPolicyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("services.account_service.browser_sentinel_enabled", return_value=False))
        self.enterContext(patch.object(type(config), "image_account_concurrency", new_callable=PropertyMock, return_value=3))
        self.enterContext(patch.object(type(config), "auto_remove_rate_limited_accounts", new_callable=PropertyMock, return_value=False))

    def test_defaults_expand_legacy_budget_and_preserve_deductions(self):
        service, _ = make_service([
            account("fixture-prolite", "self_serve_business_prolite", quota=37),
            account("fixture-edu", quota=57),
        ])
        prolite = service.get_account("fixture-prolite")
        edu = service.get_account("fixture-edu")
        self.assertEqual(0, prolite.get("image_priority"))
        self.assertEqual("auto", prolite.get("image_priority_mode"))
        self.assertEqual(1000, prolite.get("image_quota_cap"))
        self.assertEqual(917, prolite["quota"])
        self.assertEqual(917, prolite.get("image_quota_available"))
        self.assertEqual(1, edu.get("image_priority"))
        self.assertEqual("auto", edu.get("image_priority_mode"))
        self.assertIsNone(edu.get("image_quota_cap"))
        self.assertEqual(57, edu.get("image_quota_available"))

    def test_list_accounts_exposes_same_policy_as_get_account(self):
        service, _ = make_service([account("fixture-prolite", "self_serve_business_prolite", quota=7)])
        listed = service.list_accounts()[0]
        self.assertEqual(0, listed.get("image_priority"))
        self.assertEqual("auto", listed.get("image_priority_mode"))
        self.assertEqual(1000, listed.get("image_quota_cap"))
        self.assertEqual(887, listed.get("image_quota_available"))

    def test_manual_zero_is_valid_and_survives_storage_reload(self):
        service, storage = make_service([account("fixture-edu")])
        updated = service.update_account("fixture-edu", {"image_priority": 0}, quiet=True)
        self.assertEqual(0, updated.get("image_priority"))
        self.assertEqual("manual", updated.get("image_priority_mode"))
        saved = copy.deepcopy(storage.save_accounts.call_args.args[0])
        restored, _ = make_service(saved)
        self.assertEqual(0, restored.get_account("fixture-edu").get("image_priority"))
        self.assertEqual("manual", restored.get_account("fixture-edu").get("image_priority_mode"))

    def test_manual_priority_survives_remote_plan_change(self):
        service, _ = make_service([account("fixture-prolite", "self_serve_business_prolite")])
        service.update_account("fixture-prolite", {"image_priority": 9}, quiet=True)
        refreshed = service.update_account("fixture-prolite", {
            "type": "edu", "image_priority": 0, "image_priority_mode": "auto",
        }, quiet=True, remote=True)
        self.assertEqual(9, refreshed.get("image_priority"))
        self.assertEqual("manual", refreshed.get("image_priority_mode"))

    def test_remote_refresh_cannot_replace_management_policy(self):
        service, _ = make_service([account("fixture-prolite", "self_serve_business_prolite")])
        service.update_account("fixture-prolite", {"image_priority": 7}, quiet=True)
        refreshed = service.update_account("fixture-prolite", {
            "image_priority": 1, "image_priority_mode": "auto", "image_quota_cap": 999999,
        }, quiet=True, remote=True)
        self.assertEqual(7, refreshed.get("image_priority"))
        self.assertEqual("manual", refreshed.get("image_priority_mode"))
        self.assertEqual(1000, refreshed.get("image_quota_cap"))

    def test_switching_back_to_auto_recomputes_plan_default(self):
        service, _ = make_service([account("fixture-prolite", "self_serve_business_prolite")])
        service.update_account("fixture-prolite", {"image_priority": 7}, quiet=True)
        updated = service.update_account("fixture-prolite", {"image_priority_mode": "auto"}, quiet=True)
        self.assertEqual(0, updated.get("image_priority"))
        self.assertEqual("auto", updated.get("image_priority_mode"))

    def test_priority_rejects_invalid_type_and_range_without_changing_account(self):
        invalid = (True, False, -1, 1000001, 1.5, "3", None, [])
        for value in invalid:
            with self.subTest(value=value):
                service, _ = make_service([account("fixture-edu")])
                before = service.get_account("fixture-edu")
                with self.assertRaises(ValueError):
                    service.update_account("fixture-edu", {"image_priority": value}, quiet=True)
                self.assertEqual(before, service.get_account("fixture-edu"))

    def test_priority_mode_rejects_invalid_values(self):
        for value in ("automatic", "", 0, True, None):
            with self.subTest(value=value):
                service, _ = make_service([account("fixture-edu")])
                with self.assertRaises(ValueError):
                    service.update_account("fixture-edu", {"image_priority_mode": value}, quiet=True)

    def test_available_prolite_is_selected_before_earlier_edu(self):
        service, _ = make_service([
            account("fixture-edu"), account("fixture-prolite", "self_serve_business_prolite"),
        ])
        for _ in range(6):
            selected = service._acquire_next_candidate_token()
            service.release_image_slot(selected)
            self.assertEqual("fixture-prolite", selected)

    def test_manual_priority_can_prefer_edu(self):
        service, _ = make_service([
            account("fixture-prolite", "self_serve_business_prolite", image_priority=5, image_priority_mode="manual"),
            account("fixture-edu", image_priority=0, image_priority_mode="manual"),
        ])
        selected = service._acquire_next_candidate_token()
        service.release_image_slot(selected)
        self.assertEqual("fixture-edu", selected)

    def test_same_priority_accounts_receive_fair_turns(self):
        rows = [account(f"fixture-peer-{index}", image_priority=4, image_priority_mode="manual") for index in range(3)]
        service, _ = make_service(rows)
        counts = Counter()
        for _ in range(12):
            selected = service._acquire_next_candidate_token()
            counts[selected] += 1
            service.release_image_slot(selected)
        self.assertEqual({row["access_token"] for row in rows}, set(counts))
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

    def test_busy_prolite_falls_through_to_edu_without_waiting(self):
        service, _ = make_service([
            account("fixture-prolite", "self_serve_business_prolite"), account("fixture-edu"),
        ])
        with service._lock:
            service._image_inflight["fixture-prolite"] = 3
        with patch.object(service._image_slot_condition, "wait", side_effect=AssertionError("存在空闲Edu时不应等待")):
            selected = service._acquire_next_candidate_token()
        service.release_image_slot(selected)
        self.assertEqual("fixture-edu", selected)

    def test_concurrent_generation_cannot_reserve_last_quota_twice(self):
        service, _ = make_service([
            account("fixture-prolite", "self_serve_business_prolite", quota=1),
            *[account(f"fixture-edu-{index}") for index in range(3)],
        ])
        # 保持已取槽不释放，让八个选号者真实竞争最后一份ProLite额度。
        seed_known_local_balance(service, "fixture-prolite", 1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(service._acquire_next_candidate_token) for _ in range(8)]
            selected = [future.result(timeout=3) for future in futures]
        try:
            self.assertEqual(1, selected.count("fixture-prolite"))
            self.assertEqual(0, service.get_account("fixture-prolite").get("image_quota_available"))
        finally:
            for selected_token in selected:
                service.release_image_slot(selected_token)

    def test_releasing_unsent_generation_restores_reserved_quota(self):
        service, _ = make_service([account("fixture-prolite", "self_serve_business_prolite", quota=1)])
        seed_known_local_balance(service, "fixture-prolite", 1)
        selected = service._acquire_next_candidate_token()
        self.assertEqual(0, service.get_account(selected).get("image_quota_available"))
        service.release_image_slot(selected)
        self.assertEqual(1, service.get_account(selected).get("image_quota_available"))

    def test_cooldown_account_is_not_reselected_before_restore_time(self):
        service, _ = make_service([
            account("fixture-prolite", "self_serve_business_prolite"), account("fixture-edu"),
        ])
        service.mark_image_quota_limit("fixture-prolite", cooldown_evidence())
        selected = service._acquire_next_candidate_token()
        service.release_image_slot(selected)
        self.assertEqual("fixture-edu", selected)
        self.assertEqual(0, service.get_account("fixture-prolite").get("image_quota_available"))

    def test_submission_gate_rechecks_limit_after_account_was_acquired(self):
        service, _ = make_service([account("fixture-prolite", "self_serve_business_prolite")])
        selected = service._acquire_next_candidate_token()
        service.mark_image_quota_limit(selected, cooldown_evidence())
        gate = getattr(service, "assert_image_submission_allowed", None)
        self.assertTrue(callable(gate), "必须在实际发送前再次核对账号限流状态")
        try:
            with self.assertRaises(ImageQuotaExceeded):
                gate(selected)
        finally:
            service.release_image_slot(selected)

    def test_submission_gate_rejects_account_disabled_after_acquisition(self):
        service, _ = make_service([account("fixture-edu")])
        selected = service._acquire_next_candidate_token()
        service.update_account(selected, {"status": "禁用"}, quiet=True)
        gate = getattr(service, "assert_image_submission_allowed", None)
        self.assertTrue(callable(gate), "必须在实际发送前再次核对账号禁用状态")
        try:
            with self.assertRaisesRegex((RuntimeError, ValueError), "account|禁用|disabled|unavailable"):
                gate(selected)
        finally:
            service.release_image_slot(selected)


if __name__ == "__main__":
    unittest.main()
