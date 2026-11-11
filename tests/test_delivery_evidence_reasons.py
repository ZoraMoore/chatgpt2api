"""输入证据应保留准确拒绝原因，并允许在原会话补全证据后交付。"""
from __future__ import annotations

import copy
import inspect
import json
import unittest
from unittest.mock import Mock, patch

from services.protocol import conversation
from services.protocol.image_delivery import ImageDeliveryEvidence, bind_input_assets
from services.protocol.terminal_image_delivery import select_terminal_image_delivery
from tests import test_image_final_contract as fixtures
from tests.test_delivery_state_machine import FakeClock
from tests.test_terminal_image_delivery import image_result, native_document


def delivery_context():
    return {"submitted_message_id": "user-1", "input_asset_ids": ["file-uploaded"],
            "input_image_hashes": []}


class DeliveryEvidenceReasonTests(unittest.TestCase):
    def bind_with_reason(self, document, context):
        self.assertIn("diagnostic", inspect.signature(bind_input_assets).parameters,
                      "输入资产校验必须提供独立诊断，不能借空文档伪装成会话不匹配")
        diagnostic = {}
        bound = bind_input_assets(document, context, diagnostic=diagnostic)
        return bound, diagnostic

    def invalid_cases(self):
        wrong_user = native_document()
        wrong_user["messages"][0]["id"] = "unrelated-user"
        missing_user = native_document()
        del missing_user["messages"][0]
        bad_attachments = native_document()
        bad_attachments["messages"][0]["metadata"]["attachments"] = None
        bad_pointer = native_document()
        bad_pointer["messages"][0]["content"]["parts"][0]["asset_pointer"] = "unsupported://file-input"
        bad_branch = native_document()
        bad_branch["messages"] = "not-a-message-list"
        return ((wrong_user, "submission_mismatch"), (missing_user, "missing_submission"),
                (bad_attachments, "input_assets_invalid"), (bad_pointer, "input_assets_invalid"),
                (bad_branch, "invalid_branch"))

    def test_bad_input_evidence_keeps_original_document_and_conversation_identity(self):
        for document, reason in self.invalid_cases():
            with self.subTest(reason=reason):
                original = copy.deepcopy(document)
                bound = bind_input_assets(document, delivery_context())
                self.assertEqual(original, bound,
                                 "证据缺失应保留原文档，不能返回 {} 导致 conversation_mismatch")
                self.assertEqual(original, document, "校验失败不能修改上游证据")

    def test_each_input_evidence_failure_has_its_own_reason(self):
        for document, reason in self.invalid_cases():
            with self.subTest(reason=reason):
                bound, diagnostic = self.bind_with_reason(document, delivery_context())
                self.assertEqual(reason, diagnostic.get("reason"))
                self.assertEqual("conversation-1", bound["conversation_id"])

    def test_non_document_reports_invalid_document(self):
        for document in (None, [], "not-a-document"):
            with self.subTest(document=document):
                _, diagnostic = self.bind_with_reason(document, delivery_context())
                self.assertEqual("invalid_document", diagnostic.get("reason"))

    def test_invalid_context_assets_are_distinct_from_upstream_input_metadata(self):
        for supplied in (None, "file-uploaded", ["bad/id"], [f"file-{index}" for index in range(21)]):
            with self.subTest(supplied=supplied):
                document = native_document()
                context = {**delivery_context(), "input_asset_ids": supplied}
                bound, diagnostic = self.bind_with_reason(document, context)
                self.assertEqual("context_assets_invalid", diagnostic.get("reason"))
                self.assertEqual(document, bound)

    def test_evidence_document_keeps_bad_input_and_actual_conversation_identity(self):
        for document, reason in self.invalid_cases()[:4]:
            if reason == "missing_submission":
                continue
            with self.subTest(reason=reason):
                evidence = ImageDeliveryEvidence()
                for message in document["messages"]:
                    evidence.observe(message, "conversation-1")
                bound = evidence.document("conversation-1", delivery_context())
                self.assertEqual(document, bound,
                                 "直播输入证据拒绝不能抹去已绑定的原会话 ID")

    def test_evidence_document_reports_input_failure_before_selection(self):
        self.assertIn("diagnostic", inspect.signature(ImageDeliveryEvidence.document).parameters,
                      "直播证据文档也必须提供精确诊断")
        for document, reason in self.invalid_cases()[:4]:
            if reason == "missing_submission":
                continue
            with self.subTest(reason=reason):
                evidence = ImageDeliveryEvidence()
                for message in document["messages"]:
                    evidence.observe(message, "conversation-1")
                diagnostic = {}
                bound = evidence.document("conversation-1", delivery_context(), diagnostic=diagnostic)
                self.assertEqual(reason, diagnostic.get("reason"))
                self.assertEqual("conversation-1", bound["conversation_id"])

    def test_valid_binding_adds_upload_identities_without_mutating_source(self):
        document = native_document()
        original = copy.deepcopy(document)
        bound = bind_input_assets(document, delivery_context())
        self.assertEqual(original, document)
        self.assertEqual([{"id": "file-input"}, {"id": "file-uploaded"}],
                         bound["messages"][0]["metadata"]["attachments"])
        selected = select_terminal_image_delivery(
            bound, conversation_id="conversation-1", submitted_message_id="user-1")
        self.assertIsNotNone(selected)
        self.assertEqual("image-1", selected.message_id)

    def test_bound_input_assets_remain_excluded_from_final_delivery(self):
        for asset in ("file-input", "file-uploaded"):
            with self.subTest(asset=asset):
                document = native_document()
                document["messages"][1] = image_result(pointer="file-service://" + asset)
                diagnostic = {}
                selected = select_terminal_image_delivery(
                    bind_input_assets(document, delivery_context()), conversation_id="conversation-1",
                    submitted_message_id="user-1", diagnostic=diagnostic)
                self.assertIsNone(selected)
                self.assertEqual("input_asset_selected", diagnostic.get("reason"))

    def test_real_foreign_document_is_rejected_by_conversation_selector(self):
        document = native_document()
        document["conversation_id"] = "foreign-conversation"
        bound = bind_input_assets(document, delivery_context())
        self.assertEqual("foreign-conversation", bound["conversation_id"])
        diagnostic = {}
        selected = select_terminal_image_delivery(
            bound, conversation_id="conversation-1", submitted_message_id="user-1", diagnostic=diagnostic)
        self.assertIsNone(selected)
        self.assertEqual("conversation_mismatch", diagnostic.get("reason"))


class DeliveryEvidenceReadbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ImageFinalContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(patch.object(conversation, "logger", Mock()))
        self.clock = FakeClock()
        self.enterContext(patch.object(conversation.time, "monotonic", self.clock.monotonic))
        self.enterContext(patch.object(conversation.time, "sleep", self.clock.sleep))
        self.expected = fixtures.png("blue")

    def marker_backend(self, invalid_input="attachments"):
        snapshot_rows = fixtures.revisions(2)
        # 空 final 的完成来自本轮 marker；GET 保留同一条原始消息，不补造终态。
        snapshot_rows[-1].update(status="in_progress", end_turn=False)
        live_rows = copy.deepcopy(snapshot_rows)
        if invalid_input == "attachments":
            live_rows[0]["metadata"]["attachments"] = None
        elif invalid_input == "pointer":
            live_rows[0]["content"]["parts"][0]["asset_pointer"] = "unsupported://" + fixtures.INPUT_ID
        elif invalid_input == "user":
            live_rows[0]["id"] = "foreign-submitted-user"
        else:
            raise AssertionError("unknown test mutation")
        backend = self.fixture.backend(live_rows, image_bytes={
            fixtures.file_id(1): fixtures.png("green"), fixtures.file_id(2): self.expected,
        })
        backend._image_input_asset_ids = [fixtures.INPUT_ID]
        backend._image_delivery_deadline = 104.0
        backend._post_image_conversation = Mock(side_effect=AssertionError("不得重放生成 POST"))
        backend._start_image_generation = Mock(side_effect=AssertionError("不得重启生成"))
        frames = list(backend.stream_conversation.return_value)[:-1]
        frames.extend([json.dumps({"type": "message_stream_complete", "conversation_id": fixtures.CID}), "[DONE]"])
        backend.stream_conversation.return_value = iter(frames)
        snapshot = {"conversation_id": fixtures.CID, "messages": snapshot_rows}
        backend._get_conversation.return_value = snapshot
        return backend, snapshot

    def assert_no_replay(self, backend):
        backend.stream_conversation.assert_called_once()
        backend._post_image_conversation.assert_not_called()
        backend._start_image_generation.assert_not_called()

    def assert_no_download(self, backend):
        backend._request_image_transfer.assert_not_called()
        backend.download_final_sandbox_images.assert_not_called()

    def rejection_reason(self, exception):
        return next((item.get("selection_reason") for item in exception.image_execution
                     if item.get("selection_reason")), "")

    def assert_original_readback(self, backend):
        backend._get_conversation.assert_called_once()
        arguments = backend._get_conversation.call_args
        self.assertEqual((fixtures.CID,), arguments.args)
        self.assertGreater(arguments.kwargs["timeout_secs"], 0)
        self.assertLessEqual(arguments.kwargs["timeout_secs"], 3.0)

    def test_bad_live_input_metadata_can_recover_unique_final_from_original_conversation(self):
        for invalid_input in ("attachments", "pointer"):
            with self.subTest(invalid_input=invalid_input):
                backend, _ = self.marker_backend(invalid_input)
                try:
                    result = self.fixture.run_result(backend)
                except conversation.ImageGenerationError as exc:
                    self.fail("原会话已具备正确 user 和输入资产，应允许 GET 补证后交付；"
                              f"实际 code={exc.code}, reason={self.rejection_reason(exc)}, "
                              f"GET次数={backend._get_conversation.call_count}")
                self.fixture.assert_only_image(result, self.expected)
                self.assert_original_readback(backend)
                self.assert_no_replay(backend)
                backend._request_image_transfer.assert_called_once()
                self.assertIn(fixtures.file_id(2), backend._request_image_transfer.call_args.args[1])

    def test_foreign_readback_conversation_cannot_deliver_or_download(self):
        backend, snapshot = self.marker_backend()
        snapshot["conversation_id"] = "foreign-conversation"
        with self.assertRaises(conversation.ImageGenerationError) as caught:
            self.fixture.run_result(backend)
        self.assertEqual("conversation_mismatch", self.rejection_reason(caught.exception))
        self.assert_original_readback(backend)
        self.assert_no_download(backend)
        self.assert_no_replay(backend)

    def test_readback_of_another_submission_cannot_deliver_old_image(self):
        backend, snapshot = self.marker_backend()
        snapshot["messages"][0]["id"] = "older-submitted-user"
        with self.assertRaises(conversation.ImageGenerationError) as caught:
            self.fixture.run_result(backend)
        self.assertEqual("submission_mismatch", self.rejection_reason(caught.exception))
        self.assert_original_readback(backend)
        self.assert_no_download(backend)
        self.assert_no_replay(backend)

    def test_live_wrong_submission_is_rechecked_against_original_history(self):
        backend, _ = self.marker_backend("user")
        result = self.fixture.run_result(backend)
        self.fixture.assert_only_image(result, self.expected)
        self.assert_original_readback(backend)
        self.assert_no_replay(backend)


if __name__ == "__main__":
    unittest.main()
