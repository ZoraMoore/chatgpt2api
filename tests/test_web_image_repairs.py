from __future__ import annotations

import os
import io
import unittest
from unittest.mock import Mock, PropertyMock, patch

from PIL import Image

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "isolated-test-key")

from services.config import config
from services.protocol.conversation import (
    ConversationRequest,
    build_image_prompt,
    conversation_events,
    stream_image_outputs,
)


class WebImageRepairTests(unittest.TestCase):
    def test_default_image_options_preserve_exact_original_prompt(self):
        prompt = "  请按原图修改文字。\n保留排版。\n"
        for size in (None, "", "auto"):
            self.assertEqual(prompt, build_image_prompt(prompt, size, "auto"))
        backend = Mock()
        backend.stream_conversation.return_value = iter(["[DONE]"])
        with patch.object(type(config), "global_system_prompt", new_callable=PropertyMock, return_value=""):
            list(conversation_events(backend, model="gpt-image-2", prompt=prompt))
        self.assertEqual(prompt, backend.stream_conversation.call_args.kwargs["prompt"])
        backend.stream_conversation.assert_called_once()

    def test_explicit_image_options_remain_user_requirements(self):
        prompt = build_image_prompt("测试", "1024x1024", "high")
        self.assertIn("1024x1024", prompt)
        self.assertIn("high", prompt)

    def test_native_reference_image_is_delivered_from_all_poll_paths(self):
        # 原生首轮未调用代码工具时，正常交付三种轮询路径取得的图片。
        image = io.BytesIO()
        Image.new("RGB", (8, 6), "red").save(image, format="PNG")
        for mode in ("first_poll", "text_retry", "empty_retry"):
            with self.subTest(mode=mode):
                backend = Mock()
                backend.resolve_conversation_image_urls.side_effect = (
                    [["https://example.test/image.png"]] if mode == "first_poll" else
                    [[], ["https://example.test/image.png"]]
                )
                backend._poll_image_results.return_value = (["file-test"], [])
                backend.download_image_bytes.return_value = [image.getvalue()]
                event = {"type": "conversation.done", "done": True, "conversation_id": "test-conversation"}
                if mode == "text_retry":
                    event["text"] = '{"size":"1024x1024","n":1}'
                with patch("services.protocol.conversation.conversation_events", return_value=iter([event])), \
                     patch("services.protocol.conversation._get_detailed_error_from_tasks", return_value=""), \
                     patch("services.protocol.conversation.time.sleep"), \
                     patch("services.protocol.conversation.save_image_bytes", return_value="https://example.test/image.png"):
                    outputs = list(stream_image_outputs(backend, ConversationRequest(
                        model="gpt-image-2", prompt="原提示词", images=["reference"], response_format="url",
                    )))
                self.assertEqual("result", outputs[-1].kind)
                self.assertEqual("https://example.test/image.png", outputs[-1].data[0]["url"])
                self.assertEqual("image_tool", outputs[-1].execution["result_source"])
                self.assertFalse(outputs[-1].execution["code_tool_after_image"])
                backend.download_image_bytes.assert_called_once()

    def test_real_event_stages_are_reported_without_reasoning_text(self):
        from services.protocol.conversation import _image_progress_stage
        from services.protocol.final_image import FinalImageTracker

        tracker = FinalImageTracker()
        messages = [
            ({"author": {"role": "assistant"}, "content": {"content_type": "thoughts", "text": "private"}}, "thinking"),
            ({"author": {"role": "assistant"}, "recipient": "container.exec", "content": {"content_type": "code"}}, "running_code"),
            ({"author": {"role": "tool", "name": "container.open_image"}, "content": {"content_type": "multimodal_text"}}, "checking_image"),
            ({"author": {"role": "assistant"}, "end_turn": True, "content": {"content_type": "text"}}, "finalizing_image"),
        ]
        for message, expected in messages:
            tracker.update({"message": message})
            self.assertEqual(expected, _image_progress_stage(tracker))


if __name__ == "__main__":
    unittest.main()
