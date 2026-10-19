from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")
from PIL import Image
from services.openai_backend_api import OpenAIBackendAPI, ChatRequirements
from services.protocol.final_image import FinalImageTracker, validated_sandbox_image_path
from services.protocol.conversation import ConversationRequest, ImageGenerationError, stream_image_outputs


def png():
    out = io.BytesIO()
    Image.new("RGB", (8, 6), "red").save(out, format="PNG")
    return out.getvalue()


class FinalImageTests(unittest.TestCase):
    def setUp(self):
        self.payloads = json.loads((Path(__file__).parent / "fixtures/final_image_sse.json").read_text())

    def tracker(self, payloads=None):
        tracker = FinalImageTracker()
        for payload in self.payloads if payloads is None else payloads:
            if payload == "[DONE]":
                tracker.stream_complete = True
            else:
                tracker.update(json.loads(payload))
        return tracker

    def backend(self, payloads=None):
        backend = Mock()
        backend.stream_conversation.return_value = iter(self.payloads if payloads is None else payloads)
        backend.download_final_sandbox_images.return_value = [png()]
        backend._get_conversation.return_value = {"messages": []}
        return backend

    def test_captured_incremental_final_replaces_intermediate(self):
        tracker = self.tracker()
        self.assertTrue(tracker.postprocessing)
        self.assertTrue(tracker.is_complete())
        self.assertEqual(["/mnt/data/修正版.png"], tracker.paths())
        backend = self.backend()
        with patch("services.protocol.conversation.save_image_bytes", return_value="https://example.test/final.png"):
            outputs = list(stream_image_outputs(backend, ConversationRequest(
                model="gpt-image-2.5", prompt="测试", images=["test"], response_format="url")))
        result = [o for o in outputs if o.kind == "result"]
        self.assertEqual([{"url": "https://example.test/final.png", "revised_prompt": "测试"}], result[0].data)
        backend.download_final_sandbox_images.assert_called_once_with(
            "fixture-conversation", tracker.final["id"], ["/mnt/data/修正版.png"])
        backend.resolve_conversation_image_urls.assert_not_called()
        backend.download_image_bytes.assert_not_called()

    def test_download_failure_does_not_return_old_image(self):
        backend = self.backend()
        backend.download_final_sandbox_images.side_effect = RuntimeError("download failed")
        with self.assertRaisesRegex(ImageGenerationError, "最终修正版图片下载失败"):
            list(stream_image_outputs(backend, ConversationRequest(model="gpt-image-2.5", prompt="测试")))
        backend.resolve_conversation_image_urls.assert_not_called()

    def test_truncated_processing_does_not_return_old_image(self):
        payloads = self.payloads[:8]
        backend = self.backend(payloads)
        with self.assertRaisesRegex(ImageGenerationError, "尚未完整交付"):
            list(stream_image_outputs(backend, ConversationRequest(model="gpt-image-2.5", prompt="测试")))
        backend.resolve_conversation_image_urls.assert_not_called()

    def test_final_without_stream_completion_is_not_complete(self):
        payloads = [p for p in self.payloads if p != "[DONE]" and json.loads(p).get("type") != "message_stream_complete"]
        self.assertFalse(self.tracker(payloads).is_complete())

    def test_incomplete_sse_uses_final_message_from_conversation_snapshot(self):
        backend = self.backend([json.dumps({"type": "server_ste_metadata", "conversation_id": "fixture-conversation",
                                           "metadata": {"turn_use_case": "data analysis"}}), "[DONE]"])
        backend._get_conversation.return_value = {"messages": [{"id": "snapshot-final", "author": {"role": "assistant"},
            "channel": "final", "content": {"content_type": "text", "parts": ["[PNG](sandbox:/mnt/data/final.png)"]},
            "status": "finished_successfully", "end_turn": True}]}
        with patch("services.protocol.conversation.save_image_bytes", return_value="https://example.test/final.png"):
            result = list(stream_image_outputs(backend, ConversationRequest(model="gpt-image-2.5", response_format="url")))
        self.assertEqual("result", result[-1].kind)
        backend.download_final_sandbox_images.assert_called_once_with("fixture-conversation", "snapshot-final", ["/mnt/data/final.png"])
        backend.resolve_conversation_image_urls.assert_not_called()

    def test_postprocessing_hint_without_final_snapshot_rejects_intermediate(self):
        tool = next(p for p in self.payloads if '"multimodal_text"' in p)
        backend = self.backend([tool, json.dumps({"type": "server_ste_metadata",
                                "metadata": {"turn_use_case": "data analysis"}}), "[DONE]"])
        with self.assertRaisesRegex(ImageGenerationError, "尚未完整交付"):
            list(stream_image_outputs(backend, ConversationRequest(model="gpt-image-2.5")))
        backend.resolve_conversation_image_urls.assert_not_called()

    def test_plain_image_keeps_original_resolution_flow(self):
        tool = next(p for p in self.payloads if '"multimodal_text"' in p)
        backend = self.backend([tool, "[DONE]"])
        backend.resolve_conversation_image_urls.return_value = ["https://example.test/original.png"]
        backend.download_image_bytes.return_value = [png()]
        with patch("services.protocol.conversation.save_image_bytes", return_value="https://example.test/original.png"):
            result = list(stream_image_outputs(backend, ConversationRequest(model="gpt-image-2", prompt="测试", response_format="url")))
        self.assertEqual("result", result[-1].kind)
        backend.resolve_conversation_image_urls.assert_called_once()
        backend.download_final_sandbox_images.assert_not_called()

    def test_path_validation_and_encoded_chinese_deduplication(self):
        self.assertEqual("/mnt/data/修正版.png", validated_sandbox_image_path("/mnt/data/%E4%BF%AE%E6%AD%A3%E7%89%88.png"))
        for path in ["/mnt/data/../secret.png", "/mnt/data/%2e%2e/secret.png", "/mnt/data/%252e%252e/secret.png",
                     "/etc/a.png", "/mnt/data/a.py", "/mnt/data/a\x00.png", "/mnt/data/" + "a" * 2048 + ".png"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                validated_sandbox_image_path(path)

    def test_final_channel_may_arrive_as_patch_or_be_absent(self):
        for channel in (None, "final"):
            tracker = FinalImageTracker()
            tracker.update({"v": {"message": {"id": "final-id", "author": {"role": "assistant"},
                "recipient": "all", "channel": channel, "content": {"content_type": "text", "parts": [""]},
                "status": "in_progress", "end_turn": False}}})
            tracker.update({"v": [{"p": "/message/content/parts/0", "o": "append", "v": "[图](sandbox:/mnt/data/final.png)"},
                                  {"p": "/message/status", "o": "replace", "v": "finished_successfully"},
                                  {"p": "/message/end_turn", "o": "replace", "v": True}]})
            tracker.stream_complete = True
            self.assertTrue(tracker.is_complete())
            self.assertEqual(["/mnt/data/final.png"], tracker.paths())

    def test_interpreter_download_parameters_and_image_validation(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend.base_url = "https://chatgpt.com"
        backend._headers = lambda _path, extra=None: dict(extra or {})
        backend.session = Mock()
        backend.session.get.side_effect = [Mock(status_code=200, json=lambda: {"download_url": "https://chatgpt.com/backend-api/estuary/content"}),
                                          Mock(status_code=200, content=png())]
        self.assertEqual([png()], backend.download_final_sandbox_images("conversation", "message", ["/mnt/data/修正版.png"]))
        first = backend.session.get.call_args_list[0]
        self.assertEqual("https://chatgpt.com/backend-api/conversation/conversation/interpreter/download", first.args[0])
        self.assertEqual({"message_id": "message", "sandbox_path": "/mnt/data/修正版.png"}, first.kwargs["params"])
        backend.session.get.side_effect = [Mock(status_code=200, json=lambda: {"download_url": "https://chatgpt.com/test"}),
                                          Mock(status_code=200, content=b"<html>not an image</html>")]
        with self.assertRaises(Exception):
            backend.download_final_sandbox_images("conversation", "message", ["/mnt/data/修正版.png"])

    def test_reference_images_have_separate_generation_budget(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend.base_url = "https://chatgpt.com"
        backend.session = Mock(curl_options={})
        backend.session.post.return_value = Mock(status_code=200)
        backend._image_headers = Mock(return_value={})
        backend._image_generation_payload = Mock(return_value={})
        backend._start_image_generation("test", ChatRequirements(token="test"), "test", "gpt-image-2", [{}])
        self.assertEqual((30, 600), backend.session.post.call_args.kwargs["timeout"])
        backend._start_image_generation("test", ChatRequirements(token="test"), "test", "gpt-image-2")
        self.assertEqual((30, 120), backend.session.post.call_args.kwargs["timeout"])

    def test_reference_upload_processing_and_full_conversation_protocol(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend.base_url = "https://chatgpt.com"
        backend.user_agent = "test"
        backend._headers = lambda _path, extra=None: dict(extra or {})
        backend.session = Mock()
        backend.session.put.return_value = Mock(status_code=200)
        backend.session.post.side_effect = [
            Mock(status_code=200, json=lambda: {"file_id": "file-input", "upload_url": "https://example.test/upload", "library_file_id": "libfile-input"}),
            Mock(status_code=200),
            Mock(status_code=200, text=json.dumps({"file_id": "file-input", "event": "file.processing.completed",
                                                 "extra": {"metadata_object_id": "libfile_input"}})),
            Mock(status_code=200, json=lambda: {"conduit_token": "test-conduit"}),
        ]
        reference = backend._upload_image(base64.b64encode(png()).decode(), "input.png")
        self.assertEqual("libfile_input", reference["library_file_id"])
        self.assertTrue(backend.session.post.call_args.args[0].endswith("/files/process_upload_stream"))
        self.assertTrue(backend.session.post.call_args_list[0].kwargs["json"]["store_in_library"])
        backend._prepare_image_conversation("编辑", ChatRequirements(token="test"), "gpt-image-2.5", [reference])
        prepared = backend.session.post.call_args.kwargs["json"]
        self.assertEqual(["image/png"], prepared["attachment_mime_types"])
        self.assertNotIn("system_hints", prepared)
        payload = backend._image_generation_payload("编辑", "gpt-image-2.5", [reference])
        self.assertNotIn("system_hints", payload)
        self.assertEqual("sediment://file-input", payload["messages"][0]["content"]["parts"][0]["asset_pointer"])
        self.assertEqual("libfile_input", payload["messages"][0]["metadata"]["attachments"][0]["library_file_id"])


if __name__ == "__main__":
    unittest.main()
