import unittest

from services.protocol.terminal_image_delivery import select_terminal_image_delivery
from tests.test_terminal_image_delivery import native_document, image_result, python_document
from tests import test_image_final_contract as fixtures


ACKNOWLEDGEMENTS = (
    "图片已生成。",
    "图片处理完成",
    '<text color="secondary" size="xs"> </text>\n',
    '<text color="success" weight="medium" size="sm">图片处理完成</text>',
    '<badge color="success">图片处理完成</badge>',
    '<badge color="success"><icon name="check-circle" size="xs" inline/> 图片已生成</badge>',
    '<badge color="success">图片已生成</badge>',
    "Done",
)


class FinalCompletionAckTests(unittest.TestCase):
    def select(self, doc):
        return select_terminal_image_delivery(doc, conversation_id="conversation-1", submitted_message_id="user-1")

    def test_observed_completion_text_selects_latest_unique_generated_image(self):
        for text in ACKNOWLEDGEMENTS:
            with self.subTest(text=text):
                doc = native_document()
                doc["messages"].insert(2, image_result("image-2", "sediment://file-second"))
                doc["messages"][-1]["content"]["parts"] = [text]
                selected = self.select(doc)
                self.assertIsNotNone(selected)
                self.assertEqual("image-2", selected.message_id)

    def test_completion_ack_preserves_verified_postprocess_selection(self):
        doc = python_document()
        doc["messages"][-1]["content"]["parts"] = ["图片处理完成"]
        result = self.select(doc)
        self.assertIsNotNone(result)
        self.assertEqual(("python_output", "/mnt/data/final.png"), (result.kind, result.path))

    def test_completion_words_do_not_override_incomplete_or_failed_final(self):
        for mutation in ({"end_turn": False}, {"status": "in_progress"}, {"metadata": {"error": "failed"}},
                         {"metadata": {"is_refusal": True}}):
            with self.subTest(mutation=mutation):
                doc = native_document()
                doc["messages"][-1]["content"]["parts"] = ["图片已生成"]
                doc["messages"][-1].update(mutation)
                self.assertIsNone(self.select(doc))

    def test_negative_or_ambiguous_text_still_blocks_completed_final(self):
        for text in ("图片尚未生成", "图片处理失败", "已完成，但不符合要求，不能作为最终图片交付。",
                     '<badge color="success">无法完成图片修改</badge>', "请选择第一张或第二张", "仍在处理"):
            with self.subTest(text=text):
                doc = native_document()
                doc["messages"][-1]["content"]["parts"] = [text]
                self.assertIsNone(self.select(doc))

    def test_unrecognized_display_text_does_not_replace_structured_completion_evidence(self):
        # 展示标记不参与执行或下载；成功终态和本轮唯一工具文件共同证明交付。
        for text in ('<script>图片已生成</script>', '<unknown></unknown>', '<badge>图片已生成',
                     '画像生成完了', '已按要求完成调整，文字的位置和背景颜色均已更新。'):
            with self.subTest(text=text):
                doc = native_document()
                doc["messages"][-1]["content"]["parts"] = [text]
                selected = self.select(doc)
                self.assertIsNotNone(selected)
                self.assertEqual(("native_image", "image-1"), (selected.kind, selected.message_id))
                self.assertEqual("sediment://file-output",
                                 selected.image_message["content"]["parts"][0]["asset_pointer"])

    def test_completion_ack_cannot_return_input_or_multiple_output_pointers(self):
        for mutation in ("input", "multiple"):
            with self.subTest(mutation=mutation):
                doc = native_document()
                doc["messages"][-1]["content"]["parts"] = ["图片已生成"]
                parts = doc["messages"][1]["content"]["parts"]
                if mutation == "input":
                    parts[0]["asset_pointer"] = "file-service://file-input"
                else:
                    parts.append({"content_type": "image_asset_pointer", "asset_pointer": "sediment://another-output"})
                self.assertIsNone(self.select(doc))

    def test_markup_cannot_hide_explicit_delivery_failure(self):
        doc = native_document()
        doc["messages"][-1]["content"]["parts"] = [
            '<text>无法</text><text>完成图片修改</text> [图片](sediment://file-output)']
        self.assertIsNone(self.select(doc))

    def test_conflicting_fallback_does_not_make_completion_ack_succeed(self):
        doc = native_document()
        doc["messages"][-1]["content"]["parts"] = ["图片已生成"]
        doc["messages"][-1]["metadata"] = {"model_dil_v2": {"fallbackMarkdown": "无法完成图片修改"}}
        self.assertIsNone(self.select(doc))

    def test_observed_parenthesized_fallback_is_a_completion_acknowledgement(self):
        for fallback in ("(图片处理完成)", "(图片已生成)", "（图片已生成）"):
            with self.subTest(fallback=fallback):
                doc = native_document()
                doc["messages"][-1]["content"]["parts"] = ['<badge color="success">图片已生成</badge>']
                doc["messages"][-1]["metadata"] = {"model_dil_v2": {"fallbackMarkdown": fallback}}
                self.assertIsNotNone(self.select(doc))


class FinalCompletionAckIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ImageFinalContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_completed_badge_delivers_one_image_without_readback_or_regeneration(self):
        rows = fixtures.revisions(3)
        rows[-1] = fixtures.final_message(ACKNOWLEDGEMENTS[5])
        images = {fixtures.file_id(i): fixtures.png((i * 30, 40, 80)) for i in range(1, 4)}
        backend = self.fixture.backend(rows, image_bytes=images)
        result = self.fixture.run_result(backend)
        self.fixture.assert_only_image(result, images[fixtures.file_id(3)])
        backend._get_conversation.assert_not_called()
        self.assertEqual(1, backend.stream_conversation.call_count)


if __name__ == "__main__":
    unittest.main()
