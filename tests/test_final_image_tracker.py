from __future__ import annotations

import copy
import json
import unittest

from services.protocol.final_image import FinalImageTracker, validated_sandbox_image_path


def message(message_id, role="assistant", content=None, **fields):
    return {"v": {"message": {
        "id": message_id, "author": {"role": role},
        "content": content or {"content_type": "text", "parts": [""]},
        "recipient": "all", "channel": "final", "status": "in_progress", "end_turn": False,
        **fields,
    }}}


def tool_message(message_id, name, content_type="execution_output"):
    content = {"content_type": content_type, "text": "synthetic output"}
    if content_type == "multimodal_text":
        content["parts"] = [{"content_type": "image_asset_pointer", "asset_pointer": "sediment://synthetic-image"}]
    return message(message_id, role="tool", content=content, author={"role": "tool", "name": name})


class FinalImageTrackerTests(unittest.TestCase):
    def test_scalar_delta_inherits_path_and_operation(self):
        tracker = FinalImageTracker()
        tracker.update(message("final"))
        tracker.update({"p": "/message/content/parts/0", "o": "append", "v": "[PNG](sandbox:/mnt/data/"})
        tracker.update({"v": "final.png)"})
        tracker.update({"p": "/message/status", "o": "replace", "v": "finished_successfully"})
        tracker.update({"p": "/message/end_turn", "v": True})
        self.assertEqual(["/mnt/data/final.png"], tracker.paths())
        self.assertFalse(tracker.is_complete())
        tracker.update({"type": "message_stream_complete"})
        self.assertTrue(tracker.is_complete())

    def test_patch_envelope_context_is_separate_from_child_operations(self):
        tracker = FinalImageTracker()
        tracker.update(message("final"))
        tracker.update({"o": "patch", "v": [{"p": "/message/content/parts/0", "o": "append", "v": "first"}]})
        tracker.update({"v": [{"v": " second"}, {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
                              {"p": "/message/end_turn", "v": True}]})
        self.assertEqual(["first second"], tracker.current["content"]["parts"])
        tracker.update({"type": "message_stream_complete"})
        self.assertTrue(tracker.is_complete())

    def test_legacy_operation_array_remains_supported(self):
        tracker = FinalImageTracker()
        tracker.update(message("final"))
        tracker.update({"v": [{"p": "/message/content/parts/0", "o": "append", "v": "result"}]})
        tracker.update({"v": [{"p": "/message/end_turn", "o": "replace", "v": True}]})
        self.assertEqual(["result"], tracker.current["content"]["parts"])
        self.assertTrue(tracker.current["end_turn"])

    def test_thought_array_is_data_and_summary_only_exposes_count(self):
        tracker = FinalImageTracker()
        snapshot = message("thought", content={"content_type": "thoughts", "thoughts": []}, channel=None)
        tracker.update(snapshot)
        thought = {"summary": "synthetic private marker", "content": "synthetic private body", "chunks": [], "finished": True}
        tracker.update({"p": "/message/content/thoughts", "o": "append", "v": [thought]})
        tracker.update({"v": [thought]})
        self.assertEqual(2, len(tracker.current["content"]["thoughts"]))
        summary = tracker.execution_summary("image_tool")
        self.assertEqual(1, summary["thought_message_count"])
        self.assertNotIn("synthetic private", json.dumps(summary))
        self.assertEqual([], tracker.paths())
        self.assertFalse(tracker.is_complete())
        tracker.update(snapshot)
        self.assertEqual(1, tracker.execution_summary("image_tool")["thought_message_count"])
        tracker.update(message("second-thought", content={"content_type": "thoughts", "thoughts": []}))
        self.assertEqual(2, tracker.execution_summary("image_tool")["thought_message_count"])

    def test_value_array_cannot_inject_message_patches(self):
        tracker = FinalImageTracker()
        tracker.update(message("final"))
        patch_like_value = {"p": "/message/end_turn", "o": "replace", "v": True}
        tracker.update({"p": "/message/metadata/list", "o": "add", "v": [patch_like_value]})
        self.assertFalse(tracker.current["end_turn"])
        self.assertEqual([patch_like_value], tracker.current["metadata"]["list"])

    def test_new_snapshot_resets_both_patch_contexts_and_completion(self):
        for delta in ({"p": "/message/content/parts/0", "o": "append", "v": "old"},
                      {"o": "patch", "v": [{"p": "/message/content/parts/0", "o": "append", "v": "old"}]}):
            with self.subTest(delta=delta):
                tracker = FinalImageTracker()
                tracker.update(message("old"))
                tracker.update(delta)
                tracker.update({"type": "message_stream_complete"})
                tracker.update(message("new"))
                tracker.update({"v": "unexpected"})
                tracker.update({"o": "patch", "v": [{"v": "unexpected"}]})
                self.assertEqual([""], tracker.current["content"]["parts"])
                self.assertFalse(tracker.stream_complete)

    def test_open_image_does_not_reset_code_verification_or_count_as_generation(self):
        tracker = FinalImageTracker()
        tracker.update(tool_message("view-before-generation", "container.open_image", "multimodal_text"))
        self.assertFalse(tracker.generated)
        tracker.update(tool_message("generated", "image-tool", "multimodal_text"))
        tracker.update(tool_message("executed", "container.exec"))
        tracker.update(tool_message("viewed", "container.open_image", "multimodal_text"))
        self.assertTrue(tracker.code_tool_after_image)
        self.assertEqual("generated", tracker.last_image_message_id)
        self.assertEqual(1, tracker.tools["container.exec"])
        tracker.update(tool_message("regenerated", "image-tool", "multimodal_text"))
        self.assertFalse(tracker.code_tool_after_image)

    def test_open_image_execution_output_is_not_code_execution(self):
        tracker = FinalImageTracker()
        tracker.update(tool_message("generated", "image-tool", "multimodal_text"))
        tracker.update(tool_message("viewed", "container.open_image"))
        self.assertTrue(tracker.postprocessing)
        self.assertFalse(tracker.code_tool_after_image)

    def test_aliased_tool_requires_execution_output_evidence(self):
        tracker = FinalImageTracker()
        tracker.update(tool_message("generated", "image-tool", "multimodal_text"))
        tracker.update(tool_message("alias", "tool_123.run_456", "text"))
        self.assertFalse(tracker.code_tool_after_image)
        self.assertFalse(tracker.postprocessing)
        tracker.update({"p": "/message/content/content_type", "o": "replace", "v": "execution_output"})
        self.assertTrue(tracker.code_tool_after_image)
        self.assertTrue(tracker.postprocessing)
        self.assertEqual(1, tracker.tools["tool_123.run_456"])

    def test_execution_before_image_does_not_verify_new_image(self):
        tracker = FinalImageTracker()
        tracker.update(tool_message("executed", "container.exec"))
        tracker.update(tool_message("generated", "image-tool", "multimodal_text"))
        self.assertFalse(tracker.code_tool_after_image)
        tracker.update(tool_message("executed", "container.exec"))
        self.assertFalse(tracker.code_tool_after_image)

    def test_invalid_tool_names_do_not_leak_or_count_as_evidence(self):
        for invalid in ("bad\nname", "x" * 81, ["container.exec"], None):
            with self.subTest(invalid=invalid):
                tracker = FinalImageTracker()
                tracker.update(tool_message("generated", "image-tool", "multimodal_text"))
                tracker.update(tool_message("executed", invalid))
                self.assertFalse(tracker.code_tool_after_image)
                self.assertEqual({"image-tool": 1}, tracker.execution_summary("image_tool")["tools"])

    def test_observed_model_prefers_valid_resolved_identifier(self):
        tracker = FinalImageTracker()
        tracker.update(message("first", metadata={"model_slug": "requested-model", "resolved_model_slug": "resolved-model"}))
        tracker.update(message("second", metadata={"model_slug": "requested-model"}))
        self.assertEqual("resolved-model", tracker.execution_summary("image_tool")["upstream_model_slug"])
        tracker.update(message("third", metadata={"resolved_model_slug": "invalid\nsecret"}))
        self.assertEqual("resolved-model", tracker.execution_summary("image_tool")["upstream_model_slug"])
        tracker.update(message("fourth", metadata={"resolved_model_slug": "new-resolved-model"}))
        self.assertEqual("new-resolved-model", tracker.execution_summary("image_tool")["upstream_model_slug"])

    def test_terminal_fields_and_final_channel_are_required(self):
        tracker = FinalImageTracker()
        tracker.update(message("final", content={"content_type": "text", "parts": ["[PNG](sandbox:/mnt/data/final.png)"]},
                               status="finished_successfully", end_turn=True))
        self.assertFalse(tracker.is_complete())
        tracker.update({"type": "message_stream_complete"})
        self.assertTrue(tracker.is_complete())
        tracker.update({"p": "/message/channel", "o": "replace", "v": "analysis"})
        self.assertFalse(tracker.is_complete())
        self.assertEqual([], tracker.paths())

    def test_malformed_patch_cannot_modify_negative_array_indices(self):
        tracker = FinalImageTracker()
        tracker.update(message("final"))
        before = copy.deepcopy(tracker.current)
        for event in (None, {"p": [], "o": "replace", "v": "bad"},
                      {"p": "/message/end_turn", "o": [], "v": True},
                      {"p": "/message/content/parts/-1", "o": "replace", "v": "bad"},
                      {"p": "/message/content/parts/9999999999", "o": "add", "v": "bad"},
                      {"p": "/message/end_turn", "o": "replace"}):
            tracker.update(event)
        self.assertEqual(before, tracker.current)

    def test_sandbox_paths_reject_traversal_and_untrusted_schemes(self):
        for path in ("/mnt/data/../secret.png", "/mnt/data/%2e%2e/secret.png", "/mnt/data/%252e%252e/secret.png",
                     "https://example.test/image.png", "/mnt/data/image.py", "/mnt/data/im\x00age.png"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validated_sandbox_image_path(path)
        self.assertEqual("/mnt/data/image.png", validated_sandbox_image_path("/mnt/data/image.png"))


if __name__ == "__main__":
    unittest.main()
