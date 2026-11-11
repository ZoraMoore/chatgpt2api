"""明确的本轮工具失败终态应停止补查，并保留策略、引用和完成状态边界。"""
from __future__ import annotations

import copy
import unittest

from services.protocol.terminal_image_delivery import final_delivery_rejected, select_terminal_image_delivery
from tests.test_policy_refusal_variants import PROMPT_POLICY_REFUSAL
from tests.test_terminal_image_delivery import native_document
from tests.test_terminal_refusal_classification import final_message, policy_snapshot


# 只保留已观察到的回复正文，不保留账号或会话身份。
TERMINAL_TOOL_FAILURE = "申し分なく按要求完成了日文化方案，但本次图片生成工具发生错误，未能成功输出处理后的图片。"


class TerminalGenerationFailureTests(unittest.TestCase):
    def select(self, document):
        diagnostic = {}
        selected = select_terminal_image_delivery(
            document, conversation_id="conversation-1", submitted_message_id="user-1",
            diagnostic=diagnostic,
        )
        return selected, diagnostic.get("reason")

    def test_observed_terminal_tool_failure_stops_pending_calls(self):
        for with_thought in (False, True):
            for mode in ("plain", "tags", "fallback"):
                with self.subTest(with_thought=with_thought, mode=mode):
                    document = policy_snapshot(with_thought)
                    final = document["messages"][-1]
                    final["content"]["parts"] = [TERMINAL_TOOL_FAILURE]
                    if mode == "tags":
                        final["content"]["parts"] = ["".join(
                            f"<text>{char}</text>" for char in TERMINAL_TOOL_FAILURE)]
                    elif mode == "fallback":
                        final["content"]["parts"] = ["方案已完成"]
                        final["metadata"] = {"model_dil_v2": {"fallbackMarkdown": TERMINAL_TOOL_FAILURE}}
                    original = copy.deepcopy(document)
                    selected, reason = self.select(document)
                    self.assertIsNone(selected)
                    self.assertEqual("generation_failed", reason)
                    self.assertTrue(final_delivery_rejected(final))
                    self.assertEqual(original, document)

    def test_current_generation_editing_and_processing_failures_block_drafts(self):
        for text in (
            TERMINAL_TOOL_FAILURE,
            "当前图片编辑工具发生错误，未能生成处理后的图片。",
            "本次图像处理工具失败，未能成功交付图像。",
        ):
            for link in ("[草图](sediment://file-output)", "[草图](sandbox:/mnt/data/draft.png)"):
                with self.subTest(text=text, link=link):
                    document = native_document()
                    document["messages"][-1] = final_message(text + "\n" + link)
                    selected, reason = self.select(document)
                    self.assertIsNone(selected)
                    self.assertEqual("generation_failed", reason)

    def test_failure_requires_a_completed_external_assistant_message(self):
        for mutation in (
            {"author": {"role": "user"}},
            {"author": {"role": "tool", "name": "image_alias.generate"}},
            {"recipient": "image_alias.generate"},
            {"channel": "analysis"},
            {"channel": "commentary", "end_turn": False},
            {"channel": "commentary", "status": "in_progress"},
            {"content": {"content_type": "thoughts", "parts": [TERMINAL_TOOL_FAILURE]}},
            {"content": {"content_type": "code", "text": TERMINAL_TOOL_FAILURE}},
        ):
            with self.subTest(mutation=mutation):
                final = final_message(TERMINAL_TOOL_FAILURE)
                final.update(mutation)
                self.assertFalse(final_delivery_rejected(final, completed=True))
        for mutation in ({"end_turn": False}, {"status": "in_progress"}):
            with self.subTest(nonterminal_commentary=mutation):
                document = policy_snapshot()
                document["messages"][-1]["content"]["parts"] = [TERMINAL_TOOL_FAILURE]
                document["messages"][-1].update(mutation)
                selected, reason = self.select(document)
                self.assertIsNone(selected)
                self.assertEqual("calls_pending", reason)

    def test_quoted_negated_and_conditional_errors_preserve_successful_delivery(self):
        for text in (
            f"上次回复：“{TERMINAL_TOOL_FAILURE}”\n本次图片已生成。",
            f"> {TERMINAL_TOOL_FAILURE}\n\n本次图片已生成。",
            f"```text\n{TERMINAL_TOOL_FAILURE}\n```\n本次图片已生成。",
            f"<blockquote>{TERMINAL_TOOL_FAILURE}</blockquote>本次图片已生成。",
            "此前本次图片生成工具发生错误，未能成功输出处理后的图片；当前图片已完成。",
            "不是说本次图片生成工具发生错误，未能成功输出处理后的图片；本次图片已完成。",
            "若工具发生错误请重试；本次图片已生成。",
            "若本次图片生成工具发生错误，未能成功输出处理后的图片，请重试；本次图片已生成。",
            "工具未发生错误，图片已输出。",
            "本次图片生成工具没有发生错误，图片已输出。",
            "本次图片生成工具发生错误，但并非未能成功输出图片，修复后图片已输出。",
        ):
            with self.subTest(text=text):
                document = native_document()
                document["messages"][-1] = final_message(text)
                self.assertFalse(final_delivery_rejected(document["messages"][-1]))
                selected, reason = self.select(document)
                self.assertIsNotNone(selected)
                self.assertEqual("ready", reason)

    def test_content_policy_rejection_keeps_priority_over_technical_failure(self):
        document = policy_snapshot()
        document["messages"][-1]["content"]["parts"] = [
            PROMPT_POLICY_REFUSAL + "\n" + TERMINAL_TOOL_FAILURE]
        selected, reason = self.select(document)
        self.assertIsNone(selected)
        self.assertEqual("content_policy_rejected", reason)



class TerminalGenerationFailurePipelineTests(unittest.TestCase):
    def test_real_stream_stops_at_explicit_tool_error_without_gets_or_draft_download(self):
        from tests import test_image_final_contract as fixtures
        from services.protocol.conversation import ImageGenerationError
        fixture = fixtures.ImageFinalContractTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        text = "申し分なく按要求完成了日文化方案，但本次图片生成工具发生错误，未能成功输出处理后的图片。"
        rows = [fixtures.user_message(), fixtures.image_call(1), fixtures.final_message(text)]
        rows[-1]["channel"] = "commentary"
        backend = fixture.backend(rows)
        with self.assertRaises(ImageGenerationError) as caught:
            fixture.run_result(backend)
        self.assertEqual("image_generation_failed", caught.exception.code)
        self.assertEqual("generation_failed", caught.exception.image_execution[0]["selection_reason"])
        backend._get_conversation.assert_not_called()
        backend._request_image_transfer.assert_not_called()
        backend.download_final_sandbox_images.assert_not_called()
        self.assertEqual(1, backend.stream_conversation.call_count)

if __name__ == "__main__":
    unittest.main()
