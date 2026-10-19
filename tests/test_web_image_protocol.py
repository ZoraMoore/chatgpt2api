from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")

from curl_cffi import CurlOpt
from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from services.egress_timezone import EgressTimezone
from services.protocol.conversation import iter_conversation_payloads


class WebImageProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 普通请求夹具沿用已部署的工具选择策略；最新参考图夹具来自本次 HAR。
        # 两者均替换提示词、会话/消息标识和时间，不保存凭据。
        cls.fixture = json.loads((Path(__file__).parent / "fixtures/web_image_har_request.json").read_text())
        cls.reference_fixture = json.loads((Path(__file__).parent / "fixtures/web_image_latest_reference_request.json").read_text())

    def backend(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend._egress_timezone = EgressTimezone("Asia/Shanghai", "ipwho")
        backend.access_token = "test-token"
        backend.base_url = "https://chatgpt.com"
        backend.progress_callback = None
        backend.session = Mock()
        backend.session.curl_options = {}
        backend._headers = lambda _path, extra=None: dict(extra or {})
        backend._bootstrap = Mock()
        backend._get_chat_requirements = Mock(return_value=ChatRequirements(token="test-requirements"))
        return backend

    def response(self, payload):
        return Mock(status_code=200, json=Mock(return_value=payload), headers={}, text="")

    def test_both_aliases_match_har_request_bodies(self):
        for model in ("gpt-image-2", "gpt-image-2.5"):
            with self.subTest(model=model):
                backend = self.backend()
                backend.session.post.return_value = self.response({"conduit_token": "test-conduit"})
                backend._prepare_image_conversation("测试小猫", ChatRequirements(token="test"), model)
                actual = backend.session.post.call_args.kwargs["json"]
                actual["partial_query"]["id"] = "fixture-prepare-id"
                self.assertEqual(self.fixture["prepare"], actual)
                actual = backend._image_generation_payload("测试小猫", model)
                actual["messages"][0].update(id="fixture-message-id", create_time=0)
                actual["browser_context"]["instance_id"] = "fixture-browser-id"
                self.assertEqual(self.fixture["conversation"], actual)

    def test_reference_request_matches_latest_har_in_a_new_conversation(self):
        reference = {"file_id": "file-input", "library_file_id": "libfile_input", "width": 64,
                     "height": 32, "file_size": 100, "mime_type": "image/png", "file_name": "input.png"}
        backend = self.backend()
        backend.session.post.return_value = self.response({"conduit_token": "fixture-conduit"})
        backend._prepare_image_conversation("测试小猫", ChatRequirements(token="fixture"), "gpt-image-2.5", [reference])
        actual = backend.session.post.call_args.kwargs["json"]
        actual["partial_query"]["id"] = "fixture-prepare-id"
        self.assertEqual(self.reference_fixture["prepare"], actual)
        actual = backend._image_generation_payload("测试小猫", "gpt-image-2.5", [reference])
        actual["messages"][0].update(id="fixture-message-id", create_time=0)
        actual["browser_context"]["instance_id"] = "fixture-browser-id"
        self.assertEqual(self.reference_fixture["conversation"], actual)

    def test_stream_prepares_and_forwards_conduit_for_both_aliases(self):
        for model in ("gpt-image-2", "gpt-image-2.5"):
            with self.subTest(model=model):
                backend = self.backend()
                stream = self.response({})
                stream.iter_lines.return_value = iter([b"data: [DONE]", b""])
                backend.session.post.side_effect = [self.response({"conduit_token": "test-conduit"}), stream]
                backend.session.curl_options[CurlOpt.TIMEOUT_MS] = 42
                self.assertEqual(["[DONE]"], list(backend._stream_picture_conversation("测试小猫", model, [])))
                calls = backend.session.post.call_args_list
                self.assertEqual(["https://chatgpt.com/backend-api/f/conversation/prepare",
                                  "https://chatgpt.com/backend-api/f/conversation"], [c.args[0] for c in calls])
                self.assertEqual("test-conduit", calls[1].kwargs["headers"]["X-Conduit-Token"])
                self.assertEqual("codex_webview", calls[1].kwargs["headers"]["X-OpenAI-Web-Frontend"])
                self.assertTrue(calls[1].kwargs["stream"])
                self.assertEqual(42, backend.session.curl_options[CurlOpt.TIMEOUT_MS])
                stream.close.assert_called_once()

    def test_missing_conduit_stops_before_generation(self):
        for value in (None, "", " ", 123):
            with self.subTest(value=value):
                backend = self.backend()
                backend.session.post.return_value = self.response({"conduit_token": value})
                with self.assertRaisesRegex(RuntimeError, "no conduit token"):
                    list(backend._stream_picture_conversation("测试小猫", "gpt-image-2.5", []))
                self.assertEqual(1, backend.session.post.call_count)

    def test_prepare_omits_sentinel_and_generation_keeps_same_tokens(self):
        backend = self.backend()
        requirements = ChatRequirements(token="fixture-requirements", proof_token="fixture-proof",
                                        turnstile_token="fixture-turnstile")
        backend.session.post.return_value = self.response({"conduit_token": "fixture-conduit"})
        conduit = backend._prepare_image_conversation("测试图片", requirements, "gpt-image-2.5")
        prepare_headers = backend.session.post.call_args.kwargs["headers"]
        self.assertFalse(any(name.lower().startswith("openai-sentinel-") for name in prepare_headers))
        backend._post_image_conversation({"model": "gpt-6-thinking"}, requirements, conduit, 60)
        generation_headers = backend.session.post.call_args.kwargs["headers"]
        self.assertEqual("fixture-requirements", generation_headers["OpenAI-Sentinel-Chat-Requirements-Token"])
        self.assertEqual("fixture-proof", generation_headers["OpenAI-Sentinel-Proof-Token"])
        self.assertEqual("fixture-turnstile", generation_headers["OpenAI-Sentinel-Turnstile-Token"])
        self.assertEqual("fixture-conduit", generation_headers["X-Conduit-Token"])
        self.assertEqual("*/*", generation_headers["Accept"])
        self.assertEqual("gzip", generation_headers["X-OpenAI-Web-SSE-Compression"])

    def test_edit_keeps_uploaded_reference_metadata(self):
        reference = {"file_id": "file-input", "width": 64, "height": 32, "file_size": 100,
                     "mime_type": "image/png", "file_name": "input.png"}
        for model in ("gpt-image-2", "gpt-image-2.5"):
            message = self.backend()._image_generation_payload("编辑图片", model, [reference])["messages"][0]
            self.assertEqual("multimodal_text", message["content"]["content_type"])
            self.assertEqual("file-service://file-input", message["content"]["parts"][0]["asset_pointer"])
            self.assertEqual("file-input", message["metadata"]["attachments"][0]["id"])
            self.assertNotIn("system_hints", message["metadata"])
            self.assertEqual("image/png", message["metadata"]["attachments"][0]["mime_type"])

    def test_download_uses_har_path_and_rejects_path_injection(self):
        backend = self.backend()
        backend.session.get.return_value = self.response({"download_url": "https://chatgpt.com/backend-api/estuary/content"})
        self.assertTrue(backend._get_file_download_url("file_" + "0" * 32).endswith("/estuary/content"))
        self.assertEqual("https://chatgpt.com/backend-api/files/download/file_" + "0" * 32,
                         backend.session.get.call_args.args[0])
        for value in ("../file-x", "file-x?other=1", "file-x/extra", "file-" + "x" * 201, None):
            with self.assertRaises(ValueError):
                backend._get_file_download_url(value)
        self.assertEqual(1, backend.session.get.call_count)

    def test_shared_headers_do_not_change_other_protocols(self):
        headers = self.backend()._image_headers("/backend-api/f/conversation", ChatRequirements(token="test"))
        self.assertNotIn("Originator", headers)
        self.assertNotIn("X-OpenAI-Web-Frontend", headers)

    def test_image_result_survives_false_tool_invoked_metadata(self):
        # HAR 中图片先于统计元数据到达，tool_invoked=false 不能覆盖已收到的图片。
        file_id = "file_" + "0" * 32
        payloads = [json.dumps({"v": {"conversation_id": "test-conversation", "message": {
            "author": {"role": "tool"}, "content": {"content_type": "multimodal_text", "parts": [
                {"content_type": "image_asset_pointer", "asset_pointer": "sediment://" + file_id}
            ]}}}}), json.dumps({"type": "server_ste_metadata", "metadata": {
                "tool_invoked": False, "turn_use_case": "image gen"}}), "[DONE]"]
        result = list(iter_conversation_payloads(iter(payloads)))[-1]
        self.assertEqual([file_id], result["file_ids"])
        self.assertEqual([file_id], result["sediment_ids"])
        self.assertTrue(result["done"])


if __name__ == "__main__":
    unittest.main()
