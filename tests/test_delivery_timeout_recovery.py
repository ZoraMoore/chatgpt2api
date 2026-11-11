"""生成流超时后的只读交付恢复；时间及传输全部隔离。"""
from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
import unittest
from unittest.mock import Mock, patch

from services.image_quota import ImageQuotaExceeded
from services.protocol import conversation
from tests import test_image_final_contract as fixtures
from tests import test_delivery_state_machine as state_machine


class RecoveryClock(state_machine.FakeClock):
    def sleep(self, seconds):
        if not isinstance(seconds, (int, float)) or not 0 < seconds <= 5.0:
            raise AssertionError("delivery recovery sleeps must be bounded by five seconds")
        self.sleeps.append(seconds)
        self.now += seconds


class DeliveryTimeoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        # 复用状态机已有的真实交付链和禁重放断言，只替换恢复阶段的虚拟时钟。
        self.harness = state_machine.DeliveryStateMachineTests(methodName="runTest")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.fixture = self.harness.fixture
        self.expected = self.harness.expected
        self.clock = RecoveryClock()
        self.enterContext(patch.object(conversation.time, "monotonic", self.clock.monotonic))
        self.enterContext(patch.object(conversation.time, "sleep", self.clock.sleep))
        self.checkpoints = []

    def backend(self, rows, *, stream_error=None):
        backend = self.harness.backend(rows)
        backend._image_input_asset_ids = [fixtures.INPUT_ID]
        # 传输的旧 deadline 已耗尽；交付恢复必须取得自己的有限预算。
        backend._image_delivery_deadline = self.clock.now - 1.0
        if stream_error is not None:
            frames = list(backend.stream_conversation.return_value)[:-1]

            def interrupted():
                yield from frames
                raise stream_error

            backend.stream_conversation.return_value = interrupted()
        return backend

    def request(self, *, context=None):
        request = conversation.ConversationRequest(
            model="gpt-image-2.6-sunburst", prompt="synthetic private prompt marker",
            images=[base64.b64encode(fixtures.INPUT_BYTES).decode("ascii")],
            image_transport="conversation", response_format="b64_json",
            delivery_context=copy.deepcopy(context or {}),
        )
        # 独立测试 dataclass 的公开签名；其余回归在旧代码上也能到达真实缺陷。
        request.delivery_checkpoint_callback = lambda event: self.checkpoints.append(copy.deepcopy(event))
        return request

    def context(self, *, count=1):
        return {
            "submitted_message_id": fixtures.SUBMITTED,
            "input_asset_ids": [fixtures.INPUT_ID],
            "input_image_hashes": [hashlib.sha256(fixtures.INPUT_BYTES).hexdigest()],
            "observed_evidence": {
                "version": 1,
                "conversation_id": fixtures.CID,
                "tool_ids": [f"image-result-{index}" for index in range(1, count + 1)],
                "tool_calls": {f"image-call-{index}": "synthetic.image" for index in range(1, count + 1)},
                "asset_ids_by_message": {
                    f"image-result-{index}": [fixtures.file_id(index)] for index in range(1, count + 1)
                },
                "failed_message_ids": [],
            },
        }

    def readback(self, backend, pending, *, complete=None, ready_after=None):
        started = self.clock.now
        reads = []

        def get_document(cid, *, timeout_secs):
            reads.append((self.clock.now, cid, timeout_secs, bool(self.checkpoints)))
            if len(reads) > 45:
                self.clock.now = started + 1000.0
                raise AssertionError("delivery recovery exceeded bounded read count")
            ready = complete is not None and self.clock.now - started >= ready_after
            self.clock.now += min(0.05, timeout_secs)
            return copy.deepcopy(complete if ready else pending)

        backend._get_conversation.side_effect = get_document
        return started, reads

    def run_result(self, backend, *, request=None, resume=False, timeout=None):
        kwargs = {"resume_conversation_id": fixtures.CID, "poll_timeout_secs": timeout} if resume else {}
        try:
            return conversation.collect_image_outputs(conversation.stream_image_outputs(
                backend, request or self.request(), **kwargs,
            ))
        except conversation.ImageGenerationTimeoutError:
            self.fail("generation stream timeout must enter bounded delivery recovery")

    def assert_reads_bounded(self, started, reads, budget, *, limit=40):
        self.assertGreater(len(reads), 0)
        self.assertLessEqual(len(reads), limit)
        self.assertLessEqual(self.clock.now, started + budget + 1e-9)
        for when, cid, timeout, _checkpoint in reads:
            self.assertEqual(fixtures.CID, cid)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, min(5.0, started + budget - when) + 1e-9)

    def assert_pending(self, error, *, source_timeout=False):
        self.assertEqual("image_delivery_pending", error.code)
        self.assertEqual(fixtures.CID, error.conversation_id)
        self.assertTrue(error.image_execution)
        self.assertTrue(error.image_execution[0].get("selection_reason"))
        if source_timeout:
            self.assertEqual("ImageGenerationTimeoutError", error.image_execution[0].get("source_error_type"))

    def assert_no_generation(self, backend, *, resume=False):
        if resume:
            backend.stream_conversation.assert_not_called()
            backend._post_image_conversation.assert_not_called()
            backend._start_image_generation.assert_not_called()
        else:
            self.harness.assert_no_replay(backend)

    def test_request_exposes_checkpoint_callback(self):
        self.assertIn("delivery_checkpoint_callback", inspect.signature(conversation.ConversationRequest).parameters)

    def test_final_arriving_after_130_seconds_uses_new_budget_and_returns_original_call(self):
        rows = fixtures.revisions(1)
        backend = self.backend(rows[:-1], stream_error=conversation.ImageGenerationTimeoutError("synthetic timeout"))
        pending = {"conversation_id": fixtures.CID, "async_status": 3, "messages": rows[:-1]}
        complete = {"conversation_id": fixtures.CID, "async_status": 4, "messages": rows}
        started, reads = self.readback(backend, pending, complete=complete, ready_after=130.0)

        result = self.run_result(backend)

        self.fixture.assert_only_image(result, self.expected)
        self.assertGreaterEqual(self.clock.now - started, 130.0)
        self.assert_reads_bounded(started, reads, 180.0)
        self.assertTrue(all(read[3] for read in reads), "checkpoint must precede the first recovery GET")
        self.assertTrue(self.clock.sleeps)
        self.assertTrue(all(seconds == 5.0 for seconds in self.clock.sleeps))
        self.assertEqual(fixtures.CID, self.checkpoints[0].get("conversation_id"))
        self.assertEqual(self.context(), self.checkpoints[0].get("delivery_context"))
        self.assert_no_generation(backend)

    def test_pending_timeout_preserves_only_bounded_identity_evidence(self):
        rows = fixtures.revisions(1)[:-1]
        rows[1]["content"]["text"] = "synthetic_private_code_marker()"
        backend = self.backend(rows, stream_error=conversation.ImageGenerationTimeoutError("synthetic secret token marker"))
        document = {"conversation_id": fixtures.CID, "async_status": 3, "messages": rows}
        started, reads = self.readback(backend, document)

        with self.assertRaises(conversation.ImageGenerationError) as caught:
            self.run_result(backend)

        self.assert_pending(caught.exception, source_timeout=True)
        self.assertEqual(self.context(), caught.exception.delivery_context)
        self.assert_reads_bounded(started, reads, 180.0)
        self.assertGreaterEqual(self.clock.now - started, 175.0)
        saved = json.dumps([self.checkpoints, caught.exception.delivery_context], ensure_ascii=False)
        for private in ("synthetic private prompt marker", "synthetic_private_code_marker", "synthetic secret token marker",
                        "synthetic-access-token", "\"content\"", "\"thoughts\""):
            self.assertNotIn(private, saved)
        self.harness.assert_no_download(backend)
        self.assert_no_generation(backend)

    def test_unfinished_new_tool_observed_before_timeout_cannot_return_old_snapshot(self):
        rows = fixtures.revisions(2)[:-1]
        rows[-1]["status"] = "in_progress"
        rows[-1]["metadata"]["is_complete"] = False
        backend = self.backend(rows, stream_error=conversation.ImageGenerationTimeoutError("synthetic timeout"))
        stale = {"conversation_id": fixtures.CID, "async_status": 4, "messages": fixtures.revisions(1)}
        started, reads = self.readback(backend, stale)

        with self.assertRaises(conversation.ImageGenerationError) as caught:
            self.run_result(backend)

        self.assert_pending(caught.exception, source_timeout=True)
        self.assertEqual(self.context(count=2), caught.exception.delivery_context)
        self.assert_reads_bounded(started, reads, 180.0)
        self.harness.assert_no_download(backend)
        self.assert_no_generation(backend)

    def test_quota_error_never_enters_timeout_recovery(self):
        error = ImageQuotaExceeded({"conversation_id": fixtures.CID, "restore_at": 2000000000.0,
                                   "reason": "synthetic quota limit"})
        backend = self.backend(fixtures.revisions(1)[:-1], stream_error=error)

        with self.assertRaises(ImageQuotaExceeded) as caught:
            self.run_result(backend)

        self.assertIs(error, caught.exception)
        backend._get_conversation.assert_not_called()
        self.assertEqual([], self.clock.sleeps)
        self.harness.assert_no_download(backend)
        self.assert_no_generation(backend)

    def test_explicit_failed_tool_before_timeout_does_not_extend_wait(self):
        rows = fixtures.revisions(1)[:-1]
        rows[-1]["status"] = "failed"
        rows[-1]["metadata"]["error"] = "synthetic explicit failure"
        backend = self.backend(rows, stream_error=conversation.ImageGenerationTimeoutError("synthetic timeout"))

        with self.assertRaises(conversation.ImageGenerationError) as caught:
            self.run_result(backend)

        self.assertEqual("image_generation_failed", caught.exception.code)
        backend._get_conversation.assert_not_called()
        self.assertEqual([], self.clock.sleeps)
        self.harness.assert_no_download(backend)
        self.assert_no_generation(backend)

    def test_resume_with_observed_evidence_can_wait_past_old_eight_second_limit(self):
        rows = fixtures.revisions(1)
        backend = self.backend(rows)
        del backend._image_delivery_deadline
        pending = {"conversation_id": fixtures.CID, "async_status": 3, "messages": rows[:-1]}
        complete = {"conversation_id": fixtures.CID, "async_status": 4, "messages": rows}
        started, reads = self.readback(backend, pending, complete=complete, ready_after=30.0)

        try:
            result = self.run_result(backend, request=self.request(context=self.context()), resume=True, timeout=60.0)
        except conversation.ImageGenerationError as exc:
            self.fail(f"explicit resume budget ended before the final arrived: {exc.code}")

        self.fixture.assert_only_image(result, self.expected)
        self.assertGreaterEqual(self.clock.now - started, 30.0)
        self.assert_reads_bounded(started, reads, 60.0)
        self.assert_no_generation(backend, resume=True)

    def test_resume_explicit_budget_bounds_initial_and_followup_reads(self):
        rows = fixtures.revisions(1)[:-1]
        backend = self.backend(rows)
        del backend._image_delivery_deadline
        pending = {"conversation_id": fixtures.CID, "async_status": 3, "messages": rows}
        started, reads = self.readback(backend, pending)

        with self.assertRaises(conversation.ImageGenerationError) as caught:
            self.run_result(backend, request=self.request(context=self.context()), resume=True, timeout=15.0)

        self.assert_pending(caught.exception)
        self.assert_reads_bounded(started, reads, 15.0)
        self.assertGreaterEqual(self.clock.now - started, 10.0)
        self.assertEqual(self.context(), caught.exception.delivery_context)
        self.assert_no_generation(backend, resume=True)

    def test_resume_120_second_budget_stays_bounded(self):
        rows = fixtures.revisions(1)[:-1]
        backend = self.backend(rows)
        del backend._image_delivery_deadline
        pending = {"conversation_id": fixtures.CID, "async_status": 3, "messages": rows}
        started, reads = self.readback(backend, pending)

        with self.assertRaises(conversation.ImageGenerationError) as caught:
            self.run_result(backend, request=self.request(context=self.context()), resume=True, timeout=120.0)

        self.assert_pending(caught.exception)
        self.assert_reads_bounded(started, reads, 120.0)
        self.assertGreaterEqual(self.clock.now - started, 115.0)
        self.harness.assert_no_download(backend)
        self.assert_no_generation(backend, resume=True)

    def test_legacy_context_keeps_eight_second_four_get_compatibility(self):
        rows = fixtures.revisions(1)[:-1]
        backend = self.backend(rows)
        del backend._image_delivery_deadline
        legacy = self.context()
        legacy.pop("observed_evidence")
        started, reads = self.readback(backend, {"conversation_id": fixtures.CID, "async_status": 3, "messages": rows})

        with self.assertRaises(conversation.ImageGenerationError):
            self.run_result(backend, request=self.request(context=legacy), resume=True, timeout=60.0)

        self.assert_reads_bounded(started, reads, 8.0, limit=4)
        self.assertTrue(all(read[2] <= 3.0 for read in reads))
        self.harness.assert_no_download(backend)
        self.assert_no_generation(backend, resume=True)

    def test_resume_cannot_lose_newly_observed_tools(self):
        rows = fixtures.revisions(1)
        backend = self.backend(rows)
        del backend._image_delivery_deadline
        started, reads = self.readback(backend, {"conversation_id": fixtures.CID, "async_status": 4, "messages": rows})

        with self.assertRaises(conversation.ImageGenerationError) as caught:
            self.run_result(backend, request=self.request(context=self.context(count=2)), resume=True, timeout=10.0)

        self.assert_pending(caught.exception)
        self.assertEqual(self.context(count=2), caught.exception.delivery_context)
        self.assert_reads_bounded(started, reads, 10.0)
        self.harness.assert_no_download(backend)
        self.assert_no_generation(backend, resume=True)

    def test_resume_cannot_replace_observed_native_asset_under_same_tool_id(self):
        rows = fixtures.revisions(1)
        rows[2]["content"]["parts"][0]["asset_pointer"] = "sediment://" + fixtures.file_id(2)
        backend = self.backend(rows)
        backend._request_image_transfer.side_effect = None
        backend._request_image_transfer.return_value = Mock(content=self.expected)
        del backend._image_delivery_deadline
        started, reads = self.readback(backend, {"conversation_id": fixtures.CID, "async_status": 4, "messages": rows})

        with self.assertRaises(conversation.ImageGenerationError):
            self.run_result(backend, request=self.request(context=self.context()), resume=True, timeout=10.0)

        self.assert_reads_bounded(started, reads, 10.0)
        self.harness.assert_no_download(backend)
        self.assert_no_generation(backend, resume=True)


if __name__ == "__main__":
    unittest.main()
