from __future__ import annotations

import json
import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")

from services.openai_backend_api import OpenAIBackendAPI
from services.protocol.conversation import ConversationRequest
from services.protocol.final_image import FinalImageTracker


def event_message(role, content, message_id, **extra):
    return {"v": {"conversation_id": "conv-test", "message": {
        "id": message_id, "author": {"role": role}, "content": content, **extra,
    }}}


IMAGE = event_message("tool", {"content_type": "multimodal_text", "parts": [
    {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file_00000000aaaaaaaaaaaaaaaaaaaaaaaa"},
]}, "image-test")
CONTAINER = event_message("tool", {"content_type": "execution_output", "text": "saved"}, "exec-test")
CONTAINER["v"]["message"]["author"]["name"] = "container.exec"
FINAL = event_message("assistant", {"content_type": "text", "parts": [
    "[PNG](sandbox:/mnt/data/final.png)"
]}, "final-test", channel="final", status="finished_successfully", end_turn=True)


def response(events):
    return Mock(iter_lines=lambda: iter(("data: " + (e if isinstance(e, str) else json.dumps(e))).encode() for e in events))


class NativeImageFlowTests(unittest.TestCase):
    def backend(self, events):
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "test-token"
        backend.progress_callback = None
        backend._bootstrap = Mock()
        backend._upload_image = Mock(return_value={
            "file_id": "file-input", "library_file_id": "libfile_input", "mime_type": "image/png",
            "file_name": "input.png", "width": 64, "height": 64, "file_size": 120,
        })
        backend._get_chat_requirements = Mock()
        backend._prepare_image_conversation = Mock(return_value="conduit")
        backend._start_image_generation = Mock(return_value=response(events))
        backend._post_image_conversation = Mock(side_effect=AssertionError("unexpected additional conversation"))
        return backend

    def test_reference_stream_makes_one_upstream_post_only(self):
        backend = self.backend([IMAGE, CONTAINER, FINAL, "[DONE]"])
        outputs = list(backend._stream_picture_conversation("原始提示词", "gpt-image-2.5", ["input"]))
        backend._start_image_generation.assert_called_once()
        backend._post_image_conversation.assert_not_called()
        self.assertEqual(1, len([item for item in outputs if item == "[DONE]"]))
        self.assertTrue(any("container.exec" in item for item in outputs))

    def test_no_additional_request_when_upstream_skips_mainline(self):
        skipped = event_message("assistant", {"content_type": "code", "text": '{"skipped_mainline":true}'}, "skip")
        backend = self.backend([skipped, IMAGE, "[DONE]"])
        list(backend._stream_picture_conversation("原始提示词", "gpt-image-2.5", ["input"]))
        backend._start_image_generation.assert_called_once()
        backend._post_image_conversation.assert_not_called()
        self.assertEqual("原始提示词", backend._start_image_generation.call_args.args[0])

    def test_first_turn_tool_summary_marks_post_image_execution(self):
        tracker = FinalImageTracker()
        tracker.update(IMAGE)
        tracker.update(CONTAINER)
        self.assertTrue(tracker.generated)
        self.assertTrue(tracker.code_tool_after_image)
        self.assertEqual({"container.exec": 1}, tracker.execution_summary("sandbox")["tools"])

    def test_final_sandbox_path_is_parsed_without_executing_code(self):
        tracker = FinalImageTracker()
        tracker.update(FINAL)
        tracker.stream_complete = True
        self.assertEqual(["/mnt/data/final.png"], tracker.paths())
        self.assertTrue(tracker.is_complete())

    def test_reference_image_without_code_tool_finishes_in_one_request(self):
        backend = self.backend([IMAGE, "[DONE]"])
        outputs = list(backend._stream_picture_conversation("原始提示词", "gpt-image-2", ["input"]))
        self.assertEqual("[DONE]", outputs[-1])
        backend._start_image_generation.assert_called_once()
        backend._post_image_conversation.assert_not_called()
        self.assertEqual("原始提示词", backend._start_image_generation.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
