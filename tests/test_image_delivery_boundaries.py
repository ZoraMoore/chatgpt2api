import base64
import copy
import json
import unittest
from unittest.mock import Mock, PropertyMock, patch

from services.config import config
from services.protocol import conversation as conv
from services.protocol.image_delivery import delivery_context
from tests.test_image_final_contract import (
    CID, SUBMITTED, INPUT_BYTES, ImageFinalContractTests, revisions, png, file_id, message,
)


class ImageDeliveryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ImageFinalContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def request(self):
        return conv.ConversationRequest(model="gpt-image-2.6-sunburst", prompt="edit",
            images=[base64.b64encode(INPUT_BYTES).decode("ascii")], image_transport="conversation")

    def test_new_request_without_submission_identity_is_rejected(self):
        backend = self.fixture.backend(revisions(1), image_bytes={file_id(1): png("blue")})
        del backend._image_submitted_message_id
        with self.assertRaises(conv.ImageGenerationError):
            self.fixture.run_result(backend)

    def test_resume_follows_current_mapping_branch_only(self):
        rows = revisions(1)
        backend = self.fixture.backend(rows, image_bytes={file_id(1): png("blue")})
        mapping = {}
        parent = None
        for row in rows:
            mapping[row["id"]] = {"message": row, "parent": parent}
            parent = row["id"]
        discarded = message("discarded", channel="final", end_turn=True,
            content={"content_type": "text", "parts": ["无法完成图片修改"]})
        mapping["discarded"] = {"message": discarded, "parent": SUBMITTED}
        backend._get_conversation.return_value = {"conversation_id": CID, "mapping": mapping, "current_node": parent}
        result = conv.collect_image_outputs(conv.stream_image_outputs(
            backend, self.request(), resume_conversation_id=CID, resume_has_reference=True))
        self.assertEqual(1, len(result["data"]))
        backend.stream_conversation.assert_not_called()

    def test_required_reference_postprocess_cannot_deliver_native_image(self):
        backend = self.fixture.backend(revisions(1), image_bytes={file_id(1): png("blue")})
        with patch.object(type(config), "image_require_reference_postprocess", new_callable=PropertyMock, return_value=True):
            with self.assertRaises(conv.ImageGenerationError):
                self.fixture.run_result(backend)
        backend._request_image_transfer.assert_not_called()

    def test_excess_input_identities_are_rejected_without_truncation(self):
        request = self.request()
        request.delivery_context = {"submitted_message_id": SUBMITTED,
            "input_asset_ids": [f"old-{i}" for i in range(20)], "input_image_hashes": []}
        backend = Mock(_image_input_asset_ids=[f"uploaded-{i}" for i in range(10)])
        with self.assertRaises(ValueError):
            delivery_context(backend, request)

    def test_failed_live_output_is_not_replaced_with_stale_success_snapshot(self):
        rows = revisions(1)
        stale = copy.deepcopy(rows)
        rows[2]["status"] = "error"
        rows[2]["metadata"]["error"] = "failed"
        backend = self.fixture.backend(rows, image_bytes={file_id(1): png("blue")})
        backend._get_conversation.return_value = {"conversation_id": CID, "messages": stale}
        with self.assertRaises(conv.ImageGenerationError):
            self.fixture.run_result(backend)

    def test_snapshot_missing_newer_live_final_cannot_deliver_old_final(self):
        rows = revisions(1)
        stale = copy.deepcopy(rows)
        rows[-1].update(id="newer-final", status="in_progress", end_turn=False)
        backend = self.fixture.backend(rows, image_bytes={file_id(1): png("blue")})
        backend._get_conversation.return_value = {"conversation_id": CID, "messages": stale}
        with self.assertRaises(conv.ImageGenerationError):
            self.fixture.run_result(backend)

    def test_snapshot_cannot_replace_observed_output_pointer_with_older_version(self):
        rows = revisions(1)
        stale = copy.deepcopy(rows)
        rows[2]["content"]["parts"][0]["asset_pointer"] = "sediment://" + file_id(2)
        rows[-1].update(status="in_progress", end_turn=False)
        backend = self.fixture.backend(rows, image_bytes={file_id(1): png("blue"), file_id(2): png("green")})
        backend._get_conversation.return_value = {"conversation_id": CID, "messages": stale}
        with self.assertRaises(conv.ImageGenerationError):
            self.fixture.run_result(backend)

    def test_two_requested_subtasks_each_deliver_their_own_final_image(self):
        first = self.fixture.backend(revisions(3), image_bytes={file_id(i): png((10 * i, 20, 30)) for i in range(1, 4)})
        second = self.fixture.backend(revisions(5), image_bytes={file_id(i): png((20 * i, 30, 40)) for i in range(1, 6)})
        first.close = Mock()
        second.close = Mock()
        request = self.request()
        request.n = 2
        conv.account_service.get_account.return_value = {}
        with patch.object(conv, "OpenAIBackendAPI", side_effect=[first, second]), \
             patch.object(type(config), "image_parallel_generation", new_callable=PropertyMock, return_value=False):
            result = conv.collect_image_outputs(conv.stream_image_outputs_with_pool(request))
        self.assertEqual(2, len(result["data"]))
        self.assertEqual(png((30, 20, 30)), base64.b64decode(result["data"][0]["b64_json"]))
        self.assertEqual(png((100, 30, 40)), base64.b64decode(result["data"][1]["b64_json"]))
        self.assertEqual(1, first.stream_conversation.call_count)
        self.assertEqual(1, second.stream_conversation.call_count)

    def marker_backend(self, *, marker_cid=CID, after=None, pending=False, done=True):
        rows = revisions(1)
        rows[-1].update(status="in_progress", end_turn=False)
        if pending:
            rows.insert(-1, message("pending-call", recipient="synthetic.image", status="in_progress",
                                   content={"content_type": "code", "text": "pending"}))
        backend = self.fixture.backend(rows, image_bytes={file_id(1): png("blue")})
        frames = list(backend.stream_conversation.return_value)[:-1]
        frames.append(json.dumps({"type": "message_stream_complete", "conversation_id": marker_cid}))
        if after:
            frames.append(json.dumps({"conversation_id": CID, "message": after}))
        if done:
            frames.append("[DONE]")
        backend.stream_conversation.return_value = iter(frames)
        return backend

    def test_observed_image_only_completion_selects_unique_last_tool(self):
        backend = self.marker_backend()
        result = self.fixture.run_result(backend)
        self.assertEqual(1, len(result["data"]))
        backend._get_conversation.assert_not_called()

    def test_image_only_marker_requires_same_conversation_and_done(self):
        for kwargs in ({"marker_cid": "other"}, {"done": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(conv.ImageGenerationError):
                self.fixture.run_result(self.marker_backend(**kwargs))

    def test_image_only_marker_cannot_hide_unfinished_call(self):
        with self.assertRaises(conv.ImageGenerationError):
            self.fixture.run_result(self.marker_backend(pending=True))

    def test_work_after_marker_invalidates_image_only_completion(self):
        later = message("later-call", recipient="python", status="in_progress",
                        content={"content_type": "code", "text": "later_work()"})
        with self.assertRaises(conv.ImageGenerationError):
            self.fixture.run_result(self.marker_backend(after=later))

    def test_marker_cannot_hide_failure_or_fallback_text(self):
        for mutation in ("earlier_failed", "fallback", "refusal", "final_text", "unanswered_call"):
            with self.subTest(mutation=mutation):
                rows = revisions(2)
                rows[-1].update(status="in_progress", end_turn=False)
                if mutation == "earlier_failed":
                    rows[2]["status"] = "failed"
                elif mutation == "fallback":
                    rows[-1]["metadata"] = {"model_dil_v2": {"fallbackMarkdown": "无法完成图片修改"}}
                elif mutation == "refusal":
                    rows[-1]["metadata"] = {"is_refusal": True}
                elif mutation == "final_text":
                    rows[-1]["content"]["parts"] = ["正在修改"]
                else:
                    rows.insert(-1, message("unanswered", recipient="python", status="finished_successfully",
                        content={"content_type": "code", "text": "work()"}))
                backend = self.fixture.backend(rows, image_bytes={file_id(1): png("blue"), file_id(2): png("green")})
                frames = list(backend.stream_conversation.return_value)
                frames.insert(-1, json.dumps({"type": "message_stream_complete", "conversation_id": CID}))
                backend.stream_conversation.return_value = iter(frames)
                with self.assertRaises(conv.ImageGenerationError):
                    self.fixture.run_result(backend)

    def test_marker_patch_of_same_final_invalidates_receipt(self):
        backend = self.marker_backend()
        frames = list(backend.stream_conversation.return_value)
        frames.insert(-1, json.dumps({"p": "/message/content/parts/0", "o": "append", "v": "继续处理"}))
        backend.stream_conversation.return_value = iter(frames)
        with self.assertRaises(conv.ImageGenerationError):
            self.fixture.run_result(backend)

    def test_review_started_after_marker_invalidates_receipt(self):
        backend = self.marker_backend()
        frames = list(backend.stream_conversation.return_value)
        frames.insert(-1, json.dumps({"type": "image_review_started", "reason": "further processing"}))
        backend.stream_conversation.return_value = iter(frames)
        with self.assertRaises(conv.ImageGenerationError):
            self.fixture.run_result(backend)


if __name__ == "__main__":
    unittest.main()
