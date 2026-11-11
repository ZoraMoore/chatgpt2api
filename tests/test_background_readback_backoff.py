"""后台续查只重读原会话，临时读取限流按上游时刻退避。"""
from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from services import image_task_service as tasks
from services.image_quota import ImageQuotaExceeded
from services.protocol.conversation import ImageGenerationError, image_quota_error
from tests import test_image_auto_recovery as existing
from utils.helper import UpstreamHTTPError


SECRET = "sensitive-token-and-response-do-not-persist"


class BackgroundReadbackBackoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.ImageAutoRecoveryTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service

    def queued_round(self):
        key, callback = self.fixture.create_pending()
        self.fixture.release_origin(callback)
        self.assertEqual(1, self.fixture.due(key))
        return key, self.service._tasks[key]

    def quota_error(self, *, source="upstream_http_conversation_throttle", restore_at=None):
        return ImageQuotaExceeded({
            "source": source, "reason": SECRET,
            # 账号级读取冷却可能来自该账号的另一会话；续查仍保持本任务原CID。
            "conversation_id": "other-account-conversation",
            "restore_at": restore_at, "next_check_at": self.fixture.now + 90,
            "access_token": SECRET, "url": "https://" + SECRET + ".invalid",
        })

    def fail_round(self, exc, *, acquire=False):
        with self.fixture.upstream() as (account, backend, request, stream), \
             patch.object(tasks.logger, "info") as logger, \
             patch.object(tasks.log_service, "add") as delivery:
            if acquire:
                account.acquire_image_resume_token.side_effect = exc
            else:
                stream.side_effect = exc
            self.fixture.pool.run(len(self.fixture.pool.jobs) - 1)
        return account, backend, request, stream, logger, delivery

    def test_account_readback_cooldown_waits_until_restore_without_replacing_original_identity(self):
        key, task = self.queued_round()
        deadline = task["auto_recovery"]["deadline"]
        retry_at = self.fixture.now + 120
        account, backend, _, stream, _, _ = self.fail_round(
            self.quota_error(restore_at=retry_at), acquire=True)
        self.assertEqual("queued", task["status"])
        self.assertEqual("waiting", task["auto_recovery"]["state"])
        self.assertEqual("readback_throttled", task["auto_recovery"]["last_reason"])
        self.assertEqual(retry_at, task["auto_recovery"]["next_poll_at"])
        self.assertEqual(deadline, task["auto_recovery"]["deadline"])
        self.assertEqual(existing.CONVERSATION_ID, task["conversation_id"])
        self.assertEqual(existing.ACCOUNT_REF, task["resume_account_ref"])
        self.assertEqual(existing.CONTEXT, task["delivery_context"])
        self.assertEqual(0, self.service.dispatch_due_recoveries(now=retry_at - 0.01))
        self.assertEqual(1, self.service.dispatch_due_recoveries(now=retry_at))
        self.assertEqual(1, account.acquire_image_resume_token.call_count)
        account.release_image_slot.assert_not_called()
        stream.assert_not_called()
        backend.stream_conversation.assert_not_called()
        self.service.generation_handler.assert_not_called()

    def test_http_readback_429_uses_absolute_retry_after_and_releases_resume_slot(self):
        _, task = self.queued_round()
        error = UpstreamHTTPError("/backend-api/conversation/" + existing.CONVERSATION_ID,
                                  429, {"token": SECRET}, retry_after="150")
        self.fixture.now += 10
        account, _, _, _, _, _ = self.fail_round(error)
        self.assertEqual("waiting", task["auto_recovery"]["state"])
        self.assertEqual(error.retry_at, task["auto_recovery"]["next_poll_at"])
        self.assertEqual(429, task["recovery_failure"]["status_code"])
        account.release_image_slot.assert_called_once_with("synthetic-token", resume=True)
        self.service.generation_handler.assert_not_called()

    def test_wrapped_readback_throttle_preserves_structured_restore_time(self):
        _, task = self.queued_round()
        retry_at = self.fixture.now + 100
        self.fail_round(image_quota_error(self.quota_error(restore_at=retry_at)))
        self.assertEqual("readback_throttled", task["auto_recovery"]["last_reason"])
        self.assertEqual(retry_at, task["auto_recovery"]["next_poll_at"])

    def test_unknown_readback_restore_time_uses_safe_fallback_backoff(self):
        _, task = self.queued_round()
        self.fail_round(self.quota_error())
        self.assertEqual("waiting", task["auto_recovery"]["state"])
        self.assertEqual(self.fixture.now + 90, task["auto_recovery"]["next_poll_at"])

    def test_readback_throttle_without_any_timing_uses_existing_round_backoff(self):
        _, task = self.queued_round()
        error = self.quota_error()
        error.record.pop("next_check_at")
        self.fail_round(error)
        self.assertEqual("waiting", task["auto_recovery"]["state"])
        self.assertEqual(self.fixture.now + 30, task["auto_recovery"]["next_poll_at"])

    def test_restart_does_not_schedule_before_persisted_retry_time(self):
        key, task = self.queued_round()
        retry_at = self.fixture.now + 150
        self.fail_round(self.quota_error(restore_at=retry_at))
        expected = copy.deepcopy(task["auto_recovery"])
        self.fixture.now += 10
        restored = self.fixture.make_service()
        restored.prepare_auto_recovery(now=self.fixture.now)
        recovered = restored._tasks[key]
        self.assertEqual(retry_at, recovered["auto_recovery"]["next_poll_at"])
        self.assertEqual(expected["attempts"], recovered["auto_recovery"]["attempts"])
        self.assertEqual(expected["deadline"], recovered["auto_recovery"]["deadline"])
        self.assertEqual(task["recovery_failure"], recovered["recovery_failure"])
        self.assertEqual(0, restored.dispatch_due_recoveries(now=retry_at - 0.01))

    def test_throttle_still_stops_at_original_deadline_and_attempt_limit(self):
        for limit in ("deadline", "attempts"):
            with self.subTest(limit=limit):
                fixture = self.fixture
                key, callback = fixture.create_pending("limit-" + limit)
                fixture.release_origin(callback)
                task = self.service._tasks[key]
                self.assertEqual(1, fixture.due(key))
                plan = task["auto_recovery"]
                original_deadline = plan["deadline"]
                if limit == "attempts":
                    plan["attempts"] = 60
                self.fail_round(self.quota_error(restore_at=original_deadline + 1000))
                if limit == "deadline":
                    self.assertEqual(original_deadline, plan["next_poll_at"])
                    self.assertEqual(0, self.service.dispatch_due_recoveries(now=original_deadline))
                self.assertEqual("stopped", plan["state"])
                self.assertEqual("error", task["status"])
                self.assertEqual(original_deadline, plan["deadline"])
                self.assertEqual("recovery_" + ("deadline_exceeded" if limit == "deadline" else "attempts_exhausted"),
                                 plan["last_reason"])

    def test_true_quota_and_policy_failures_remain_terminal(self):
        for source in ("assistant_final", "image_tool_system_error", "upstream_http_image_quota"):
            with self.subTest(source=source):
                key, callback = self.fixture.create_pending(source)
                self.fixture.release_origin(callback)
                self.assertEqual(1, self.fixture.due(key))
                self.fail_round(self.quota_error(source=source, restore_at=self.fixture.now + 100))
                task = self.service._tasks[key]
                self.assertEqual("stopped", task["auto_recovery"]["state"])
                self.assertEqual("image_quota_exceeded", task["auto_recovery"]["last_reason"])
        key, callback = self.fixture.create_pending("policy")
        self.fixture.release_origin(callback)
        self.assertEqual(1, self.fixture.due(key))
        self.fail_round(ImageGenerationError(SECRET, status_code=400, code="content_policy_violation"))
        self.assertEqual("stopped", self.service._tasks[key]["auto_recovery"]["state"])

    def test_deadline_transition_updates_timestamp_and_preserves_safe_failure_details(self):
        key, task = self.queued_round()
        _, _, _, _, logger, delivery = self.fail_round(self.quota_error(restore_at=self.fixture.now + 100))
        self.assertIsInstance(task.get("recovery_failure"), dict)
        failure = copy.deepcopy(task["recovery_failure"])
        deadline = task["auto_recovery"]["deadline"]
        self.fixture.now = deadline
        self.assertEqual(0, self.service.dispatch_due_recoveries(now=deadline))
        self.assertEqual(deadline, task["updated_ts"])
        self.assertEqual(datetime.fromtimestamp(deadline).strftime("%Y-%m-%d %H:%M:%S"), task["updated_at"])
        self.assertEqual(failure, task["recovery_failure"])
        stored = self.fixture.path.read_text(encoding="utf-8")
        recorded = json.dumps([call.args for call in logger.call_args_list] +
                              [call.args for call in delivery.call_args_list], default=str)
        self.assertNotIn(SECRET, stored + recorded)
        self.assertEqual("ImageQuotaExceeded", failure["exception_type"])
        self.assertEqual("upstream_http_conversation_throttle", failure["source"])
        self.assertEqual(429, failure["status_code"])
        self.assertNotIn("recovery_failure", tasks._public_task(task))
        self.assertEqual(failure, logger.call_args.args[0]["failure"])

    def test_terminal_exception_keeps_safe_diagnostics_without_persisting_exception_message(self):
        _, task = self.queued_round()
        _, _, _, _, logger, delivery = self.fail_round(RuntimeError(SECRET))
        self.assertEqual("stopped", task["auto_recovery"]["state"])
        self.assertIsInstance(task.get("recovery_failure"), dict)
        self.assertEqual("RuntimeError", task["recovery_failure"]["exception_type"])
        combined = self.fixture.path.read_text(encoding="utf-8") + repr(logger.call_args_list) + repr(delivery.call_args_list)
        self.assertNotIn(SECRET, combined)
        self.assertEqual(task["recovery_failure"], delivery.call_args.args[2]["failure"])

    def test_invalid_persisted_diagnostic_fields_cannot_reenter_task(self):
        key, task = self.queued_round()
        for invalid_source in (SECRET, [], {}, None):
            with self.subTest(source=invalid_source):
                task["recovery_failure"] = {"source": invalid_source, "exception_type": SECRET,
                                            "status_code": True, "retry_at": float("nan"), "token": SECRET}
                self.service._save_locked()
                try:
                    restored = self.fixture.make_service()
                except Exception as exc:
                    self.fail("无效诊断字段不能阻止恢复任务加载：" + type(exc).__name__)
                serialized = json.dumps(restored._tasks[key], ensure_ascii=False)
                self.assertNotIn(SECRET, serialized)
                self.assertNotIn("NaN", serialized)

    def test_invalid_quota_source_cannot_interrupt_safe_exception_handling(self):
        _, task = self.queued_round()
        error = ImageGenerationError(SECRET, status_code=429, code="image_request_rate_limited",
            image_execution=[{"image_quota_limit": {"source": [], "token": SECRET}}])
        try:
            self.fail_round(error)
        except Exception as exc:
            self.fail("无效上游诊断不能中断异常收尾：" + type(exc).__name__)
        self.assertEqual("stopped", task["auto_recovery"]["state"])
        self.assertEqual("resume_poll", task["recovery_failure"]["source"])

    def test_out_of_range_persisted_retry_time_does_not_prevent_task_loading(self):
        key, task = self.queued_round()
        for invalid_time in (10**400, -(10**400), 253402300800, float("inf"), float("nan"), True, -1):
            with self.subTest(time_type=type(invalid_time).__name__):
                task["recovery_failure"] = {"source": "resume_poll", "exception_type": "RuntimeError",
                                            "retry_at": invalid_time}
                self.service._save_locked()
                try:
                    restored = self.fixture.make_service()
                except Exception as exc:
                    self.fail("越界恢复时刻不能阻止任务加载：" + type(exc).__name__)
                self.assertEqual({"source": "resume_poll", "exception_type": "RuntimeError"},
                                 restored._tasks[key]["recovery_failure"])

    def test_out_of_range_retry_time_cannot_interrupt_or_extend_round_backoff(self):
        _, task = self.queued_round()
        for invalid_time in (10**400, -(10**400), 253402300800, float("inf"), float("nan"), True, -1):
            with self.subTest(time_type=type(invalid_time).__name__):
                task["auto_recovery"].update(state="polling", next_poll_at=self.fixture.now)
                try:
                    with self.service._lock:
                        self.service._set_recovery_error_locked(task, "readback_throttled", "原会话稍后重查",
                                                               now=self.fixture.now, retry_at=invalid_time)
                except Exception as exc:
                    self.fail("越界恢复时刻不能中断异常收尾：" + type(exc).__name__)
                self.assertEqual("waiting", task["auto_recovery"]["state"])
                self.assertEqual(self.fixture.now + 30, task["auto_recovery"]["next_poll_at"])

    def test_huge_upstream_timing_is_ignored_by_safe_exception_parsing(self):
        _, task = self.queued_round()
        error = ImageGenerationError(SECRET, status_code=429, code="image_request_rate_limited",
            image_execution=[{"image_quota_limit": {"source": "upstream_http_conversation_throttle",
                "restore_at": 10**400, "next_check_at": 10**400}}])
        error.restore_at = 10**400
        error.retry_after = 10**400
        for retry_at in (None, 10**400):
            error.retry_at = retry_at
            details = self.service._resume_failure_details(error)
            self.assertNotIn("retry_at", details)
        self.fail_round(error)
        self.assertEqual("waiting", task["auto_recovery"]["state"])
        self.assertEqual(self.fixture.now + 30, task["auto_recovery"]["next_poll_at"])


if __name__ == "__main__":
    unittest.main()
