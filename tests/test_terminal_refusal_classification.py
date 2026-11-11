"""已结束的对外拒绝必须终止补查；引用、分析和缺失终态仍不能当作交付结论。"""
from __future__ import annotations

import copy
import unittest

from services.protocol.terminal_image_delivery import final_delivery_rejected, select_terminal_image_delivery
from tests.test_terminal_image_delivery import image_result, message, native_document, user


# 来自 2026-11-11 只读审计的七条漏判回复；不保留账号、会话及文件身份。
POLICY_REFUSAL = "非常抱歉，生成的图片可能违反了关于与第三方内容相似性的防护限制。如果你认为此判断有误，请重试或修改提示语。"
QUALITY_REFUSALS = (
    "目前无法在严格保护圣诞树主体像素、同时无痕清除全部背景道具的条件下可靠完成。本次不将未达标图片作为成品交付。",
    "当前图片右上角的圆形细节放大图覆盖了圣诞树部分枝叶。由于无法确认被遮挡的真实枝条结构，按照主体真实性保护规则，不能强行补画并作为合格成品输出。",
    "当前图片无法在不破坏圣诞树底部枝叶及原有场景结构的前提下可靠完成无痕修复，因此不将存在修复痕迹的图片作为合格成品交付。",
    "当前素材无法在可靠保护圣诞树枝叶、树形轮廓及黑色花盆原始像素的前提下完成无痕清理，因此不输出不合格成品。",
    "当前图片无法在保证圣诞树及黑色花盆原始细节完全不变的前提下，可靠清除左侧书本和展示台。\n\n按照你的主体锁定规则，本次不交付有明显修复痕迹或商品主体被重绘的图片。",
)


def final_message(text, **fields):
    return message("final", "assistant", {"content_type": "text", "parts": [text]},
                   channel="final", end_turn=True, **fields)


def image_call():
    return message("image-call", "assistant", {"content_type": "code", "text": "synthetic"},
                   recipient="image_alias.generate", channel="commentary", end_turn=False)


def missing_final_snapshot(async_status=None):
    # 两条原始缺失 final 的审计摘要均为这五条消息；只替换身份和正文。
    return {"conversation_id": "conversation-1", "async_status": async_status, "messages": [
        user(), message("system", "system", {"content_type": "text", "parts": [""]}, end_turn=True),
        image_call(), image_result(),
        message("recap", "assistant", {"content_type": "reasoning_recap", "parts": []}, end_turn=False),
    ]}


def policy_snapshot(with_thought=False):
    document = missing_final_snapshot()
    del document["messages"][3]
    if with_thought:
        document["messages"].insert(2, message(
            "thought", "assistant", {"content_type": "thoughts", "parts": []},
            status="in_progress", end_turn=False))
    final = final_message(POLICY_REFUSAL)
    # 实际策略拒绝使用 commentary，但明确 end_turn=True 且消息已完成。
    final["channel"] = "commentary"
    document["messages"].append(final)
    return document


def quality_snapshot(text):
    document = missing_final_snapshot(4)
    document["messages"][-1:-1] = [
        message("postprocess-call", "assistant", {"content_type": "code", "text": "synthetic"},
                recipient="container.exec", end_turn=False),
        message("postprocess-result", "tool", {"content_type": "execution_output", "text": ""},
                author={"role": "tool", "name": "container.exec"}),
    ]
    document["messages"].append(final_message(text))
    return document


class TerminalRefusalClassificationTests(unittest.TestCase):
    def select(self, document, **kwargs):
        diagnostic = {}
        selected = select_terminal_image_delivery(
            document, conversation_id="conversation-1", submitted_message_id="user-1",
            diagnostic=diagnostic, **kwargs)
        return selected, diagnostic.get("reason")

    def test_two_observed_policy_refusals_override_unanswered_tool_call(self):
        for with_thought in (False, True):
            with self.subTest(with_thought=with_thought):
                document = policy_snapshot(with_thought)
                original = copy.deepcopy(document)
                selected, reason = self.select(document)
                self.assertIsNone(selected)
                self.assertEqual("content_policy_rejected", reason)
                self.assertTrue(final_delivery_rejected(document["messages"][-1]))
                self.assertEqual(original, document)

    def test_five_observed_quality_refusals_override_unconfirmed_postprocess(self):
        for index, text in enumerate(QUALITY_REFUSALS):
            with self.subTest(case=index):
                document = quality_snapshot(text)
                selected, reason = self.select(document)
                self.assertIsNone(selected)
                self.assertEqual("quality_rejected", reason)
                self.assertTrue(final_delivery_rejected(document["messages"][-1]))

    def test_refusal_always_blocks_an_explicit_draft_reference(self):
        for text, expected in ((POLICY_REFUSAL, "content_policy_rejected"),
                               *((text, "quality_rejected") for text in QUALITY_REFUSALS)):
            with self.subTest(expected=expected, text=text):
                document = native_document()
                document["messages"][-1] = final_message(text + "\n[草图](sediment://file-output)")
                selected, reason = self.select(document)
                self.assertIsNone(selected)
                self.assertEqual(expected, reason)

    def test_both_original_missing_final_snapshots_still_require_completion(self):
        for async_status in (None, 4):
            with self.subTest(async_status=async_status):
                selected, reason = self.select(missing_final_snapshot(async_status))
                self.assertIsNone(selected)
                self.assertEqual("completion_missing", reason)

    def test_nonterminal_commentary_refusal_remains_pending(self):
        for mutation in ({"end_turn": False}, {"status": "in_progress"}):
            with self.subTest(mutation=mutation):
                document = policy_snapshot()
                document["messages"][-1].update(mutation)
                selected, reason = self.select(document)
                self.assertIsNone(selected)
                self.assertEqual("calls_pending", reason)
                self.assertFalse(final_delivery_rejected(document["messages"][-1], completed=True))

    def test_only_external_assistant_text_is_classified(self):
        for mutation in (
            {"author": {"role": "user"}}, {"author": {"role": "tool", "name": "image_alias.generate"}},
            {"recipient": "image_alias.generate"}, {"channel": "analysis"},
            {"content": {"content_type": "code", "text": POLICY_REFUSAL}},
            {"content": {"content_type": "thoughts", "parts": [POLICY_REFUSAL]}},
        ):
            with self.subTest(mutation=mutation):
                final = final_message(POLICY_REFUSAL)
                final.update(mutation)
                self.assertFalse(final_delivery_rejected(final, completed=True))

    def test_quoted_or_negated_refusal_text_does_not_reject_a_successful_final(self):
        for text in (
            f"上一次提示：“{POLICY_REFUSAL}”\n本次图片已生成。",
            f"> {POLICY_REFUSAL}\n\n本次图片已生成。",
            f"```text\n{POLICY_REFUSAL}\n```\n本次图片已生成。",
            f"<blockquote>{POLICY_REFUSAL}</blockquote>本次图片已生成。",
            f"<q>{QUALITY_REFUSALS[3]}</q>本次图片已生成。",
            '用户要求是“不输出不合格成品”；本次图片已生成。',
            "本次并非不交付有明显修复痕迹的图片，图片已生成。",
            "本次不是无法完成图片修改，图片已生成。",
            "生成的图片没有违反关于第三方内容相似性的防护限制，图片已生成。",
            "不是说生成的图片可能违反了关于第三方内容相似性的防护限制，图片已生成。",
        ):
            with self.subTest(text=text):
                document = native_document()
                document["messages"][-1] = final_message(text)
                self.assertFalse(final_delivery_rejected(document["messages"][-1]))
                selected, reason = self.select(document)
                self.assertIsNotNone(selected)
                self.assertEqual("ready", reason)

    def test_quality_promises_do_not_reject_a_real_successful_artifact(self):
        for text in (
            "我不会交付有修复痕迹的图片；本次图片已生成。",
            "不输出不合格成品，当前图片已完成。",
            "本次不输出不合格成品，当前图片已完成。",
            "本次不交付有明显修复痕迹的图片；最终图片已完成。",
            "不将未达标图片作为成品交付；本次图片已生成。",
            "不能强行补画并作为合格成品输出；本次图片已完成。",
            "当前并非无法在保护主体像素的条件下完成修复；本次不输出不合格成品，图片已完成。",
            "如果无法在保护主体像素的条件下完成修复，就不输出不合格成品；本次图片已完成。",
            "此前无法在保护主体像素的条件下完成修复；现已完成。不输出不合格成品，当前图片已完成。",
        ):
            with self.subTest(text=text):
                document = native_document()
                document["messages"][-1] = final_message(text)
                self.assertFalse(final_delivery_rejected(document["messages"][-1]))
                selected, reason = self.select(document)
                self.assertIsNotNone(selected)
                self.assertEqual("ready", reason)

    def test_quality_withholding_needs_actual_current_failure_context(self):
        for text in (
            "目前无法在保护主体像素的条件下完成修复，因此不输出不合格成品。",
            "本次生成的图片未达标，因此不交付有修复痕迹的图片。",
        ):
            with self.subTest(text=text):
                document = native_document()
                document["messages"][-1] = final_message(text)
                selected, reason = self.select(document)
                self.assertIsNone(selected)
                self.assertEqual("quality_rejected", reason)

    def test_display_fallback_and_split_tags_preserve_refusal_reason(self):
        for text, expected in ((POLICY_REFUSAL, "content_policy_rejected"),
                               (QUALITY_REFUSALS[3], "quality_rejected")):
            for mode in ("fallback", "tags"):
                with self.subTest(expected=expected, mode=mode):
                    document = native_document()
                    if mode == "fallback":
                        document["messages"][-1] = final_message("图片已生成", metadata={
                            "model_dil_v2": {"fallbackMarkdown": text}})
                    else:
                        document["messages"][-1] = final_message("".join(f"<text>{char}</text>" for char in text))
                    selected, reason = self.select(document)
                    self.assertIsNone(selected)
                    self.assertEqual(expected, reason)


if __name__ == "__main__":
    unittest.main()
