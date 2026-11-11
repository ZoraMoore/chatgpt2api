"""提示词政策拒绝沿用终态与直接表述边界，不能误收已有草图。"""
from __future__ import annotations

import copy
import unittest

from services.protocol.terminal_image_delivery import final_delivery_rejected, select_terminal_image_delivery
from tests.test_terminal_image_delivery import native_document
from tests.test_terminal_refusal_classification import final_message, policy_snapshot


# 十一条真实漏判均使用此文案；只保留正文，不保留账号或会话身份。
PROMPT_POLICY_REFUSAL = "非常抱歉，该提示可能违反了我们的内容政策。如果你认为此判断有误，请重试或修改提示语。"


class PolicyRefusalVariantTests(unittest.TestCase):
    def select(self, document):
        diagnostic = {}
        selected = select_terminal_image_delivery(
            document, conversation_id="conversation-1", submitted_message_id="user-1",
            diagnostic=diagnostic)
        return selected, diagnostic.get("reason")

    def test_observed_prompt_refusal_stops_pending_tool_call(self):
        for with_thought in (False, True):
            with self.subTest(with_thought=with_thought):
                document = policy_snapshot(with_thought)
                document["messages"][-1]["content"]["parts"] = [PROMPT_POLICY_REFUSAL]
                original = copy.deepcopy(document)
                selected, reason = self.select(document)
                self.assertIsNone(selected)
                self.assertEqual("content_policy_rejected", reason)
                self.assertTrue(final_delivery_rejected(document["messages"][-1]))
                self.assertEqual(original, document)

    def test_explicit_prompt_refusal_blocks_native_and_sandbox_drafts(self):
        for text in (
            PROMPT_POLICY_REFUSAL,
            "抱歉，此提示词可能违反了内容政策，请修改后重试。",
            "抱歉，该请求违反了我们的安全政策，请修改提示语。",
        ):
            for link in ("[草图](sediment://file-output)", "[草图](sandbox:/mnt/data/draft.png)"):
                with self.subTest(text=text, link=link):
                    document = native_document()
                    document["messages"][-1] = final_message(text + "\n" + link)
                    selected, reason = self.select(document)
                    self.assertIsNone(selected)
                    self.assertEqual("content_policy_rejected", reason)

    def test_prompt_refusal_requires_a_completed_external_assistant_message(self):
        for mutation in (
            {"author": {"role": "user"}},
            {"author": {"role": "tool", "name": "image_alias.generate"}},
            {"recipient": "image_alias.generate"},
            {"channel": "analysis"},
            {"channel": "commentary", "end_turn": False},
            {"channel": "commentary", "status": "in_progress"},
            {"content": {"content_type": "thoughts", "parts": [PROMPT_POLICY_REFUSAL]}},
            {"content": {"content_type": "code", "text": PROMPT_POLICY_REFUSAL}},
        ):
            with self.subTest(mutation=mutation):
                final = final_message(PROMPT_POLICY_REFUSAL)
                final.update(mutation)
                self.assertFalse(final_delivery_rejected(final, completed=True))

    def test_nonterminal_prompt_commentary_remains_pending(self):
        for mutation in ({"end_turn": False}, {"status": "in_progress"}):
            with self.subTest(mutation=mutation):
                document = policy_snapshot()
                document["messages"][-1]["content"]["parts"] = [PROMPT_POLICY_REFUSAL]
                document["messages"][-1].update(mutation)
                selected, reason = self.select(document)
                self.assertIsNone(selected)
                self.assertEqual("calls_pending", reason)

    def test_quotes_negations_and_policy_promises_preserve_successful_delivery(self):
        for text in (
            f"上次回复：“{PROMPT_POLICY_REFUSAL}”\n本次图片已生成。",
            f"> {PROMPT_POLICY_REFUSAL}\n\n本次图片已生成。",
            f"```text\n{PROMPT_POLICY_REFUSAL}\n```\n本次图片已生成。",
            f"<blockquote>{PROMPT_POLICY_REFUSAL}</blockquote>本次图片已生成。",
            f"<q>{PROMPT_POLICY_REFUSAL}</q>本次图片已生成。",
            "此前该提示可能违反了我们的内容政策；本次图片已生成。",
            "不是说该提示可能违反了我们的内容政策，本次图片已生成。",
            "该提示没有违反我们的内容政策，本次图片已生成。",
            "此提示词不违反我们的内容政策，本次图片已生成。",
            "如果该提示可能违反了我们的内容政策，请先修改；本次图片已生成。",
            "用户要求请勿生成违反政策的图片；本次图片已生成。",
            "请勿生成违反政策的图片；本次图片已生成。",
            "我会遵守内容政策，不生成违规图片；本次图片已生成。",
        ):
            with self.subTest(text=text):
                document = native_document()
                document["messages"][-1] = final_message(text)
                self.assertFalse(final_delivery_rejected(document["messages"][-1]))
                selected, reason = self.select(document)
                self.assertIsNotNone(selected)
                self.assertEqual("ready", reason)

    def test_prompt_refusal_in_display_fallback_and_tags_is_still_terminal(self):
        for mode in ("fallback", "tags"):
            with self.subTest(mode=mode):
                document = native_document()
                if mode == "fallback":
                    final = final_message("图片已生成", metadata={
                        "model_dil_v2": {"fallbackMarkdown": PROMPT_POLICY_REFUSAL}})
                else:
                    final = final_message("".join(f"<text>{char}</text>" for char in PROMPT_POLICY_REFUSAL))
                document["messages"][-1] = final
                selected, reason = self.select(document)
                self.assertIsNone(selected)
                self.assertEqual("content_policy_rejected", reason)


if __name__ == "__main__":
    unittest.main()
