from __future__ import annotations

import copy
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime
import inspect
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "isolated-test-key")

from services import image_task_service as tasks
from services.protocol.conversation import ImageGenerationError


ACCOUNT_REF = "b" * 64
CONVERSATION_ID = "original-conversation"
CONTEXT = {
    "submitted_message_id": "original-user-message",
    "input_asset_ids": [],
    "input_image_hashes": [],
    "observed_evidence": {
        "version": 1,
        "conversation_id": CONVERSATION_ID,
        "tool_ids": ["original-tool"],
        "tool_calls": {"original-call": "image_gen"},
        "asset_ids_by_message": {"original-tool": ["original-asset"]},
        "failed_message_ids": [],
    },
}


class ControlledWorkPool:
    """显式执行排队任务，模拟真实 Future，避免提交时递归执行下一轮。"""

    def __init__(self):
        self.jobs = []

    def submit(self, function, *args, **kwargs):
        future = Future()
        self.jobs.append((future, function, args, kwargs))
        return future

    def run(self, index=0):
        future, function, args, kwargs = self.jobs[index]
        if future.set_running_or_notify_cancel():
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
                raise
            else:
                future.set_result(result)


class ImageAutoRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "tasks.json"
        self.now = datetime(2026, 11, 6, 12, 0).timestamp()
        self.identity = {"id": "owner"}
        self.pool = ControlledWorkPool()
        for target, value in (
            ("image_work_pool", self.pool),
            ("_now_iso", lambda: datetime.fromtimestamp(self.now).strftime("%Y-%m-%d %H:%M:%S")),
        ):
            patcher = patch.object(tasks, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        clock = patch.object(tasks.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.service = self.make_service()

    def make_service(self):
        service = tasks.ImageTaskService(self.path, retention_days_getter=lambda: 30)
        service._log_call = Mock()
        service.generation_handler = Mock(side_effect=AssertionError("不能重放生成请求"))
        return service

    def require_method(self, name, service=None):
        method = getattr(service or self.service, name, None)
        self.assertTrue(callable(method), "缺少自动续查接口 " + name)
        return method

    def event(self, phase, reason="completion_missing"):
        return {
            "phase": phase, "index": 1, "total": 1,
            "conversation_id": CONVERSATION_ID,
            "resume_account_ref": ACCOUNT_REF,
            "delivery_context": copy.deepcopy(CONTEXT),
            "reason": reason,
        }

    def create_pending(self, task_id="job", reason="completion_missing"):
        callback = self.service.make_recovery_checkpoint(
            self.identity, task_id=task_id, mode="generate", model="gpt-image-2",
        )
        public_id = callback(self.event("pending"))
        callback(self.event("error", reason))
        return "owner:" + public_id, callback

    def release_origin(self, callback):
        try:
            callback(self.event("released"))
        except ValueError as exc:
            self.fail("checkpoint 必须接受原请求已结算的 released 事件: " + str(exc))

    def due(self, key):
        plan = self.service._tasks[key].get("auto_recovery")
        self.assertIsInstance(plan, dict, "可恢复错误必须建立自动续查计划")
        self.now = max(self.now, plan["next_poll_at"])
        return self.require_method("dispatch_due_recoveries")(now=self.now)

    @contextmanager
    def upstream(self, *, reason=None):
        account, backend = Mock(), Mock()
        account.acquire_image_resume_token.return_value = "synthetic-token"
        account.refresh_access_token.return_value = "synthetic-token"
        error = None
        if reason:
            error = ImageGenerationError(
                "原会话图片尚未确认", code="image_delivery_pending",
                conversation_id=CONVERSATION_ID,
                image_execution=[{"selection_reason": reason}],
                delivery_context=copy.deepcopy(CONTEXT),
            )
        with patch.object(tasks, "account_service", account), \
             patch("services.openai_backend_api.OpenAIBackendAPI", return_value=backend), \
             patch("services.protocol.conversation.ConversationRequest", side_effect=lambda **kw: SimpleNamespace(**kw)) as request, \
             patch("services.protocol.conversation.stream_image_outputs", return_value=iter(()), side_effect=error) as stream, \
             patch("services.protocol.conversation.collect_image_outputs", return_value={"data": [{"url": "https://example.test/final.png"}]}), \
             patch.object(tasks.time, "sleep", side_effect=AssertionError("自动续查轮间不能 sleep 占用工作槽位")):
            yield account, backend, request, stream

    def test_recoverable_checkpoint_persists_private_bounded_plan(self):
        key, _ = self.create_pending()
        task = self.service._tasks[key]
        plan = task.get("auto_recovery")
        self.assertIsInstance(plan, dict, "确认期结束后必须登记后台续查计划")
        self.assertEqual("queued", task["status"])
        self.assertEqual("confirming_image", task["progress"])
        self.assertEqual(1, plan["version"])
        self.assertEqual("waiting", plan["state"])
        self.assertEqual(0, plan["attempts"])
        self.assertEqual(self.now, plan["started_at"])
        self.assertEqual(self.now + 30 * 60, plan["deadline"])
        self.assertGreaterEqual(plan["next_poll_at"], self.now)
        self.assertEqual("completion_missing", plan["last_reason"])
        stored = json.loads(self.path.read_text(encoding="utf-8"))["tasks"][0]
        self.assertEqual(plan, stored["auto_recovery"])
        public = self.service.list_tasks(self.identity, [task["id"]])["items"][0]
        for private in ("auto_recovery", "resume_account_ref", "delivery_context"):
            self.assertNotIn(private, public)
        self.assertEqual([], self.pool.jobs)

    def test_origin_must_release_before_due_recovery_is_queued(self):
        self.require_method("dispatch_due_recoveries")
        key, callback = self.create_pending()
        self.assertEqual(0, self.due(key))
        self.assertEqual([], self.pool.jobs)
        self.release_origin(callback)
        self.assertEqual(1, self.due(key))
        self.assertEqual(1, len(self.pool.jobs))
        self.assertEqual(0, self.due(key))

    def test_two_future_limit_includes_queued_and_running_recoveries(self):
        dispatch = self.require_method("dispatch_due_recoveries")
        for index in range(4):
            key, callback = self.create_pending("cap-" + str(index))
            self.release_origin(callback)
        self.now = max(task["auto_recovery"]["next_poll_at"] for task in self.service._tasks.values())
        self.assertEqual(2, dispatch(now=self.now))
        self.assertEqual(0, dispatch(now=self.now))
        self.assertEqual(2, len(self.pool.jobs))
        with self.upstream():
            self.pool.run(0)
        self.assertEqual(1, dispatch(now=self.now))
        self.assertEqual(3, len(self.pool.jobs))

    def test_manual_resume_cannot_duplicate_an_automatic_future(self):
        self.require_method("dispatch_due_recoveries")
        key, callback = self.create_pending()
        self.release_origin(callback)
        self.assertEqual(1, self.due(key))
        try:
            self.service.resume_poll(self.identity, self.service._tasks[key]["id"])
        except ValueError:
            pass
        self.assertEqual(1, len(self.pool.jobs))
        self.assertEqual(0, self.due(key))

    def test_auto_worker_uses_original_identity_short_poll_and_one_release(self):
        self.require_method("dispatch_due_recoveries")
        signature = inspect.signature(self.service._run_resume_poll)
        self.assertIn("auto_recovery", signature.parameters)
        key, callback = self.create_pending()
        self.release_origin(callback)
        self.assertEqual(1, self.due(key))
        with self.upstream() as (account, backend, request, stream):
            self.pool.run()
        self.assertEqual("success", self.service._tasks[key]["status"])
        self.assertEqual("completed", self.service._tasks[key]["auto_recovery"]["state"])
        self.assertEqual(CONTEXT, request.call_args.kwargs["delivery_context"])
        self.assertEqual(CONVERSATION_ID, stream.call_args.kwargs["resume_conversation_id"])
        self.assertLessEqual(stream.call_args.kwargs["poll_timeout_secs"], 8)
        acquire = inspect.signature(type(tasks.account_service).acquire_image_resume_token).bind(
            None, *account.acquire_image_resume_token.call_args.args,
            **account.acquire_image_resume_token.call_args.kwargs,
        )
        acquire.apply_defaults()
        self.assertEqual(ACCOUNT_REF, acquire.arguments["account_ref"])
        self.assertEqual(CONVERSATION_ID, acquire.arguments["conversation_id"])
        self.assertLessEqual(acquire.arguments["wait_timeout_secs"], 2)
        account.release_image_slot.assert_called_once_with("synthetic-token")
        account.mark_image_result.assert_not_called()
        backend.stream_conversation.assert_not_called()
        self.service.generation_handler.assert_not_called()
        self.assertEqual(0, self.due(key))

    def test_retryable_round_returns_to_waiting_without_sleep_or_recursive_submit(self):
        self.require_method("dispatch_due_recoveries")
        key, callback = self.create_pending()
        self.release_origin(callback)
        self.assertEqual(1, self.due(key))
        with self.upstream(reason="document_in_progress") as (account, _, _, _):
            self.pool.run()
        task = self.service._tasks[key]
        self.assertEqual("waiting", task["auto_recovery"]["state"])
        self.assertEqual("queued", task["status"])
        self.assertEqual(1, task["auto_recovery"]["attempts"])
        self.assertGreater(task["auto_recovery"]["next_poll_at"], self.now)
        self.assertEqual(1, len(self.pool.jobs))
        account.release_image_slot.assert_called_once_with("synthetic-token")
        self.assertEqual(0, self.service.dispatch_due_recoveries(now=self.now))
        self.assertEqual(1, self.due(key))

    def test_explicit_failure_or_identity_conflict_stops_automatic_recovery(self):
        self.require_method("dispatch_due_recoveries")
        for index, reason in enumerate(("generation_failed", "last_tool_failed", "conversation_mismatch", "submission_mismatch")):
            with self.subTest(reason=reason):
                key, callback = self.create_pending("stop-" + str(index))
                self.release_origin(callback)
                self.assertEqual(1, self.due(key))
                with self.upstream(reason=reason):
                    self.pool.run(len(self.pool.jobs) - 1)
                task = self.service._tasks[key]
                self.assertEqual("error", task["status"])
                self.assertEqual("stopped", task["auto_recovery"]["state"])
                self.assertEqual(0, self.due(key))

    def test_deadline_and_attempt_limit_prevent_another_future(self):
        dispatch = self.require_method("dispatch_due_recoveries")
        for label, updates in (("attempts", {"attempts": 60}), ("deadline", {"deadline": self.now})):
            with self.subTest(limit=label):
                key, callback = self.create_pending(label)
                self.release_origin(callback)
                plan = self.service._tasks[key]["auto_recovery"]
                plan.update(updates, next_poll_at=self.now)
                self.assertEqual(0, dispatch(now=self.now))
                self.assertEqual("stopped", plan["state"])
                self.assertEqual("error", self.service._tasks[key]["status"])
        self.assertEqual([], self.pool.jobs)

    def test_restart_preserves_attempts_and_deadline_and_releases_old_lease(self):
        self.require_method("prepare_auto_recovery")
        key, callback = self.create_pending()
        self.release_origin(callback)
        plan = self.service._tasks[key]["auto_recovery"]
        plan.update(state="polling", attempts=7, next_poll_at=self.now - 1)
        self.service._save_locked()
        original_deadline = plan["deadline"]
        self.now += 20
        restored = self.make_service()
        self.require_method("prepare_auto_recovery", restored)(now=self.now)
        recovered = restored._tasks[key]["auto_recovery"]
        self.assertEqual(7, recovered["attempts"])
        self.assertEqual(original_deadline, recovered["deadline"])
        self.assertEqual("waiting", recovered["state"])
        self.assertEqual("queued", restored._tasks[key]["status"])
        self.now = max(self.now, recovered["next_poll_at"])
        self.assertEqual(1, restored.dispatch_due_recoveries(now=self.now))

    def legacy_record(self, task_id, **updates):
        stamp = datetime.fromtimestamp(self.now - 60).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "id": task_id, "owner_id": "owner", "status": "error", "mode": "generate",
            "model": "gpt-image-2", "image_transport": "conversation",
            "created_at": stamp, "updated_at": stamp,
            "created_ts": self.now - 60, "updated_ts": self.now - 60,
            "conversation_id": CONVERSATION_ID, "resume_account_ref": ACCOUNT_REF,
            "delivery_context": copy.deepcopy(CONTEXT),
            "error": "原会话图片仍待确认：completion_missing", **updates,
        }

    def test_legacy_migration_is_explicit_recent_and_requires_complete_identity(self):
        self.require_method("prepare_auto_recovery")
        records = [
            self.legacy_record("recovery-valid-1"),
            self.legacy_record("ordinary-error"),
            self.legacy_record("recovery-old-1", updated_ts=self.now - 86401,
                               updated_at=datetime.fromtimestamp(self.now - 86401).strftime("%Y-%m-%d %H:%M:%S")),
            self.legacy_record("recovery-no-account-1", resume_account_ref=""),
            self.legacy_record("recovery-no-observed-1", delivery_context={k: v for k, v in CONTEXT.items() if k != "observed_evidence"}),
            self.legacy_record("recovery-failed-1", error="原会话图片仍待确认：generation_failed"),
        ]
        self.path.write_text(json.dumps({"tasks": records}), encoding="utf-8")
        restored = self.make_service()
        self.assertTrue(all("auto_recovery" not in task for task in restored._tasks.values()))
        restored.prepare_auto_recovery(now=self.now)
        planned = {task["id"] for task in restored._tasks.values() if task.get("auto_recovery", {}).get("state") == "waiting"}
        self.assertEqual({"recovery-valid-1"}, planned)
        first = copy.deepcopy(restored._tasks["owner:recovery-valid-1"]["auto_recovery"])
        restored.prepare_auto_recovery(now=self.now + 1)
        self.assertEqual(first, restored._tasks["owner:recovery-valid-1"]["auto_recovery"])

    def test_legacy_migration_is_capped_at_two_hundred_tasks(self):
        self.require_method("prepare_auto_recovery")
        records = [self.legacy_record("recovery-old-" + str(index)) for index in range(205)]
        self.path.write_text(json.dumps({"tasks": records}), encoding="utf-8")
        restored = self.make_service()
        restored.prepare_auto_recovery(now=self.now)
        plans = [task for task in restored._tasks.values() if task.get("auto_recovery", {}).get("state") == "waiting"]
        self.assertEqual(200, len(plans))
        self.assertEqual([], self.pool.jobs)

    def test_scheduler_lifecycle_starts_and_stops_one_thread_idempotently(self):
        start = self.require_method("start_auto_recovery")
        stop = self.require_method("stop_auto_recovery")
        with patch.object(tasks.threading, "Thread") as thread:
            thread.return_value.is_alive.return_value = True
            start()
            start()
            self.assertEqual(1, thread.call_count)
            thread.return_value.start.assert_called_once()
            stop()
            stop()
        self.assertEqual([], self.pool.jobs)

    def test_completed_before_submit_returns_does_not_leak_automatic_capacity(self):
        for index in range(3):
            _, callback = self.create_pending("fast-" + str(index))
            self.release_origin(callback)
        self.now = max(task["auto_recovery"]["next_poll_at"] for task in self.service._tasks.values())
        original_submit = self.pool.submit

        def finish_before_return(function, *args, **kwargs):
            future = original_submit(function, *args, **kwargs)
            # 真实执行器允许工作在线程池 submit 返回前结束，回调登记必须容忍该顺序。
            worker = threading.Thread(target=self.pool.run, args=(len(self.pool.jobs) - 1,))
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(future.done())
            return future

        with self.upstream(), patch.object(self.pool, "submit", side_effect=finish_before_return):
            self.assertEqual(2, self.service.dispatch_due_recoveries(now=self.now))
            self.assertEqual(1, self.service.dispatch_due_recoveries(now=self.now))
        self.assertTrue(all(task["status"] == "success" for task in self.service._tasks.values()))

    def test_stop_joins_real_scheduler_without_waiting_for_cancelled_queue_entries(self):
        key, callback = self.create_pending()
        self.release_origin(callback)
        self.assertEqual(1, self.due(key))
        future = self.pool.jobs[0][0]
        self.service.start_auto_recovery()
        scheduler = self.service._recovery_thread
        stopped = threading.Event()

        def stop():
            try:
                self.service.stop_auto_recovery()
            finally:
                stopped.set()

        stopper = threading.Thread(target=stop)
        stopper.start()
        finished_promptly = stopped.wait(timeout=1)
        try:
            self.assertTrue(future.cancelled())
            self.assertFalse(scheduler.is_alive())
        finally:
            # 原实现会 wait 已取消但尚未由执行器确认的 Future；解锁它，避免测试真等45秒。
            if future.cancelled():
                future.set_running_or_notify_cancel()
            stopper.join(timeout=2)
        self.assertTrue(finished_promptly, "取消的队列项无需等待执行器取走，停服应立即完成")
        self.assertFalse(stopper.is_alive())
        self.assertEqual(0, self.service.dispatch_due_recoveries(now=self.now))

    def test_generation_exception_cannot_replace_checkpoint_submission_identity(self):
        self.service.submit_generation(self.identity, client_task_id="frozen", prompt="synthetic",
            model="gpt-image-2", size=None, image_transport="conversation")
        key = "owner:frozen"

        def handler(payload):
            callback = payload["_delivery_checkpoint_callback"]
            callback(self.event("pending"))
            callback(self.event("error"))
            changed = copy.deepcopy(CONTEXT)
            changed["submitted_message_id"] = "different-user-message"
            error = ImageGenerationError("synthetic late error", conversation_id=CONVERSATION_ID,
                delivery_context=changed, image_execution=[{"selection_reason": "completion_missing"}])
            error.resume_account_ref = ACCOUNT_REF
            raise error

        self.service.generation_handler = handler
        self.pool.run()
        self.assertEqual(CONTEXT, self.service._tasks[key]["delivery_context"])
        self.assertEqual("stopped", self.service._tasks[key]["auto_recovery"]["state"])

    def test_generation_exception_with_foreign_conversation_cannot_replace_original_evidence(self):
        for index, error_cid in enumerate(("foreign-conversation", CONVERSATION_ID)):
            with self.subTest(error_conversation_id=error_cid):
                task_id = "frozen-conversation-" + str(index)
                self.service.submit_generation(self.identity, client_task_id=task_id, prompt="synthetic",
                    model="gpt-image-2", size=None, image_transport="conversation")

                def handler(payload):
                    callback = payload["_delivery_checkpoint_callback"]
                    callback(self.event("pending"))
                    callback(self.event("error"))
                    changed = copy.deepcopy(CONTEXT)
                    changed["observed_evidence"]["conversation_id"] = "foreign-conversation"
                    error = ImageGenerationError("synthetic foreign conversation", conversation_id=error_cid,
                        delivery_context=changed, image_execution=[{"selection_reason": "completion_missing"}])
                    error.resume_account_ref = ACCOUNT_REF
                    raise error

                self.service.generation_handler = handler
                self.pool.run(len(self.pool.jobs) - 1)
                task = self.service._tasks["owner:" + task_id]
                self.assertEqual(CONVERSATION_ID, task["conversation_id"])
                self.assertEqual(ACCOUNT_REF, task["resume_account_ref"])
                self.assertEqual(CONTEXT, task["delivery_context"])
                self.assertEqual("stopped", task["auto_recovery"]["state"])

    def test_generation_exception_with_foreign_account_cannot_replace_original_evidence(self):
        self.service.submit_generation(self.identity, client_task_id="frozen-account", prompt="synthetic",
            model="gpt-image-2", size=None, image_transport="conversation")

        def handler(payload):
            callback = payload["_delivery_checkpoint_callback"]
            callback(self.event("pending"))
            callback(self.event("error"))
            changed = copy.deepcopy(CONTEXT)
            changed["observed_evidence"]["asset_ids_by_message"]["original-tool"] = ["foreign-account-asset"]
            error = ImageGenerationError("synthetic foreign account", conversation_id=CONVERSATION_ID,
                delivery_context=changed, image_execution=[{"selection_reason": "completion_missing"}])
            error.resume_account_ref = "c" * 64
            raise error

        self.service.generation_handler = handler
        self.pool.run()
        task = self.service._tasks["owner:frozen-account"]
        self.assertEqual(CONVERSATION_ID, task["conversation_id"])
        self.assertEqual(ACCOUNT_REF, task["resume_account_ref"])
        self.assertEqual(CONTEXT, task["delivery_context"])
        self.assertEqual("stopped", task["auto_recovery"]["state"])

    def test_late_origin_error_cannot_overwrite_checkpoint_success(self):
        self.service.submit_generation(self.identity, client_task_id="completed", prompt="synthetic",
            model="gpt-image-2", size=None, image_transport="conversation")
        key = "owner:completed"
        result = [{"url": "https://example.test/final.png"}]

        def handler(payload):
            callback = payload["_delivery_checkpoint_callback"]
            callback(self.event("pending"))
            callback(self.event("error"))
            callback({**self.event("success"), "data": result})
            callback(self.event("error"))
            raise RuntimeError("synthetic post-delivery cleanup failure")

        self.service.generation_handler = handler
        self.pool.run()
        self.assertEqual("success", self.service._tasks[key]["status"])
        self.assertEqual(result, self.service._tasks[key]["data"])
        self.assertEqual("completed", self.service._tasks[key]["auto_recovery"]["state"])

    def test_stale_first_snapshot_does_not_poison_later_matching_snapshot(self):
        from services.protocol import conversation as conversation
        from services.protocol.image_delivery import observed_evidence
        from tests.test_image_final_contract import CID, SUBMITTED, ImageFinalContractTests, revisions, png, file_id

        fixture = ImageFinalContractTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        rows = revisions(1)
        stale = copy.deepcopy(rows)
        stale[2]["content"]["parts"][0]["asset_pointer"] = "sediment://" + file_id(2)
        expected = png("blue")
        backend = fixture.backend(rows, image_bytes={file_id(1): expected, file_id(2): png("red")})
        snapshots = [{"conversation_id": CID, "messages": stale}, {"conversation_id": CID, "messages": rows}]
        backend._get_conversation.side_effect = lambda *_a, **_kw: copy.deepcopy(snapshots.pop(0) if len(snapshots) > 1 else snapshots[0])
        request = conversation.ConversationRequest(model="gpt-image-2", prompt="synthetic", image_transport="conversation",
            response_format="b64_json", delivery_context={"submitted_message_id": SUBMITTED,
                "input_asset_ids": [], "input_image_hashes": [], "observed_evidence": observed_evidence(rows, CID)})
        with patch.object(conversation.time, "sleep"):
            try:
                result = conversation.collect_image_outputs(conversation.stream_image_outputs(
                    backend, request, resume_conversation_id=CID, poll_timeout_secs=8))
            except ImageGenerationError as exc:
                self.fail("被拒绝的首个快照不应污染后续正确快照: " + str(exc.image_execution))
        fixture.assert_only_image(result, expected)
        backend.stream_conversation.assert_not_called()

    def test_malformed_persisted_plan_stops_without_breaking_service_startup(self):
        key, _ = self.create_pending()
        valid = copy.deepcopy(self.service._tasks[key]["auto_recovery"])
        for invalid_state in ([], {}, 1, None):
            with self.subTest(state=invalid_state):
                record = self.legacy_record("recovery-invalid-state-1",
                    auto_recovery={**valid, "state": invalid_state})
                self.path.write_text(json.dumps({"tasks": [record]}), encoding="utf-8")
                try:
                    restored = self.make_service()
                except (TypeError, ValueError) as exc:
                    self.fail("非法持久化计划应安全停止，不能阻断服务启动: " + type(exc).__name__)
                restored.prepare_auto_recovery(now=self.now)
                self.assertEqual("stopped", restored._tasks["owner:recovery-invalid-state-1"]["auto_recovery"]["state"])
                self.assertEqual(0, restored.dispatch_due_recoveries(now=self.now))


if __name__ == "__main__":
    unittest.main()
