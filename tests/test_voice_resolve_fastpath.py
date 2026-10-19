from __future__ import annotations

import copy
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.openai_backend_api import OpenAIBackendAPI


INPUT = "file_00000000111111111111111111111111"
OUTPUT = "file_00000000222222222222222222222222"
OTHER = "file_00000000333333333333333333333333"


def pointer(file_id):
    return {"content_type": "image_asset_pointer", "asset_pointer": "sediment://" + file_id}


class VoiceCompletedResolveTests(unittest.TestCase):
    def setUp(self):
        self.backend = object.__new__(OpenAIBackendAPI)
        self.backend._closed = True
        self.user = {"id": "submitted", "author": {"role": "user"},
                     "content": {"parts": [pointer(INPUT)]}}
        self.output = {"id": "image-result", "author": {"role": "tool", "name": "image_gen"},
                       "status": "finished_successfully", "create_time": 2,
                       "content": {"content_type": "multimodal_text", "parts": [pointer(OUTPUT)]},
                       "metadata": {"referenced_image_ids": [INPUT, OTHER]}}
        self.final = {"id": "final", "author": {"role": "assistant"}, "recipient": "all",
                      "status": "finished_successfully", "channel": "final",
                      "content": {"content_type": "multimodal_text", "parts": ["已完成。"]}}
        self.snapshot = {"conversation_id": "conversation", "voice_turn_complete": True,
                         "messages": [self.user, self.output, self.final]}
        self.voice = SimpleNamespace(completed=True, message=copy.deepcopy(self.user),
                                     snapshot=Mock(side_effect=lambda _: self.snapshot))
        self.backend._voice_image_session = self.voice
        self.backend._poll_image_results = Mock(return_value=([OUTPUT], []))
        self.backend._resolve_image_urls = Mock(return_value=["https://storage.example.test/result"])
        config = SimpleNamespace(image_poll_timeout_secs=120, image_check_before_hit_enabled=True,
                                 image_settle_enabled=True)
        patcher = patch("services.openai_backend_api.config", config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve(self):
        return self.backend.resolve_conversation_image_urls("conversation", [INPUT, OUTPUT], [INPUT, OUTPUT])

    def test_completed_snapshot_uses_only_output_pointer_not_input_or_metadata(self):
        self.assertEqual(["https://storage.example.test/result"], self.resolve())
        self.backend._resolve_image_urls.assert_called_once_with("conversation", [OUTPUT], [OUTPUT])
        self.backend._poll_image_results.assert_not_called()

    def test_snapshot_without_user_echo_uses_original_submission_to_exclude_inputs(self):
        self.snapshot["messages"].remove(self.user)
        self.output["content"]["parts"].append(pointer(INPUT))
        self.resolve()
        self.backend._resolve_image_urls.assert_called_once_with("conversation", [OUTPUT], [OUTPUT])
        self.backend._poll_image_results.assert_not_called()

    def test_multiple_output_parts_in_one_tool_message_are_retained(self):
        self.output["content"]["parts"].append(pointer(OTHER))
        self.resolve()
        self.backend._resolve_image_urls.assert_called_once_with("conversation", [OUTPUT, OTHER], [OUTPUT, OTHER])
        self.backend._poll_image_results.assert_not_called()

    def test_unfinished_voice_keeps_polling(self):
        self.voice.completed = False
        self.resolve()
        self.voice.snapshot.assert_not_called()
        self.backend._poll_image_results.assert_called_once()

    def test_wrong_conversation_or_missing_completion_keeps_polling(self):
        for field, value in (("conversation_id", "other"), ("voice_turn_complete", False)):
            with self.subTest(field=field):
                previous = self.snapshot[field]
                self.snapshot[field] = value
                self.backend._poll_image_results.reset_mock()
                self.resolve()
                self.backend._poll_image_results.assert_called_once()
                self.snapshot[field] = previous

    def test_unfinished_output_or_final_keeps_polling(self):
        for message in (self.output, self.final):
            with self.subTest(message_id=message["id"]):
                message["status"] = "in_progress"
                self.backend._poll_image_results.reset_mock()
                self.resolve()
                self.backend._poll_image_results.assert_called_once()
                message["status"] = "finished_successfully"

    def test_reference_echo_without_new_image_keeps_polling(self):
        self.output["content"]["parts"] = [pointer(INPUT)]
        self.resolve()
        self.backend._poll_image_results.assert_called_once()
        self.assertEqual(1, self.backend._resolve_image_urls.call_count)

    def test_multiple_image_messages_keep_original_selection_rules(self):
        earlier = copy.deepcopy(self.output)
        earlier["id"] = "old-image"
        earlier["content"]["parts"] = [pointer(OTHER)]
        self.snapshot["messages"].insert(1, earlier)
        self.resolve()
        self.backend._poll_image_results.assert_called_once()

    def test_other_user_turn_cannot_enable_fast_delivery_of_an_old_image(self):
        self.user["id"] = "other-submission"
        self.resolve()
        self.backend._poll_image_results.assert_called_once()

    def test_sandbox_or_code_postprocessing_does_not_enable_fast_image_delivery(self):
        self.final["content"]["parts"] = ["[最终图](sandbox:/mnt/data/final.png)"]
        self.resolve()
        self.backend._poll_image_results.assert_called_once()
        self.backend._poll_image_results.reset_mock()
        self.final["content"]["parts"] = ["已完成。"]
        self.snapshot["messages"].insert(2, {
            "id": "postprocess", "author": {"role": "tool", "name": "python"},
            "content": {"content_type": "execution_output", "text": "done"},
            "status": "finished_successfully",
        })
        self.resolve()
        self.backend._poll_image_results.assert_called_once()

    def test_failed_or_empty_direct_resolution_keeps_original_poll_fallback(self):
        for first in ([], RuntimeError("resolution unavailable")):
            with self.subTest(first=type(first).__name__):
                self.backend._resolve_image_urls.reset_mock()
                self.backend._poll_image_results.reset_mock()
                self.backend._resolve_image_urls.side_effect = [first, ["https://storage.example.test/fallback"]]
                self.assertEqual(["https://storage.example.test/fallback"], self.resolve())
                self.backend._poll_image_results.assert_called_once()
                self.assertEqual(2, self.backend._resolve_image_urls.call_count)

    def test_ordinary_backend_and_explicit_no_poll_keep_existing_behavior(self):
        del self.backend._voice_image_session
        self.resolve()
        self.backend._poll_image_results.assert_called_once()
        self.backend._poll_image_results.reset_mock()
        self.backend._voice_image_session = self.voice
        self.backend.resolve_conversation_image_urls("conversation", [OUTPUT], [], poll=False)
        self.voice.snapshot.assert_not_called()
        self.backend._poll_image_results.assert_not_called()


if __name__ == "__main__":
    unittest.main()
