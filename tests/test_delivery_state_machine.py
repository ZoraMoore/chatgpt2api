"""交付状态机回归：完整业务链使用合成图片，外部传输全部隔离。"""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from services.protocol import conversation
from services.protocol.image_delivery import ImageDeliveryEvidence
from tests import test_image_final_contract as fixtures


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if not isinstance(seconds, (int, float)) or not 0 < seconds <= 2.0:
            raise AssertionError("confirmation sleeps must be short and bounded")
        self.sleeps.append(seconds)
        self.now += seconds


class DeliveryStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ImageFinalContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(patch.object(conversation, "logger", Mock()))
        self.expected = fixtures.png("blue")
        self.clock = FakeClock()
        self.enterContext(patch.object(conversation.time, "monotonic", self.clock.monotonic))
        self.enterContext(patch.object(conversation.time, "sleep", self.clock.sleep))

    def backend(self, rows):
        backend = self.fixture.backend(rows, image_bytes={fixtures.file_id(1): self.expected})
        backend._image_delivery_deadline = 104.0
        # 明确阻止重放路径；其余业务处理使用真实的stream_image_outputs。
        backend._post_image_conversation = Mock(side_effect=AssertionError("generation POST must not be replayed"))
        backend._start_image_generation = Mock(side_effect=AssertionError("generation must not be restarted"))
        return backend

    def marker_backend(self, *, marker_cid=fixtures.CID, omit_cid=False, after=()):
        rows = fixtures.revisions(1)
        rows[-1].update(status="in_progress", end_turn=False)
        backend = self.backend(rows)
        frames = list(backend.stream_conversation.return_value)[:-1]
        marker = {"type": "message_stream_complete"}
        if not omit_cid:
            marker["conversation_id"] = marker_cid
        frames.extend([json.dumps(marker), *(json.dumps(event) for event in after), "[DONE]"])
        backend.stream_conversation.return_value = iter(frames)
        return backend, rows

    def tool_only_backend(self, *, extra_rows=(), marker=True, after=()):
        rows = [*fixtures.revisions(1)[:-1], *extra_rows]
        backend = self.backend(rows)
        frames = list(backend.stream_conversation.return_value)[:-1]
        if marker:
            frames.append(json.dumps({"type": "message_stream_complete", "conversation_id": fixtures.CID}))
        frames.extend([*(json.dumps(event) for event in after), "[DONE]"])
        backend.stream_conversation.return_value = iter(frames)
        return backend

    def assert_no_replay(self, backend):
        backend.stream_conversation.assert_called_once()
        backend._post_image_conversation.assert_not_called()
        backend._start_image_generation.assert_not_called()

    def assert_no_download(self, backend):
        backend._request_image_transfer.assert_not_called()
        backend.download_final_sandbox_images.assert_not_called()

    def assert_single_final(self, backend):
        result = self.fixture.run_result(backend)
        self.fixture.assert_only_image(result, self.expected)
        self.assert_no_replay(backend)
        return result

    def readback_sequence(self, backend, documents):
        pending = list(documents)
        reads = []

        def readback(cid, *, timeout_secs):
            reads.append((self.clock.now, cid, timeout_secs))
            if len(reads) > 4:
                # 让错误实现也有确定的结束点，避免测试本身进入无限重试。
                self.clock.now = 105.0
                raise AssertionError("unbounded confirmation readbacks")
            self.assertEqual(fixtures.CID, cid)
            self.assertGreater(timeout_secs, 0)
            self.assertLessEqual(timeout_secs, 3.0)
            self.assertLessEqual(timeout_secs, 104.0 - self.clock.now + 1e-9)
            self.clock.now += min(0.1, timeout_secs)
            return copy.deepcopy(pending.pop(0) if len(pending) > 1 else pending[0])

        backend._get_conversation.side_effect = readback
        return reads

    def test_marker_without_cid_inherits_already_bound_conversation(self):
        backend, _ = self.marker_backend(omit_cid=True)
        self.assert_single_final(backend)
        backend._get_conversation.assert_not_called()

    def test_marker_with_explicit_wrong_cid_cannot_complete_current_turn(self):
        backend, _ = self.marker_backend(marker_cid="another-conversation")
        with self.assertRaises(conversation.ImageGenerationError):
            self.fixture.run_result(backend)
        self.assert_no_download(backend)
        self.assert_no_replay(backend)

    def test_successful_tool_with_marker_and_done_delivers_without_assistant_final(self):
        backend = self.tool_only_backend()
        self.assert_single_final(backend)
        backend._get_conversation.assert_not_called()

    def test_successful_tool_with_only_done_does_not_invent_completion(self):
        backend = self.tool_only_backend(marker=False)
        with self.assertRaises(conversation.ImageGenerationError):
            self.fixture.run_result(backend)
        self.assert_no_download(backend)
        self.assert_no_replay(backend)

    def test_pending_tool_after_marker_cannot_return_prior_image(self):
        call = fixtures.message(
            "new-image-call", recipient="synthetic.image", channel="analysis",
            content={"content_type": "code", "text": "synthetic_revision()"},
        )
        result = fixtures.image_result(2, fixtures.file_id(2))
        result.update(status="in_progress")
        result["metadata"]["is_complete"] = False
        backend = self.tool_only_backend(after=[
            {"conversation_id": fixtures.CID, "message": row} for row in (call, result)
        ])
        with self.assertRaises(conversation.ImageGenerationError):
            self.fixture.run_result(backend)
        self.assert_no_download(backend)
        self.assert_no_replay(backend)

    def test_failed_tool_after_marker_cannot_return_prior_image_or_recheck_it(self):
        call = fixtures.message(
            "failed-revision-call", recipient="synthetic.image", channel="analysis",
            content={"content_type": "code", "text": "synthetic_revision()"},
        )
        result = fixtures.message(
            "failed-revision-result", "tool", name="synthetic.image", status="failed",
            content={"content_type": "text", "parts": ["synthetic revision failed"]},
            metadata={"error": "synthetic failure"},
        )
        backend = self.tool_only_backend(after=[
            {"conversation_id": fixtures.CID, "message": row} for row in (call, result)
        ])
        with self.assertRaises(conversation.ImageGenerationError):
            self.fixture.run_result(backend)
        self.assert_no_download(backend)
        self.assert_no_replay(backend)
        backend._get_conversation.assert_not_called()

    def test_successful_inspection_before_marker_keeps_unique_generated_artifact(self):
        call = fixtures.message(
            "view-call", recipient="container.open_image", channel="analysis",
            content={"content_type": "code", "text": "synthetic_view()"},
        )
        result = fixtures.message(
            "view-result", "tool", name="container.open_image",
            content={"content_type": "text", "parts": ["synthetic inspection completed"]},
        )
        backend = self.tool_only_backend(extra_rows=[call, result])
        output = self.assert_single_final(backend)
        self.assertEqual(1, output["_image_execution"][0]["tools"]["container.open_image"])
        backend._get_conversation.assert_not_called()

    def test_unfinished_inspection_cannot_be_completed_by_marker(self):
        call = fixtures.message(
            "pending-view-call", recipient="container.open_image", channel="analysis",
            content={"content_type": "code", "text": "synthetic_view()"},
        )
        result = fixtures.message(
            "pending-view-result", "tool", name="container.open_image", status="in_progress",
            content={"content_type": "text", "parts": ["synthetic inspection pending"]},
            metadata={"is_complete": False},
        )
        backend = self.tool_only_backend(extra_rows=[call, result])
        with self.assertRaises(conversation.ImageGenerationError):
            self.fixture.run_result(backend)
        self.assert_no_download(backend)
        self.assert_no_replay(backend)

    def test_identical_old_tool_replay_preserves_original_message_order(self):
        rows = fixtures.revisions(1)
        evidence = ImageDeliveryEvidence()
        for row in rows:
            evidence.observe(row, fixtures.CID)
        evidence.observe(copy.deepcopy(rows[2]), fixtures.CID)
        document = evidence.document(fixtures.CID, {
            "submitted_message_id": fixtures.SUBMITTED,
            "input_asset_ids": [fixtures.INPUT_ID], "input_image_hashes": [],
        })
        self.assertEqual([row["id"] for row in rows], [row["id"] for row in document["messages"]])

    def test_identical_old_tool_replay_does_not_erase_completion(self):
        rows = fixtures.revisions(1)
        backend, _ = self.marker_backend(after=[{"conversation_id": fixtures.CID, "message": rows[2]}])
        self.assert_single_final(backend)
        backend._get_conversation.assert_not_called()

    def test_latency_metadata_patch_does_not_erase_completion(self):
        backend, _ = self.marker_backend(after=[{
            "p": "/message/metadata/latency_ms", "o": "add", "v": 17,
        }])
        self.assert_single_final(backend)
        backend._get_conversation.assert_not_called()

    def test_new_tool_work_after_marker_still_invalidates_completion(self):
        new_work = fixtures.message(
            "new-python-call", recipient="python", channel="analysis", status="in_progress",
            content={"content_type": "code", "text": "synthetic_next_step()"},
        )
        backend, _ = self.marker_backend(after=[{"conversation_id": fixtures.CID, "message": new_work}])
        with self.assertRaises(conversation.ImageGenerationError):
            self.fixture.run_result(backend)
        self.assert_no_download(backend)
        self.assert_no_replay(backend)

    def test_changed_final_content_after_marker_still_invalidates_completion(self):
        backend, _ = self.marker_backend(after=[{
            "p": "/message/content/parts/0", "o": "append", "v": "synthetic revision continues",
        }])
        with self.assertRaises(conversation.ImageGenerationError):
            self.fixture.run_result(backend)
        self.assert_no_download(backend)
        self.assert_no_replay(backend)

    def test_newer_live_final_revision_is_not_overwritten_by_stale_same_id_snapshot(self):
        stale = fixtures.revisions(1)
        live = copy.deepcopy(stale)
        live[-1].update(status="in_progress", end_turn=False)
        live[-1]["content"]["parts"] = ["synthetic revision continues after the earlier draft"]
        backend = self.backend(live)
        self.readback_sequence(backend, [{"conversation_id": fixtures.CID, "messages": stale}])
        with self.assertRaises(conversation.ImageGenerationError):
            self.fixture.run_result(backend)
        self.assert_no_download(backend)
        self.assert_no_replay(backend)

    def test_pending_first_snapshot_is_retried_until_complete_within_original_budget(self):
        rows = fixtures.revisions(1)
        backend = self.backend(rows[:-1])
        complete = {"conversation_id": fixtures.CID, "async_status": 4, "messages": rows}
        pending = copy.deepcopy(complete)
        pending["async_status"] = 3
        pending["messages"][-1].update(status="in_progress", end_turn=False)
        reads = self.readback_sequence(backend, [pending, complete])
        self.assert_single_final(backend)
        self.assertGreaterEqual(len(reads), 2)
        self.assertLessEqual(len(reads), 4)
        self.assertLessEqual(self.clock.now, 104.0 + 1e-9)

    def test_permanently_pending_snapshots_exhaust_a_bounded_original_budget(self):
        rows = fixtures.revisions(1)
        backend = self.backend(rows[:-1])
        pending = {"conversation_id": fixtures.CID, "async_status": 3, "messages": rows}
        reads = self.readback_sequence(backend, [pending])
        with self.assertRaises(conversation.ImageGenerationError):
            self.fixture.run_result(backend)
        self.assertGreaterEqual(len(reads), 2)
        self.assertLessEqual(len(reads), 4)
        self.assertLessEqual(self.clock.now, 104.0 + 1e-9)
        self.assert_no_download(backend)
        self.assert_no_replay(backend)

    def test_complete_final_can_read_back_missing_tool_pointer(self):
        complete = fixtures.revisions(1)
        sparse = copy.deepcopy(complete)
        sparse[2]["content"]["parts"] = []
        backend = self.backend(sparse)
        reads = self.readback_sequence(backend, [{"conversation_id": fixtures.CID, "messages": complete}])
        self.assert_single_final(backend)
        self.assertGreaterEqual(len(reads), 1)
        self.assertLessEqual(len(reads), 4)

    def test_resume_observes_only_original_submission_turn_not_prior_tools(self):
        current = fixtures.revisions(1)
        earlier = copy.deepcopy(fixtures.revisions(1))
        for row in earlier:
            row["id"] = "earlier-" + row["id"]
        earlier[2]["content"]["parts"][0]["asset_pointer"] = "sediment://" + fixtures.file_id(99)
        backend = self.backend(current)
        backend._get_conversation.return_value = {
            "conversation_id": fixtures.CID, "async_status": 4, "messages": [*earlier, *current],
        }
        request = conversation.ConversationRequest(
            model="gpt-image-2.6-sunburst", image_transport="conversation", response_format="b64_json",
            delivery_context={"submitted_message_id": fixtures.SUBMITTED,
                              "input_asset_ids": [fixtures.INPUT_ID],
                              "input_image_hashes": [hashlib.sha256(fixtures.INPUT_BYTES).hexdigest()]},
        )
        result = conversation.collect_image_outputs(conversation.stream_image_outputs(
            backend, request, resume_conversation_id=fixtures.CID, resume_has_reference=True,
        ))
        self.fixture.assert_only_image(result, self.expected)
        backend.stream_conversation.assert_not_called()
        backend._post_image_conversation.assert_not_called()
        backend._start_image_generation.assert_not_called()
        self.assertEqual(1, backend._get_conversation.call_count)
        self.assertEqual(1, result["_image_execution"][0]["tools"]["synthetic.image"])

    def resume_pending(self, *, timeout, consume_get):
        rows = fixtures.revisions(1)
        backend = self.backend(rows)
        # 续查新建后端，不复用上次已耗尽的生成截止时间。
        del backend._image_delivery_deadline
        document = {"conversation_id": fixtures.CID, "async_status": 3, "messages": rows}
        reads = []

        def readback(cid, *, timeout_secs):
            reads.append((self.clock.now, cid, timeout_secs))
            if len(reads) > 8:
                self.clock.now = 200.0
                raise AssertionError("resume confirmation is unbounded")
            self.clock.now += min(consume_get, timeout_secs)
            return copy.deepcopy(document)

        backend._get_conversation.side_effect = readback
        request = conversation.ConversationRequest(
            model="gpt-image-2.6-sunburst", image_transport="conversation",
            delivery_context={"submitted_message_id": fixtures.SUBMITTED,
                              "input_asset_ids": [fixtures.INPUT_ID], "input_image_hashes": []},
        )
        with self.assertRaises(conversation.ImageGenerationError):
            list(conversation.stream_image_outputs(
                backend, request, resume_conversation_id=fixtures.CID, poll_timeout_secs=timeout,
            ))
        backend.stream_conversation.assert_not_called()
        backend._post_image_conversation.assert_not_called()
        backend._start_image_generation.assert_not_called()
        self.assert_no_download(backend)
        return backend, reads

    def test_resume_initial_get_counts_toward_total_four_reads_and_per_get_limit(self):
        _, reads = self.resume_pending(timeout=60.0, consume_get=0.1)
        self.assertGreaterEqual(len(reads), 2)
        self.assertLessEqual(len(reads), 4)
        for started, cid, timeout in reads:
            self.assertEqual(fixtures.CID, cid)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 3.0)
            self.assertLessEqual(timeout, 108.0 - started + 1e-9)
        self.assertLessEqual(self.clock.now, 108.0 + 1e-9)

    def test_resume_initial_get_consumes_shared_eight_second_budget(self):
        _, reads = self.resume_pending(timeout=60.0, consume_get=3.0)
        self.assertGreaterEqual(len(reads), 2)
        self.assertLessEqual(len(reads), 4)
        self.assertLessEqual(self.clock.now, 108.0 + 1e-9)
        for started, _, timeout in reads:
            self.assertLessEqual(timeout, min(3.0, 108.0 - started) + 1e-9)

    def test_resume_shorter_explicit_deadline_bounds_initial_and_followup_gets(self):
        _, reads = self.resume_pending(timeout=2.0, consume_get=0.1)
        self.assertGreaterEqual(len(reads), 1)
        self.assertLessEqual(len(reads), 4)
        self.assertLessEqual(self.clock.now, 102.0 + 1e-9)
        for started, _, timeout in reads:
            self.assertLessEqual(timeout, min(3.0, 102.0 - started) + 1e-9)

    def test_selector_programming_error_during_readback_is_not_retried_as_transport_failure(self):
        rows = fixtures.revisions(1)
        backend = self.backend(rows[:-1])
        self.readback_sequence(backend, [{"conversation_id": fixtures.CID, "async_status": 4, "messages": rows}])
        select = conversation.select_terminal_image_delivery

        def broken_selector(document, **options):
            if document.get("async_status") == 4:
                raise ValueError("synthetic selector programming error")
            return select(document, **options)

        with patch.object(conversation, "select_terminal_image_delivery", side_effect=broken_selector):
            with self.assertRaisesRegex(ValueError, "synthetic selector programming error"):
                self.fixture.run_result(backend)
        self.assertEqual(1, backend._get_conversation.call_count)
        self.assert_no_download(backend)
        self.assert_no_replay(backend)

    def test_download_failure_context_resumes_exact_native_asset_when_final_is_omitted(self):
        rows = fixtures.revisions(2)
        selected_asset = fixtures.file_id(2)
        first = self.backend(rows)
        first._resolve_image_urls = Mock(wraps=first._resolve_image_urls)
        first.download_image_bytes = Mock(side_effect=RuntimeError("synthetic download interruption"))
        with self.assertRaises(conversation.ImageGenerationError) as raised:
            self.fixture.run_result(first)
        self.assertEqual("image_final_download_failed", raised.exception.code)
        context = copy.deepcopy(raised.exception.delivery_context)
        receipt = context.get("completion_receipt")
        self.assertIsInstance(receipt, dict)
        self.assertEqual(fixtures.CID, receipt["conversation_id"])
        self.assertEqual(fixtures.SUBMITTED, receipt["submitted_message_id"])
        self.assertEqual("image-result-2", receipt["message_id"])
        self.assertEqual(selected_asset, receipt["asset_id"])
        first._resolve_image_urls.assert_called_once_with(fixtures.CID, [], [selected_asset])
        first.download_image_bytes.assert_called_once()
        self.assert_no_replay(first)

        # 新后端没有直播完成状态，只有异常携带的原轮次证明可用于缺失final的历史文档。
        resumed = self.fixture.backend(rows, image_bytes={
            fixtures.file_id(1): fixtures.png("yellow"), selected_asset: self.expected,
        })
        resumed._get_conversation.return_value = {
            "conversation_id": fixtures.CID, "async_status": 4, "messages": copy.deepcopy(rows[:-1]),
        }
        resumed.stream_conversation = Mock(side_effect=AssertionError("resume must not submit generation"))
        resumed._post_image_conversation = Mock(side_effect=AssertionError("generation POST must not be replayed"))
        resumed._start_image_generation = Mock(side_effect=AssertionError("generation must not be restarted"))
        resumed._resolve_image_urls = Mock(wraps=resumed._resolve_image_urls)
        resumed.download_image_bytes = Mock(wraps=resumed.download_image_bytes)
        request = conversation.ConversationRequest(
            model="gpt-image-2.6-sunburst", image_transport="conversation", response_format="b64_json",
            delivery_context=context,
        )
        result = conversation.collect_image_outputs(conversation.stream_image_outputs(
            resumed, request, resume_conversation_id=fixtures.CID, poll_timeout_secs=8,
            resume_has_reference=True,
        ))
        self.fixture.assert_only_image(result, self.expected)
        self.assertEqual("stored_completion", result["_image_execution"][0]["completion_source"])
        resumed._get_conversation.assert_called_once()
        resumed._resolve_image_urls.assert_called_once_with(fixtures.CID, [], [selected_asset])
        resumed.download_image_bytes.assert_called_once_with([f"https://example.invalid/{selected_asset}.png"])
        resumed._request_image_transfer.assert_called_once()
        resumed.download_final_sandbox_images.assert_not_called()
        resumed.stream_conversation.assert_not_called()
        resumed._post_image_conversation.assert_not_called()
        resumed._start_image_generation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
