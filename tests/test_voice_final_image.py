from __future__ import annotations

import unittest
import json
from unittest.mock import patch

from services.protocol.final_image import FinalImageTracker


def final_message(message_id="final", *, voice=True, status="finished_successfully", end_turn=None, path="final.png"):
    return {
        "_voice_transport": voice,
        "message": {
            "id": message_id, "author": {"role": "assistant"}, "recipient": "all", "channel": "final",
            "content": {"content_type": "multimodal_text" if voice else "text",
                        "parts": [f"[PNG](sandbox:/mnt/data/{path})"]},
            "status": status, "end_turn": end_turn,
        },
    }


class VoiceFinalImageTests(unittest.TestCase):
    @staticmethod
    def narration(message_id="voice-final", *, status="finished_successfully"):
        event = final_message(message_id, status=status)
        event["message"]["content"]["parts"] = ["图片已经处理好了。"]
        return event

    def test_voice_narration_keeps_completed_artifact_and_its_message_id(self):
        tracker = FinalImageTracker()
        artifact = final_message("artifact", voice=False, end_turn=True)
        artifact["_voice_transport"] = True
        tracker.update(artifact)
        tracker.update(self.narration(status="in_progress"))
        self.assertFalse(tracker.is_complete())
        tracker.update(self.narration())
        self.assertEqual("artifact", tracker.final["id"])
        self.assertEqual(["/mnt/data/final.png"], tracker.paths())
        self.assertFalse(tracker.is_complete())
        tracker.update({"type": "voice_turn_complete"})
        self.assertTrue(tracker.is_complete())
        self.assertTrue(tracker.execution_summary("sandbox")["voice_final_preserved"])

    def test_narration_cannot_promote_an_unfinished_artifact(self):
        tracker = FinalImageTracker()
        tracker.update(final_message("unfinished", status="in_progress"))
        tracker.update(self.narration())
        tracker.update({"type": "voice_turn_complete"})
        self.assertEqual([], tracker.paths())

    def test_same_message_replacement_cannot_resurrect_removed_link(self):
        tracker = FinalImageTracker()
        tracker.update(final_message("same"))
        tracker.update(self.narration("same"))
        tracker.update({"type": "voice_turn_complete"})
        self.assertEqual([], tracker.paths())

    def test_new_artifact_replaces_previous_even_after_voice_narration(self):
        tracker = FinalImageTracker()
        tracker.update(final_message("old", path="old.png"))
        tracker.update(self.narration())
        tracker.update(final_message("new", path="new.png"))
        tracker.update(self.narration("new-narration"))
        tracker.update({"type": "voice_turn_complete"})
        self.assertEqual("new", tracker.final["id"])
        self.assertEqual(["/mnt/data/new.png"], tracker.paths())

    def test_new_user_tool_or_review_invalidates_previous_artifact(self):
        boundaries = [
            {"message": {"id": "user-next", "author": {"role": "user"}, "content": {"content_type": "text", "parts": ["修改"]}}},
            {"message": {"id": "call-next", "author": {"role": "assistant"}, "recipient": "image-tool",
                         "content": {"content_type": "code", "text": "synthetic"}}},
            {"message": {"id": "tool-next", "author": {"role": "tool", "name": "image-tool"},
                         "content": {"content_type": "text", "parts": ["synthetic"]}}},
            {"type": "image_review_started"},
        ]
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                tracker = FinalImageTracker()
                tracker.update(final_message("old", path="old.png"))
                tracker.update(self.narration())
                tracker.update(boundary)
                tracker.update(self.narration("new"))
                tracker.update({"type": "voice_turn_complete"})
                self.assertEqual([], tracker.paths())

    def test_invalid_new_path_does_not_fall_back_to_old_artifact(self):
        tracker = FinalImageTracker()
        tracker.update(final_message("old", path="old.png"))
        tracker.update(final_message("invalid", path="../secret.png"))
        with self.assertRaises(ValueError):
            tracker.paths()

    def test_new_plain_text_final_still_replaces_the_previous_final(self):
        tracker = FinalImageTracker()
        tracker.update(final_message("old"))
        event = self.narration("main-final")
        event["message"]["content"]["content_type"] = "text"
        tracker.update(event)
        self.assertEqual("main-final", tracker.final["id"])
        self.assertEqual([], tracker.paths())

    def test_delivery_downloads_artifact_message_not_the_voice_narration(self):
        from tests.test_final_image import FinalImageTests
        from services.protocol.conversation import ConversationRequest, stream_image_outputs

        artifact = final_message("artifact", voice=False, end_turn=True)
        artifact["_voice_transport"] = True
        events = [{"conversation_id": "fixture-conversation"}, artifact, self.narration(),
                  {"type": "voice_turn_complete"}]
        backend = FinalImageTests().backend([*[json.dumps(e) for e in events], "[DONE]"])
        with patch("services.protocol.conversation.save_image_bytes", return_value="https://example.test/final.png"):
            outputs = list(stream_image_outputs(backend, ConversationRequest(
                model="gpt-image-2.5", prompt="测试", response_format="url")))
        self.assertEqual("result", outputs[-1].kind)
        backend.download_final_sandbox_images.assert_called_once_with(
            "fixture-conversation", "artifact", ["/mnt/data/final.png"])
        backend.resolve_conversation_image_urls.assert_not_called()

    def test_voice_null_end_turn_requires_explicit_voice_completion(self):
        tracker = FinalImageTracker()
        tracker.update(final_message())
        self.assertEqual(["/mnt/data/final.png"], tracker.paths())
        self.assertFalse(tracker.is_complete())
        tracker.update({"type": "message_stream_complete"})
        self.assertFalse(tracker.is_complete())
        tracker.update({"type": "voice_turn_complete"})
        self.assertTrue(tracker.is_complete())

    def test_voice_completion_does_not_finish_in_progress_message(self):
        tracker = FinalImageTracker()
        tracker.update(final_message(status="in_progress"))
        tracker.update({"type": "voice_turn_complete"})
        self.assertFalse(tracker.is_complete())

    def test_tool_after_old_sandbox_final_requires_new_assistant_final(self):
        tracker = FinalImageTracker()
        tracker.update(final_message("old-final", path="old.png"))
        tracker.update({"_voice_transport": True, "message": {
            "id": "tool-a", "author": {"role": "tool", "name": "image-tool"},
            "content": {"content_type": "multimodal_text", "parts": [
                {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file-test"}]},
            "status": "finished_successfully",
        }})
        self.assertEqual([], tracker.paths())
        tracker.update({"type": "voice_turn_complete"})
        self.assertFalse(tracker.is_complete())
        self.assertEqual([], tracker.paths())
        tracker.update(final_message("new-final", path="new.png"))
        tracker.update({"type": "voice_turn_complete"})
        self.assertTrue(tracker.is_complete())
        self.assertEqual(["/mnt/data/new.png"], tracker.paths())

    def test_legacy_still_requires_end_turn_and_message_stream_completion(self):
        for end_turn in (None, False, True):
            with self.subTest(end_turn=end_turn):
                tracker = FinalImageTracker()
                tracker.update(final_message(voice=False, end_turn=end_turn))
                self.assertFalse(tracker.is_complete())
                tracker.update({"type": "message_stream_complete"})
                self.assertEqual(end_turn is True, tracker.is_complete())
                self.assertFalse(tracker.voice_transport)

    def test_voice_completion_alone_cannot_invent_final_image(self):
        tracker = FinalImageTracker()
        tracker.update({"type": "voice_turn_complete"})
        self.assertFalse(tracker.is_complete())
        self.assertEqual([], tracker.paths())


if __name__ == "__main__":
    unittest.main()
