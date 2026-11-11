"""直播提交身份只能由原会话 GET 核验；读回冲突不能借重试变成成功。"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.image_task_service import ImageTaskService
from services.protocol import conversation
from services.protocol.image_delivery import bind_input_assets, semantic_message
from tests import test_image_final_contract as fixtures
from tests.test_delivery_state_machine import FakeClock


class DeliveryIdentityRecheckTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ImageFinalContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(patch.object(conversation, "logger", Mock()))
        self.clock = FakeClock()
        self.enterContext(patch.object(conversation.time, "monotonic", self.clock.monotonic))
        self.enterContext(patch.object(conversation.time, "sleep", self.clock.sleep))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "tasks.json"
        self.service = ImageTaskService(self.path)
        self.service._log_call = Mock()
        self.persist = False
        self.checkpoints = []
        self.saved_checkpoint = self.service.make_recovery_checkpoint(
            {"id": "identity-owner"}, task_id="identity-recheck", mode="edit",
            model="gpt-image-2.6-sunburst", base_url="https://example.invalid",
        )
        self.expected = fixtures.png("blue")

    def backend(self, *, marker=False, done=True):
        original = fixtures.revisions(2)
        if marker:
            original[-1].update(status="in_progress", end_turn=False)
        live = copy.deepcopy(original)
        live[0]["id"] = "unconfirmed-live-user"
        backend = self.fixture.backend(live, image_bytes={
            fixtures.file_id(1): fixtures.png("green"), fixtures.file_id(2): self.expected,
        })
        backend._image_input_asset_ids = [fixtures.INPUT_ID]
        backend._image_resume_account_ref = "b" * 64
        backend._image_delivery_deadline = 104.0
        backend._post_image_conversation = Mock(side_effect=AssertionError("不得重放生成 POST"))
        backend._start_image_generation = Mock(side_effect=AssertionError("不得重启生成"))
        frames = list(backend.stream_conversation.return_value)[:-1]
        if marker:
            frames.append(json.dumps({"type": "message_stream_complete", "conversation_id": fixtures.CID}))
        if done:
            frames.append("[DONE]")
        backend.stream_conversation.return_value = iter(frames)
        return backend, {"conversation_id": fixtures.CID, "messages": original}, live

    def request(self):
        request = conversation.ConversationRequest(
            model="gpt-image-2.6-sunburst", prompt="synthetic image edit request",
            images=[base64.b64encode(fixtures.INPUT_BYTES).decode("ascii")], n=1,
            image_transport="conversation", response_format="b64_json",
        )

        def checkpoint(event):
            self.checkpoints.append(copy.deepcopy(event))
            if self.persist:
                return self.saved_checkpoint(event)

        request.delivery_checkpoint_callback = checkpoint
        return request

    def readback(self, backend, *documents):
        reads = []

        def get_original(cid, *, timeout_secs):
            self.assertEqual(fixtures.CID, cid)
            self.assertGreater(timeout_secs, 0)
            self.assertLessEqual(timeout_secs, min(3.0, 104.0 - self.clock.now) + 1e-9)
            reads.append((cid, bool(self.checkpoints)))
            if len(reads) > 4:
                self.clock.now = 105.0
                raise AssertionError("身份补证不得超过原读回预算")
            self.clock.now += min(0.1, timeout_secs)
            return copy.deepcopy(documents[min(len(reads) - 1, len(documents) - 1)])

        backend._get_conversation.side_effect = get_original
        return reads

    def run_result(self, backend):
        return conversation.collect_image_outputs(conversation.stream_image_outputs(backend, self.request()))

    def assert_no_replay(self, backend):
        backend.stream_conversation.assert_called_once()
        backend._post_image_conversation.assert_not_called()
        backend._start_image_generation.assert_not_called()

    def assert_original_checkpoint(self, reads):
        self.assertTrue(reads, "直播身份不一致时必须 GET 原会话核验")
        self.assertTrue(all(before_get for _, before_get in reads), "首次 GET 前必须保存原身份检查点")
        self.assertEqual("pending", self.checkpoints[0]["phase"])
        self.assertEqual("stream_submission_unconfirmed", self.checkpoints[0]["reason"])
        for event in self.checkpoints:
            self.assertEqual(fixtures.CID, event["conversation_id"])
            self.assertEqual("b" * 64, event["resume_account_ref"])
            self.assertEqual(fixtures.SUBMITTED, event["delivery_context"]["submitted_message_id"])
        self.assertLessEqual(self.clock.now, 104.0 + 1e-9)

    def assert_success(self, backend, reads):
        try:
            result = self.run_result(backend)
        except conversation.ImageGenerationError as error:
            reason = error.image_execution[0].get("selection_reason") if error.image_execution else ""
            self.fail(f"原会话已确认提交身份和唯一最终图，应成功；实际 {error.code}/{reason}，"
                      f"GET={backend._get_conversation.call_count}")
        self.fixture.assert_only_image(result, self.expected)
        self.assert_original_checkpoint(reads)
        self.assertEqual("success", self.checkpoints[-1]["phase"])
        self.assert_no_replay(backend)
        backend._request_image_transfer.assert_called_once()
        self.assertIn(fixtures.file_id(2), backend._request_image_transfer.call_args.args[1])
        return result

    def assert_rejected(self, backend, reads, reason=None, *, attempts=None):
        self.persist = True
        with self.assertRaises(conversation.ImageGenerationError) as caught:
            self.run_result(backend)
        error = caught.exception
        actual = error.image_execution[0]["selection_reason"]
        if reason is not None:
            self.assertEqual(reason, actual)
        self.assert_original_checkpoint(reads)
        if attempts is not None:
            self.assertEqual(attempts, backend._get_conversation.call_count)
        self.assertEqual("error", self.checkpoints[-1]["phase"])
        self.assertEqual(actual, self.checkpoints[-1]["reason"])
        saved = json.loads(self.path.read_text(encoding="utf-8"))["tasks"][0]
        recoverable = {
            "observed_tools_missing", "observed_calls_missing", "snapshot_asset_changed",
            "completion_missing", "snapshot_messages_missing",
        }
        if actual in recoverable:
            self.assertEqual("queued", saved["status"])
            self.assertEqual("confirming_image", saved["progress"])
            self.assertEqual("", saved["error"])
            self.assertEqual("waiting", saved["auto_recovery"]["state"])
            self.assertEqual(actual, saved["auto_recovery"]["last_reason"])
            self.assertEqual(0, saved["auto_recovery"]["attempts"])
        else:
            self.assertIn(actual, {"conversation_mismatch", "submission_mismatch"})
            self.assertEqual("error", saved["status"])
            self.assertTrue(saved["error"].endswith(":" + actual) or saved["error"].endswith("：" + actual))
            self.assertNotIn("auto_recovery", saved, "真实身份冲突不能登记自动续查")
        self.assertEqual([], saved["data"], "未通过证据核验不能交付候选图")
        self.assertEqual(fixtures.CID, saved["conversation_id"])
        self.assertEqual(fixtures.SUBMITTED, saved["delivery_context"]["submitted_message_id"])
        self.assertEqual("b" * 64, saved["resume_account_ref"])
        self.assertEqual(error.delivery_context, saved["delivery_context"])
        backend._request_image_transfer.assert_not_called()
        backend.download_final_sandbox_images.assert_not_called()
        self.assert_no_replay(backend)
        return error

    def test_wrong_live_user_get_verified_formal_final_delivers_once(self):
        backend, snapshot, _ = self.backend()
        reads = self.readback(backend, snapshot)
        self.assert_success(backend, reads)
        backend._get_conversation.assert_called_once()

    def test_wrong_live_user_get_verified_marker_final_keeps_original_status(self):
        backend, snapshot, _ = self.backend(marker=True)
        original = copy.deepcopy(snapshot)
        reads = self.readback(backend, snapshot)
        self.assert_success(backend, reads)
        self.assertEqual(original, snapshot, "核验不得修改上游 status 或 end_turn")
        backend._get_conversation.assert_called_once()

    def test_history_omits_empty_final_after_identity_check_reuses_exact_live_anchor(self):
        backend, snapshot, live = self.backend(marker=True)
        snapshot["messages"].pop()
        original = copy.deepcopy(snapshot)
        reads = self.readback(backend, snapshot)
        self.assert_success(backend, reads)
        receipt = self.checkpoints[-1]["delivery_context"]["completion_receipt"]
        expected_hash = hashlib.sha256(json.dumps(
            semantic_message(live[-1]), sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()
        self.assertEqual(live[-1]["id"], receipt["anchor_id"])
        self.assertEqual(expected_hash, receipt["anchor_sha256"], "必须复用原始空 final，不能伪造成功状态")
        self.assertEqual(original, snapshot)
        backend._get_conversation.assert_called_once()

    def test_foreign_readback_cid_is_hard_rejected_before_later_valid_snapshot(self):
        backend, valid, _ = self.backend()
        invalid = copy.deepcopy(valid)
        invalid["conversation_id"] = "foreign-conversation"
        reads = self.readback(backend, invalid, valid)
        self.assert_rejected(backend, reads, "conversation_mismatch", attempts=1)

    def test_other_readback_user_is_hard_rejected_before_later_valid_snapshot(self):
        backend, valid, _ = self.backend()
        invalid = copy.deepcopy(valid)
        invalid["messages"][0]["id"] = "different-submitted-user"
        reads = self.readback(backend, invalid, valid)
        self.assert_rejected(backend, reads, "submission_mismatch", attempts=1)

    def test_new_readback_user_cannot_select_previous_submission_image(self):
        backend, valid, _ = self.backend()
        invalid = copy.deepcopy(valid)
        invalid["messages"].append(fixtures.message("newer-user", "user"))
        reads = self.readback(backend, invalid, valid)
        self.assert_rejected(backend, reads, "submission_mismatch", attempts=1)

    def test_readback_missing_observed_tool_never_delivers_stale_image(self):
        backend, snapshot, _ = self.backend()
        snapshot["messages"] = [row for row in snapshot["messages"] if row["id"] != "image-result-2"]
        reads = self.readback(backend, snapshot)
        self.assert_rejected(backend, reads, "observed_tools_missing", attempts=4)

    def test_readback_missing_observed_call_never_delivers_without_work_identity(self):
        backend, snapshot, _ = self.backend()
        snapshot["messages"] = [row for row in snapshot["messages"] if row["id"] != "image-call-2"]
        reads = self.readback(backend, snapshot)
        self.assert_rejected(backend, reads, "observed_calls_missing", attempts=4)

    def test_readback_changed_asset_never_downloads_replacement(self):
        backend, snapshot, _ = self.backend()
        snapshot["messages"][-2]["content"]["parts"][0]["asset_pointer"] = "sediment://" + fixtures.file_id(3)
        reads = self.readback(backend, snapshot)
        self.assert_rejected(backend, reads, "snapshot_asset_changed", attempts=4)

    def test_stale_work_snapshot_can_settle_only_when_all_observed_assets_return(self):
        backend, valid, _ = self.backend()
        stale = copy.deepcopy(valid)
        stale["messages"][-2]["content"]["parts"][0]["asset_pointer"] = "sediment://" + fixtures.file_id(1)
        reads = self.readback(backend, stale, valid)
        self.assert_success(backend, reads)
        self.assertEqual(2, backend._get_conversation.call_count)

    def test_omitted_empty_final_cannot_cover_changed_work_digest(self):
        backend, snapshot, _ = self.backend(marker=True)
        snapshot["messages"].pop()
        snapshot["messages"][-2]["content"]["text"] = "synthetic_different_image_work()"
        reads = self.readback(backend, snapshot)
        self.assert_rejected(backend, reads, attempts=4)

    def test_omitted_final_without_done_cannot_borrow_live_completion(self):
        backend, snapshot, _ = self.backend(marker=True, done=False)
        snapshot["messages"].pop()
        reads = self.readback(backend, snapshot)
        self.assert_rejected(backend, reads, attempts=4)

    def test_binding_function_keeps_real_readback_identity_mismatch(self):
        backend, snapshot, _ = self.backend()
        snapshot["messages"][0]["id"] = "different-submitted-user"
        diagnostic = {}
        original = copy.deepcopy(snapshot)
        bound = bind_input_assets(snapshot, {
            "submitted_message_id": fixtures.SUBMITTED, "input_asset_ids": [fixtures.INPUT_ID],
            "input_image_hashes": [],
        }, diagnostic=diagnostic)
        self.assertEqual("submission_mismatch", diagnostic["reason"])
        self.assertEqual(original, bound)
        self.assertEqual(original, snapshot)
        backend.stream_conversation.assert_not_called()

    def test_missing_actual_submission_identity_never_uses_live_user_as_authority(self):
        backend, _, _ = self.backend()
        backend._image_submitted_message_id = ""
        with self.assertRaises(conversation.ImageGenerationError) as caught:
            self.run_result(backend)
        self.assertEqual("submission_identity_missing", caught.exception.image_execution[0]["selection_reason"])
        backend._get_conversation.assert_not_called()
        backend._request_image_transfer.assert_not_called()
        self.assertEqual([], self.checkpoints)
        self.assert_no_replay(backend)


if __name__ == "__main__":
    unittest.main()
