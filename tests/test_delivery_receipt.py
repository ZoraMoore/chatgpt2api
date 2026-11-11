import copy
import json
import unittest
from unittest.mock import patch

from services.image_task_service import _validated_delivery_context, _public_task
from services.protocol import image_delivery
from services.protocol.terminal_image_delivery import select_terminal_image_delivery
from tests.test_terminal_image_delivery import native_document


class DeliveryReceiptTests(unittest.TestCase):
    def test_live_completion_can_reconcile_history_omitting_only_empty_final(self):
        from services.protocol import conversation
        from tests import test_image_final_contract as fixtures
        fixture = fixtures.ImageFinalContractTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        rows = fixtures.revisions(1)
        rows[-1].update(status='in_progress', end_turn=False)
        historical = {'conversation_id': fixtures.CID, 'messages': copy.deepcopy(rows[:-1])}
        rows[0]['metadata']['attachments'] = None
        expected = fixtures.png('blue')
        backend = fixture.backend(rows, image_bytes={fixtures.file_id(1): expected})
        frames = list(backend.stream_conversation.return_value)
        frames.insert(-1, json.dumps({'type': 'message_stream_complete', 'conversation_id': fixtures.CID}))
        backend.stream_conversation.return_value = iter(frames)
        backend._get_conversation.return_value = historical
        with patch.object(conversation.time, 'sleep', lambda seconds: None):
            result = fixture.run_result(backend)
        fixture.assert_only_image(result, expected)
        backend._get_conversation.assert_called_once()

    def receipt(self):
        factory = getattr(image_delivery, "make_completion_receipt", None)
        self.assertIsNotNone(factory, "需要持久化可核验的原轮次完成证明")
        document = native_document()
        selected = select_terminal_image_delivery(document, conversation_id="conversation-1", submitted_message_id="user-1")
        return factory(document, "user-1", selected)

    def test_internal_context_preserves_bounded_completion_receipt(self):
        receipt = {"version": 1, "conversation_id": "conversation-1", "submitted_message_id": "user-1",
                   "message_id": "image-1", "asset_id": "file-output", "anchor_id": "final",
                   "work_sha256": "a" * 64, "anchor_sha256": "b" * 64}
        context = {"submitted_message_id": "user-1", "input_asset_ids": ["file-input"],
                   "input_image_hashes": [], "completion_receipt": receipt}
        self.assertEqual(context, _validated_delivery_context(context))
        self.assertNotIn("completion_receipt", str(_public_task({"id": "task", "delivery_context": context})))

    def test_receipt_cannot_store_credentials_or_foreign_submission(self):
        for mutation in ({"token": "must-not-persist"}, {"submitted_message_id": "other-user"}, {"version": True}):
            receipt = {"version": 1, "conversation_id": "conversation-1", "submitted_message_id": "user-1",
                       "message_id": "image-1", "asset_id": "file-output", "anchor_id": "final",
                       "work_sha256": "a" * 64, "anchor_sha256": "b" * 64, **mutation}
            self.assertEqual({}, _validated_delivery_context({"submitted_message_id": "user-1",
                "input_asset_ids": [], "input_image_hashes": [], "completion_receipt": receipt}))

    def test_stored_proof_can_restore_native_artifact_when_empty_final_was_not_persisted(self):
        receipt = self.receipt()
        document = native_document()
        document["messages"].pop()
        selected = select_terminal_image_delivery(document, conversation_id="conversation-1",
            submitted_message_id="user-1", completion_receipt=receipt)
        self.assertIsNotNone(selected)
        self.assertEqual("image-1", selected.message_id)

    def test_work_changes_or_newer_final_invalidate_stored_proof(self):
        receipt = self.receipt()
        for mutation in ("asset", "failure", "new_user", "final_revision"):
            with self.subTest(mutation=mutation):
                doc = native_document()
                if mutation == "asset":
                    doc["messages"][1]["content"]["parts"][0]["asset_pointer"] = "sediment://changed"
                elif mutation == "failure":
                    doc["messages"][1]["status"] = "failed"
                elif mutation == "new_user":
                    new = copy.deepcopy(doc["messages"][0]); new["id"] = "other-user"; doc["messages"].append(new)
                else:
                    doc["messages"][-1].update(status="in_progress", end_turn=False)
                    doc["messages"][-1]["content"]["parts"] = ["继续修订"]
                self.assertFalse(image_delivery.receipt_matches(receipt, doc, "user-1"))


if __name__ == "__main__":
    unittest.main()
