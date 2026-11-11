import copy
import unittest

from services.protocol.terminal_image_delivery import select_terminal_image_delivery
from tests.test_terminal_image_delivery import native_document, image_result


class FinalDeliverySelectionTests(unittest.TestCase):
    def select(self, doc):
        return select_terminal_image_delivery(doc, conversation_id="conversation-1", submitted_message_id="user-1")

    def test_explicit_final_selects_one_earlier_generated_asset(self):
        doc = native_document()
        doc["messages"].insert(2, image_result("image-2", "sediment://file-second"))
        doc["messages"][-1]["content"]["parts"] = ["[最终图片](file-service://file-output)"]
        selected = self.select(doc)
        self.assertIsNotNone(selected)
        self.assertEqual("image-1", selected.message_id)

    def test_final_sandbox_body_has_priority_over_reference_metadata(self):
        doc = native_document()
        doc["messages"][-1]["content"]["parts"] = ["[最终图片](sandbox:/mnt/data/final.png)"]
        doc["messages"][-1]["metadata"] = {"model_dil_v2": {
            "fallbackMarkdown": "[旧图](sandbox:/mnt/data/old.png)",
            "appData": {"reference": "sandbox:/mnt/data/input.png"}},
            "content_references": [{"path": "sandbox:/mnt/data/other.png"}]}
        selected = self.select(doc)
        self.assertIsNotNone(selected)
        self.assertEqual(("sandbox_final", "final", "/mnt/data/final.png"),
                         (selected.kind, selected.message_id, selected.path))

    def test_explicit_input_asset_is_never_selected(self):
        doc = native_document()
        doc["messages"][-1]["content"]["parts"] = ["[最终图片](file-service://file-input)"]
        self.assertIsNone(self.select(doc))

    def test_explicit_final_cannot_override_delivery_failure(self):
        doc = native_document()
        doc["messages"][-1]["content"]["parts"] = [
            "本次生成未能满足修改要求，因此不能作为合格图片交付。[图片](sediment://file-output)"]
        self.assertIsNone(self.select(doc))

    def test_multiple_final_references_are_ambiguous(self):
        doc = native_document()
        doc["messages"].insert(2, image_result("image-2", "sediment://file-second"))
        doc["messages"][-1]["content"]["parts"] = [
            "[图一](sediment://file-output) [图二](sediment://file-second)"]
        self.assertIsNone(self.select(doc))

    def test_same_final_asset_with_two_schemes_is_one_output(self):
        doc = native_document()
        doc["messages"][-1]["content"]["parts"] = [
            "[图片](file-service://file-output) [下载](sediment://file-output)"]
        selected = self.select(doc)
        self.assertIsNotNone(selected)
        self.assertEqual("image-1", selected.message_id)

    def test_no_channel_with_explicit_end_turn_is_supported(self):
        doc = native_document()
        doc["messages"][-1].pop("channel")
        selected = self.select(doc)
        self.assertIsNotNone(selected)
        self.assertEqual("image-1", selected.message_id)

    def test_later_failed_processing_does_not_fall_back_to_explicit_old_image(self):
        doc = native_document()
        failed = copy.deepcopy(doc["messages"][1])
        failed.update(id="failed-processing", status="failed", author={"role": "tool", "name": "python"})
        doc["messages"].insert(-1, failed)
        doc["messages"][-1]["content"]["parts"] = ["[图片](sediment://file-output)"]
        self.assertIsNone(self.select(doc))


if __name__ == "__main__":
    unittest.main()
