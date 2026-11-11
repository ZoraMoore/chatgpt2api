"""恢复异常必须结束任务；未验证的读回快照不能变成持久化事实。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services import image_task_service as tasks
from services.image_quota import ImageQuotaExceeded
from services.protocol import conversation
from tests import test_delivery_timeout_recovery as recovery
from tests import test_image_final_contract as fixtures


class DeliveryRecoveryGuardsTests(unittest.TestCase):
    def setUp(self):
        self.harness = recovery.DeliveryTimeoutRecoveryTests(methodName="runTest")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "tasks.json"
        self.service = tasks.ImageTaskService(self.path)
        self.service._log_call = Mock()
        self.identity = {"id": "review-owner"}
        self.callback = self.service.make_recovery_checkpoint(
            self.identity, task_id="review-call", mode="edit", model="gpt-image-2.6-sunburst",
            base_url="https://example.test",
        )

    def request(self):
        request = self.harness.request()
        request.delivery_checkpoint_callback = self.callback
        return request

    def run_generation_error(self, backend, request):
        accounts = Mock()
        accounts.get_available_access_token.return_value = "synthetic-original-token"
        accounts.get_account.return_value = {"email": "synthetic@example.test"}
        accounts.image_account_ref.return_value = "b" * 64
        backend._webimage_generation_sent = True
        backend.close = Mock()
        with patch.object(conversation, "account_service", accounts), \
             patch.object(conversation, "OpenAIBackendAPI", return_value=backend):
            with self.assertRaises(conversation.ImageGenerationError) as caught:
                conversation._generate_single_image_impl(request, 1, 1)
        accounts.get_available_access_token.assert_called_once()
        accounts.mark_image_result.assert_called_once()
        self.assertEqual(("synthetic-original-token", False), accounts.mark_image_result.call_args.args)
        self.assertRegex(accounts.mark_image_result.call_args.kwargs["generation_id"], r"^[a-f0-9]{32}$")
        self.assertIsInstance(accounts.mark_image_result.call_args.kwargs["not_consumed"], bool)
        backend.close.assert_called_once()
        self.harness.assert_no_generation(backend)
        return caught.exception

    def assert_checkpoint_failed(self):
        records = self.service.list_tasks(self.identity, [])['items']
        self.assertEqual(1, len(records))
        self.assertEqual("error", records[0]["status"], "恢复已退出，任务不能永久停留在 running")
        saved = json.loads(self.path.read_text(encoding="utf-8"))["tasks"][0]
        self.assertEqual("error", saved["status"])
        self.assertEqual("b" * 64, saved["resume_account_ref"])
        self.assertEqual(fixtures.CID, saved["conversation_id"])
        self.assertEqual(self.harness.context(), saved["delivery_context"])

    def test_quota_during_readback_finishes_checkpoint_and_settles_original_attempt_once(self):
        backend = self.harness.backend(
            fixtures.revisions(1)[:-1],
            stream_error=conversation.ImageGenerationTimeoutError("synthetic timeout"),
        )
        backend._get_conversation.side_effect = ImageQuotaExceeded({
            "conversation_id": fixtures.CID, "restore_at": 2000000000.0,
            "reason": "synthetic quota limit",
        })
        error = self.run_generation_error(backend, self.request())
        self.assertEqual("image_quota_exceeded", error.code)
        self.assert_checkpoint_failed()

    def test_selector_error_after_checkpoint_does_not_leave_running_task(self):
        rows = fixtures.revisions(1)[:-1]
        backend = self.harness.backend(
            rows, stream_error=conversation.ImageGenerationTimeoutError("synthetic timeout"),
        )
        backend._get_conversation.return_value = {
            "conversation_id": fixtures.CID, "async_status": 3, "messages": rows,
        }
        select = conversation.select_terminal_image_delivery

        def fail_after_checkpoint(*args, **kwargs):
            if self.service.list_tasks(self.identity, [])['items']:
                raise RuntimeError("synthetic selector failure")
            return select(*args, **kwargs)

        with patch.object(conversation, "select_terminal_image_delivery", side_effect=fail_after_checkpoint):
            self.run_generation_error(backend, self.request())
        self.assert_checkpoint_failed()

    def test_success_checkpoint_failure_preserves_original_conversation_for_resume(self):
        rows = fixtures.revisions(1)
        backend = self.harness.backend(
            rows[:-1], stream_error=conversation.ImageGenerationTimeoutError("synthetic timeout"),
        )
        backend._get_conversation.return_value = {
            "conversation_id": fixtures.CID, "async_status": 4, "messages": rows,
        }
        request = self.request()

        def reject_success_checkpoint(event):
            if event["phase"] == "success":
                raise OSError("synthetic checkpoint write failure")
            return self.callback(event)

        request.delivery_checkpoint_callback = reject_success_checkpoint
        with patch.object(conversation, "_remove_image_conversation_later") as remove:
            self.run_generation_error(backend, request)
        remove.assert_not_called()
        saved = json.loads(self.path.read_text(encoding="utf-8"))["tasks"][0]
        self.assertEqual("error", saved["status"])
        self.assertEqual(fixtures.CID, saved["conversation_id"])
        self.assertEqual(self.harness.context()["observed_evidence"], saved["delivery_context"]["observed_evidence"])

    def test_invalid_input_binding_in_first_resume_snapshot_cannot_poison_saved_evidence(self):
        original = self.harness.context()
        task_id = self.callback({
            "phase": "pending", "index": 1, "total": 1, "conversation_id": fixtures.CID,
            "resume_account_ref": "b" * 64, "delivery_context": original,
        })
        key = "review-owner:" + task_id
        self.service._update_task(key, status="error")
        self.callback({"phase": "released", "index": 1, "total": 1, "conversation_id": fixtures.CID,
                       "resume_account_ref": "b" * 64, "delivery_context": original})

        # 额外工具仅存在于输入绑定无效的读回中，不能将其提升为必须存在的历史证据。
        invalid_rows = fixtures.revisions(2)
        invalid_rows[0]["metadata"]["attachments"] = {"invalid": "shape"}
        invalid = {"conversation_id": fixtures.CID, "async_status": 4, "messages": invalid_rows}
        valid = {"conversation_id": fixtures.CID, "async_status": 4, "messages": fixtures.revisions(1)}
        backend = self.harness.backend(fixtures.revisions(1))
        del backend._image_delivery_deadline
        backend.close = Mock()
        reads = []

        def read_original(cid, *, timeout_secs):
            reads.append(cid)
            self.assertEqual(fixtures.CID, cid)
            self.harness.clock.now += min(0.05, timeout_secs)
            return copy.deepcopy(invalid if len(reads) == 1 else valid)

        backend._get_conversation.side_effect = read_original
        accounts = Mock()
        accounts.acquire_image_resume_token.return_value = "synthetic-original-token"
        accounts.refresh_access_token.return_value = "synthetic-original-token"
        with patch.object(tasks, "account_service", accounts), \
             patch("services.openai_backend_api.OpenAIBackendAPI", return_value=backend):
            self.service._run_resume_poll(key, fixtures.CID, 10, self.identity, "edit", "gpt-image-2.6-sunburst")

        saved = json.loads(self.path.read_text(encoding="utf-8"))["tasks"][0]
        self.assertEqual(original["observed_evidence"], saved["delivery_context"]["observed_evidence"])
        self.assertNotIn("image-result-2", json.dumps(saved["delivery_context"]))
        accounts.release_image_slot.assert_called_once_with("synthetic-original-token", resume=True)
        accounts.mark_image_result.assert_not_called()
        backend.stream_conversation.assert_not_called()
        backend._post_image_conversation.assert_not_called()
        backend._start_image_generation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
