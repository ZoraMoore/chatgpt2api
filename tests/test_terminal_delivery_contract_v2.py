from __future__ import annotations

import copy
import unittest
from urllib.parse import urlsplit

from services.protocol.conversation import ImageGenerationError
from services.protocol.image_delivery import make_completion_receipt, receipt_matches
from services.protocol.terminal_image_delivery import select_terminal_image_delivery
from tests import test_image_final_contract as fixtures
from tests.test_terminal_image_delivery import python_pair


COMPLETED_EXPLANATIONS = (
    "已生成图片",
    "已为你生成图片。",
    "图片已根据参考图完成修改，主体构图和色调保持一致；你可以查看本次最终结果。",
    "ご指定の参考画像に合わせて、構図と色合いを維持したまま編集しました。完成した画像をご確認ください。",
)
FINAL_PATH = "/mnt/data/final.png"


def inspection_pair(*, failed=False):
    return [
        fixtures.message(
            "view-call", recipient="container.open_image", channel="analysis",
            content={"content_type": "code", "text": '{"path":"/mnt/data/preview.png"}'},
        ),
        fixtures.message(
            "view-result", "tool", name="container.open_image",
            status="error" if failed else "finished_successfully",
            content={"content_type": "execution_output", "text": "Synthetic image inspection"},
            metadata={"is_complete": True},
        ),
    ]


class TerminalDeliveryContractV2Tests(unittest.TestCase):
    def setUp(self):
        # 复用隔离配置和图片夹具；不模拟选择器或SSE解析器本身。
        self.fixture = fixtures.ImageFinalContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def select(self, rows):
        return select_terminal_image_delivery(
            {"conversation_id": fixtures.CID, "messages": copy.deepcopy(rows)},
            conversation_id=fixtures.CID, submitted_message_id=fixtures.SUBMITTED,
        )

    def run_success(self, backend):
        try:
            return self.fixture.run_result(backend)
        except ImageGenerationError as exc:
            self.fail(f"完整成功轮次未交付唯一产物：{exc.code}: {exc}")

    def assert_native(self, rows, expected_id):
        selected = self.select(rows)
        self.assertIsNotNone(selected, "完整final的普通说明不应使唯一生产产物失效")
        self.assertEqual("native_image", selected.kind)
        self.assertEqual(expected_id, selected.message_id)

    def test_complete_final_explanations_do_not_require_a_completion_phrase_whitelist(self):
        for text in COMPLETED_EXPLANATIONS:
            with self.subTest(text=text):
                rows = fixtures.revisions(3)
                rows[-1] = fixtures.final_message(text)
                self.assert_native(rows, "image-result-3")

    def test_complete_chinese_and_japanese_explanations_deliver_last_image_through_real_stream(self):
        for text in COMPLETED_EXPLANATIONS:
            with self.subTest(text=text):
                rows = fixtures.revisions(3)
                rows[-1] = fixtures.final_message(text)
                images = {fixtures.file_id(i): fixtures.png((i * 30, 40, 80)) for i in range(1, 4)}
                backend = self.fixture.backend(rows, image_bytes=images)
                self.fixture.assert_only_image(self.run_success(backend), images[fixtures.file_id(3)])
                self.assertEqual(1, backend.stream_conversation.call_count)

    def test_async_image_placeholder_component_uses_unique_tool_asset_without_external_download(self):
        rows = fixtures.revisions(1)
        rows[-1] = fixtures.final_message(
            '图片已生成。<AsyncImage src="https://placeholder.invalid/generated.png" alt="最终图片" />'
        )
        expected = fixtures.png("blue")
        backend = self.fixture.backend(rows, image_bytes={fixtures.file_id(1): expected})
        self.fixture.assert_only_image(self.run_success(backend), expected)
        self.assertTrue(backend._request_image_transfer.called)
        for transfer in backend._request_image_transfer.call_args_list:
            url = transfer.args[1]
            self.assertEqual("example.invalid", urlsplit(url).hostname)
            self.assertNotIn("placeholder.invalid", url)
        backend.download_final_sandbox_images.assert_not_called()

    def test_two_explicit_valid_artifact_references_remain_ambiguous(self):
        rows = fixtures.revisions(2)
        rows[-1] = fixtures.final_message(
            f"[图一](sediment://{fixtures.file_id(1)}) [图二](sediment://{fixtures.file_id(2)})"
        )
        self.assertIsNone(self.select(rows))
        backend = self.fixture.backend(rows, image_bytes={
            fixtures.file_id(1): fixtures.png("blue"), fixtures.file_id(2): fixtures.png("green"),
        })
        with self.assertRaises(ImageGenerationError):
            self.fixture.run_result(backend)
        backend._request_image_transfer.assert_not_called()

    def test_native_production_result_survives_later_successful_image_inspection(self):
        rows = fixtures.revisions(1)
        rows[-1:-1] = inspection_pair()
        self.assert_native(rows, "image-result-1")
        expected = fixtures.png("blue")
        backend = self.fixture.backend(rows, image_bytes={fixtures.file_id(1): expected})
        self.fixture.assert_only_image(self.run_success(backend), expected)
        backend.download_final_sandbox_images.assert_not_called()

    def test_python_production_result_survives_later_successful_image_inspection(self):
        rows = fixtures.revisions(1)
        rows[-1:-1] = [*python_pair(), *inspection_pair()]
        selected = self.select(rows)
        self.assertIsNotNone(selected, "查看已完成文件不能使该文件丢失生产身份")
        self.assertEqual(("python_output", "python-result", FINAL_PATH),
                         (selected.kind, selected.message_id, selected.path))
        expected = fixtures.png("blue")
        backend = self.fixture.backend(
            rows, image_bytes={fixtures.file_id(1): fixtures.png("yellow")},
            sandbox_bytes={FINAL_PATH: expected},
        )
        self.fixture.assert_only_image(self.run_success(backend), expected)
        backend.download_final_sandbox_images.assert_called_once_with(fixtures.CID, "python-result", [FINAL_PATH])
        backend._request_image_transfer.assert_not_called()

    def test_successful_plain_text_summary_before_complete_final_preserves_production_result(self):
        rows = fixtures.revisions(1)
        rows.insert(-1, fixtures.message(
            "completed-summary", channel="commentary",
            content={"content_type": "text", "parts": ["我已完成图片调整，下面是本次生成的最终结果。"]},
        ))
        self.assert_native(rows, "image-result-1")
        expected = fixtures.png("blue")
        backend = self.fixture.backend(rows, image_bytes={fixtures.file_id(1): expected})
        self.fixture.assert_only_image(self.run_success(backend), expected)

    def test_refusal_failed_production_and_explicit_input_reference_remain_rejected(self):
        for mutation in ("refusal", "failed_production", "input_reference"):
            with self.subTest(mutation=mutation):
                rows = fixtures.revisions(1)
                if mutation == "refusal":
                    rows[-1] = fixtures.final_message("本次生成未能满足修改要求，因此不能作为合格图片交付。")
                elif mutation == "failed_production":
                    rows[2]["status"] = "failed"
                    rows[-1] = fixtures.final_message("已生成图片")
                else:
                    rows[-1] = fixtures.final_message(f"[原图](sediment://{fixtures.INPUT_ID})")
                self.assertIsNone(self.select(rows))
                backend = self.fixture.backend(rows, image_bytes={fixtures.file_id(1): fixtures.png("blue")})
                with self.assertRaises(ImageGenerationError):
                    self.fixture.run_result(backend)
                backend._request_image_transfer.assert_not_called()

    def test_input_bytes_under_a_new_asset_id_are_still_rejected(self):
        rows = [fixtures.user_message(), fixtures.image_call(1),
                fixtures.image_result(1, fixtures.ALIAS_ID), fixtures.final_message("图片已生成")]
        backend = self.fixture.backend(rows, image_bytes={fixtures.ALIAS_ID: fixtures.INPUT_BYTES})
        with self.assertRaises(ImageGenerationError):
            self.fixture.run_result(backend)

    def test_multiple_pointers_in_last_production_result_are_not_reduced_to_first_image(self):
        rows = [fixtures.user_message(), fixtures.image_call(1),
                fixtures.image_result(1, fixtures.file_id(1), fixtures.file_id(2)), fixtures.final_message()]
        self.assertIsNone(self.select(rows))
        backend = self.fixture.backend(rows, image_bytes={
            fixtures.file_id(1): fixtures.png("blue"), fixtures.file_id(2): fixtures.png("green"),
        })
        with self.assertRaises(ImageGenerationError):
            self.fixture.run_result(backend)
        backend._request_image_transfer.assert_not_called()

    def test_failed_inspection_does_not_establish_successful_delivery(self):
        rows = fixtures.revisions(1)
        rows[-1:-1] = inspection_pair(failed=True)
        self.assertIsNone(self.select(rows))
        backend = self.fixture.backend(rows, image_bytes={fixtures.file_id(1): fixtures.png("blue")})
        with self.assertRaises(ImageGenerationError):
            self.fixture.run_result(backend)
        backend._request_image_transfer.assert_not_called()

    def test_explicit_failure_in_display_fallback_is_not_hidden_by_positive_body(self):
        rows = fixtures.revisions(1)
        rows[-1] = fixtures.final_message(
            "图片已生成", metadata={"model_dil_v2": {"fallbackMarkdown": "无法完成图片修改"}},
        )
        self.assertIsNone(self.select(rows))
        backend = self.fixture.backend(rows, image_bytes={fixtures.file_id(1): fixtures.png("blue")})
        with self.assertRaises(ImageGenerationError):
            self.fixture.run_result(backend)
        backend._request_image_transfer.assert_not_called()

    def test_async_image_component_cannot_hide_failure_split_across_display_tags(self):
        rows = fixtures.revisions(1)
        rows[-1] = fixtures.final_message(
            '<AsyncImage src="https://placeholder.invalid/generated.png"/>'
            '<text>无法</text><text>完成图片修改</text>'
        )
        self.assertIsNone(self.select(rows))
        backend = self.fixture.backend(rows, image_bytes={fixtures.file_id(1): fixtures.png("blue")})
        with self.assertRaises(ImageGenerationError):
            self.fixture.run_result(backend)
        backend._request_image_transfer.assert_not_called()

    def test_explicit_japanese_generation_failure_is_not_a_successful_explanation(self):
        rows = fixtures.revisions(1)
        rows[-1] = fixtures.final_message("ご要望どおりの画像を生成できませんでした。")
        self.assertIsNone(self.select(rows))
        backend = self.fixture.backend(rows, image_bytes={fixtures.file_id(1): fixtures.png("blue")})
        with self.assertRaises(ImageGenerationError):
            self.fixture.run_result(backend)
        backend._request_image_transfer.assert_not_called()

    def test_saved_receipt_preserves_explicitly_selected_earlier_asset_when_final_is_omitted(self):
        rows = fixtures.revisions(2)
        rows[-1] = fixtures.final_message(f"[最终图片](sediment://{fixtures.file_id(1)})")
        document = {"conversation_id": fixtures.CID, "messages": rows}
        original = self.select(rows)
        self.assertIsNotNone(original)
        self.assertEqual("image-result-1", original.message_id)
        receipt = make_completion_receipt(document, fixtures.SUBMITTED, original)
        self.assertEqual(fixtures.file_id(1), receipt["asset_id"])
        # 历史接口可以省略展示final；已确认的图片身份不能随之从A变成后来的B。
        snapshot = {"conversation_id": fixtures.CID, "messages": copy.deepcopy(rows[:-1])}
        restored = select_terminal_image_delivery(
            snapshot, conversation_id=fixtures.CID, submitted_message_id=fixtures.SUBMITTED,
            completion_receipt=receipt,
        )
        self.assertIsNotNone(restored)
        self.assertEqual(original.message_id, restored.message_id)
        self.assertEqual(original.image_message["content"]["parts"][0]["asset_pointer"],
                         restored.image_message["content"]["parts"][0]["asset_pointer"])

    def test_successful_thought_cannot_revalidate_a_receipt_after_its_final_anchor_changed(self):
        rows = fixtures.revisions(1)
        document = {"conversation_id": fixtures.CID, "messages": rows}
        original = self.select(rows)
        self.assertIsNotNone(original)
        receipt = make_completion_receipt(document, fixtures.SUBMITTED, original)
        changed = copy.deepcopy(rows)
        changed[-1].update(status="in_progress", end_turn=False)
        changed[-1]["content"]["parts"] = ["正在继续调整，尚未交付最终结果。"]
        changed.append(fixtures.message(
            "later-thought", content={"content_type": "thoughts", "thoughts": []},
        ))
        snapshot = {"conversation_id": fixtures.CID, "messages": changed}
        self.assertFalse(receipt_matches(receipt, snapshot, fixtures.SUBMITTED))
        self.assertIsNone(select_terminal_image_delivery(
            snapshot, conversation_id=fixtures.CID, submitted_message_id=fixtures.SUBMITTED,
            completion_receipt=receipt,
        ))


if __name__ == "__main__":
    unittest.main()
