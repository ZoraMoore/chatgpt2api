from __future__ import annotations

import copy
import os
import unittest
from unittest.mock import Mock, PropertyMock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "isolated-document-test-key")

from services.config import config
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol.conversation_document import conversation_timestamp, iter_conversation_messages


INPUT_FILE = "file_00000000aaaaaaaaaaaaaaaaaaaaaaaa"
OUTPUT_FILE = "file_00000000bbbbbbbbbbbbbbbbbbbbbbbb"


def image_message(message_id, role, file_id, created=1):
    return {"id": message_id, "author": {"role": role, "name": "image_tool" if role == "tool" else None},
            "create_time": created, "metadata": {}, "content": {"content_type": "multimodal_text", "parts": [
                {"content_type": "image_asset_pointer", "asset_pointer": "sediment://" + file_id}]}}


class ConversationDocumentTests(unittest.TestCase):
    def backend(self):
        return object.__new__(OpenAIBackendAPI)

    def test_flat_messages_return_generated_image_and_exclude_reference(self):
        document = {"messages": [image_message("input", "user", INPUT_FILE),
                                 image_message("output", "tool", OUTPUT_FILE)]}
        original = copy.deepcopy(document)
        records = self.backend()._extract_image_tool_records(document)
        self.assertEqual(["output"], [record["message_id"] for record in records])
        self.assertEqual([OUTPUT_FILE], records[0]["sediment_ids"])
        self.assertNotIn(INPUT_FILE, repr(records))
        self.assertEqual(original, document)

    def test_legacy_mapping_retains_message_id_fallback(self):
        message = image_message("output", "tool", OUTPUT_FILE)
        message.pop("id")
        records = self.backend()._extract_image_tool_records({"mapping": {"legacy-id": {"message": message}}})
        self.assertEqual("legacy-id", records[0]["message_id"])
        self.assertEqual([OUTPUT_FILE], records[0]["sediment_ids"])

    def test_explicit_flat_list_takes_precedence_over_old_mapping(self):
        document = {"messages": [], "mapping": {"old": {"message": image_message("old", "tool", INPUT_FILE)}}}
        self.assertEqual([], self.backend()._extract_image_tool_records(document))

    def test_poll_finds_background_result_after_initial_empty_snapshot(self):
        backend = self.backend()
        backend._query_backend_tasks = Mock(return_value=[])
        backend._get_conversation = Mock(side_effect=[
            {"messages": [image_message("input", "user", INPUT_FILE)]},
            {"messages": [image_message("input", "user", INPUT_FILE),
                          image_message("output", "tool", OUTPUT_FILE)]},
        ])
        with patch.object(type(config), "image_poll_initial_wait_secs", new_callable=PropertyMock, return_value=0), \
             patch.object(type(config), "image_poll_interval_secs", new_callable=PropertyMock, return_value=0), \
             patch.object(type(config), "image_check_before_hit_enabled", new_callable=PropertyMock, return_value=False):
            files, sediment = backend._poll_image_results("fixture-conversation", timeout_secs=2)
        self.assertEqual([OUTPUT_FILE], sediment)
        self.assertNotIn(INPUT_FILE, files + sediment)
        self.assertEqual(2, backend._get_conversation.call_count)

    def test_flat_refusal_is_not_lost_when_stream_has_no_final_answer(self):
        message = {"author": {"role": "assistant"}, "content": {"content_type": "text", "parts": [
            "This request violates our content policy."]}}
        result = self.backend()._find_content_policy_error_in_conversation({"messages": [message]})
        self.assertIn("content policy", result)

    def test_timestamp_formats_and_ordering(self):
        timestamp = conversation_timestamp("2026-10-15T08:00:00+08:00")
        self.assertEqual(timestamp, conversation_timestamp("2026-10-15T00:00:00Z"))
        self.assertEqual(timestamp, conversation_timestamp(str(timestamp)))
        records = self.backend()._extract_image_tool_records({"messages": [
            image_message("later", "tool", OUTPUT_FILE, "2026-10-15T00:00:01Z"),
            image_message("earlier", "tool", INPUT_FILE, timestamp),
        ]})
        self.assertEqual(["earlier", "later"], [record["message_id"] for record in records])

    def test_history_lookup_accepts_current_iso_timestamp(self):
        backend = self.backend()
        backend._list_recent_conversations = Mock(return_value=[{
            "id": "fixture-conversation", "title": "blue circle", "update_time": "2026-10-15T00:00:10Z"}])
        self.assertEqual("fixture-conversation", backend.find_conversation_by_prompt(
            "blue circle", conversation_timestamp("2026-10-15T00:00:00Z")))

    def test_invalid_message_shapes_and_timestamp_inputs_are_bounded(self):
        document = {"messages": [None, "invalid", {"id": "bad\nheader", "author": "invalid"}]}
        self.assertEqual([], self.backend()._extract_image_tool_records(document))
        for value in (None, True, "invalid", "x" * 129, float("nan"), float("inf"), "NaN"):
            self.assertEqual(0, conversation_timestamp(value))
        for value in ([], {"mapping": []}, {"messages": [None] * 10001}):
            with self.assertRaises(ValueError):
                list(iter_conversation_messages(value))


if __name__ == "__main__":
    unittest.main()
