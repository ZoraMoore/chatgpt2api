from __future__ import annotations

import base64
import io
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")

from curl_cffi import requests
from curl_cffi import CurlOpt
from curl_cffi.requests.exceptions import RequestException, Timeout
from PIL import Image
from services.config import config
from services.openai_backend_api import ChatRequirements, ImageGenerationTimeoutError, OpenAIBackendAPI
from services.protocol import conversation as conv


def reference_image():
    output = io.BytesIO()
    Image.new("RGB", (8, 6), "blue").save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode()


class RecordingSession:
    def __init__(self, generation_status=200, conduit="test-conduit"):
        self.headers = {"Authorization": "Bearer synthetic", "Originator": "Codex Browser",
                        "X-OpenAI-Web-Frontend": "codex_webview", "X-OpenAI-Codex-Window-Type": "browser"}
        self.curl_options = {}
        self.cookies = requests.Cookies()
        self.calls = []
        self.generation_status = generation_status
        self.conduit = conduit
        self.responses = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", urlsplit(url).path, kwargs))
        path = urlsplit(url).path
        if path == "/backend-api/files":
            body = {"file_id": "file-reference", "upload_url": "https://files.oaiusercontent.com/reference"}
        elif path == "/backend-api/files/file-reference/uploaded":
            body = {}
        elif path == "/backend-api/f/conversation/prepare":
            body = {"conduit_token": self.conduit}
        elif path == "/backend-api/f/conversation":
            body = {"detail": "Model not found"}
        else:
            raise AssertionError("unexpected protocol path: " + path)
        response = requests.Response()
        response.status_code = self.generation_status if path.endswith("/f/conversation") else 200
        response.content = json.dumps(body).encode()
        response.close = Mock()
        response.iter_content = Mock(return_value=iter([response.content]))
        response.iter_lines = Mock(return_value=iter([
            b'data: {"conversation_id":"test-conversation","type":"message_stream_complete"}', b"", b"data: [DONE]", b""
        ]))
        self.responses.append(response)
        return response

    def close(self):
        pass


class AsyncRecordingSession:
    """协议契约继续记录原请求；真实连接收尾由本地 HTTP 故障注入测试覆盖。"""
    def __init__(self, original):
        self.original = original
        self.cookies = original.cookies

    async def post(self, url, **kwargs):
        response = self.original.post(url, **kwargs)
        async def lines():
            for line in response.iter_lines():
                yield line
        async def chunks():
            for chunk in response.iter_content():
                yield chunk
        async def close():
            response.close()
        response.aiter_lines = lines
        response.aiter_content = chunks
        response.aclose = close
        return response

    async def close(self):
        pass


class WebImageConversationTransportTests(unittest.TestCase):
    def setUp(self):
        factory = patch('services.image_generation_stream._make_async_session',
                        side_effect=lambda original, timeout: AsyncRecordingSession(original))
        factory.start()
        self.addCleanup(factory.stop)

    def backend(self, *, status=200, conduit="test-conduit"):
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "synthetic"
        backend.image_transport = "conversation"
        backend.base_url = "https://chatgpt.com"
        backend.progress_callback = None
        backend.device_id = "device"
        backend.session_id = "session"
        backend.client_version = "test-version"
        backend.client_build_number = "test-build"
        backend.user_agent = "test-agent"
        backend._web_session_token = ""
        backend.session = RecordingSession(status, conduit)
        backend._bootstrap = Mock()
        backend._get_chat_requirements = Mock(return_value=ChatRequirements("requirements", "proof", "turnstile", "so"))
        backend._request_image_transfer = Mock(return_value=Mock(status_code=200))
        backend._timezone_fields = lambda: {"timezone": "Asia/Shanghai", "timezone_offset_min": -480}
        self.addCleanup(backend.close)
        return backend

    def run_stream(self, backend, images=None, model="gpt-image-2"):
        with patch("services.voice_image_transport.stream_voice_image", side_effect=AssertionError("ordinary opened voice")), \
             patch("services.openai_backend_api.browser_sentinel_enabled", return_value=False):
            return list(backend._stream_picture_conversation("画一只猫", model, images or []))

    def test_webimage_routing_and_headers_for_both_public_aliases(self):
        for model in ("gpt-image-2", "gpt-image-2.5"):
            with self.subTest(model=model):
                backend = self.backend()
                self.assertEqual("[DONE]", self.run_stream(backend, model=model)[-1])
                self.assertEqual(["/backend-api/f/conversation/prepare", "/backend-api/f/conversation"],
                                 [path for _, path, _ in backend.session.calls])
                prepare, generation = [kwargs for _, _, kwargs in backend.session.calls]
                for request in (prepare, generation):
                    self.assertEqual("auto", request["json"]["model"])
                    self.assertEqual(["picture_v2"], request["json"]["system_hints"])
                    self.assertNotIn("local_function_names", request["json"])
                    self.assertNotIn("browser_context", request["json"])
                    self.assertNotIn("conversation_id", request["json"])
                    self.assertNotIn("Originator", request["headers"])
                    self.assertNotIn("X-OpenAI-Web-Frontend", request["headers"])
                    self.assertEqual("requirements", request["headers"]["OpenAI-Sentinel-Chat-Requirements-Token"])
                self.assertEqual("success", prepare["json"]["client_prepare_state"])
                self.assertEqual("text/event-stream", generation["headers"]["Accept"])
                self.assertEqual("test-conduit", generation["headers"]["X-Conduit-Token"])
                self.assertEqual(generation["json"]["messages"][0]["id"], backend._image_submitted_message_id)
                self.assertTrue(backend._webimage_generation_sent)
                self.assertEqual("test-conversation", backend.image_conversation_id)
                backend.session.responses[-1].close.assert_called_once()

    def test_ordinary_edit_does_not_use_voice_upload_or_payload(self):
        backend = self.backend()
        with patch.object(backend, "_upload_image", side_effect=AssertionError("voice upload reused")), \
             patch.object(backend, "_image_generation_payload", side_effect=AssertionError("voice message reused")), \
             patch.object(backend, "_image_model_slug", side_effect=AssertionError("voice model reused")):
            self.run_stream(backend, [reference_image()])
        self.assertEqual(["/backend-api/files", "/backend-api/files/file-reference/uploaded",
                          "/backend-api/f/conversation/prepare", "/backend-api/f/conversation"],
                         [path for _, path, _ in backend.session.calls])
        upload = backend.session.calls[0][2]["json"]
        self.assertNotIn("store_in_library", upload)
        message = backend.session.calls[-1][2]["json"]["messages"][0]
        self.assertEqual("file-service://file-reference", message["content"]["parts"][0]["asset_pointer"])
        self.assertEqual("image/png", message["metadata"]["attachments"][0]["mimeType"])
        self.assertEqual(["picture_v2"], message["metadata"]["system_hints"])
        backend._request_image_transfer.assert_called_once()

    def test_http_failure_is_single_submission_and_closes_stream(self):
        backend = self.backend(status=404)
        with self.assertRaisesRegex(Exception, "Model not found"):
            self.run_stream(backend)
        self.assertTrue(getattr(backend, "_webimage_generation_sent", False))
        self.assertEqual(1, sum(path == "/backend-api/f/conversation" for _, path, _ in backend.session.calls))
        backend.session.responses[-1].close.assert_called_once()

    def test_bad_conduit_never_submits_generation(self):
        for value in (None, "", "bad\r\nHeader: injected", "x" * 65537, 123):
            with self.subTest(value_type=type(value).__name__):
                backend = self.backend(conduit=value)
                with self.assertRaisesRegex(RuntimeError, "conduit"):
                    self.run_stream(backend)
                self.assertEqual(1, len(backend.session.calls))

    def test_new_requests_have_distinct_roots(self):
        roots, messages = [], []
        for _ in range(2):
            backend = self.backend()
            backend.image_conversation_id = "previous"
            self.run_stream(backend)
            body = backend.session.calls[-1][2]["json"]
            roots.append(body["parent_message_id"])
            messages.append(body["messages"][0]["id"])
        self.assertEqual(2, len(set(roots)))
        self.assertEqual(2, len(set(messages)))

    def test_generation_attempt_prevents_account_retry(self):
        request = conv.ConversationRequest(model="gpt-image-2", prompt="draw", image_transport="conversation")
        backend = self.backend()
        backend._webimage_generation_sent = True
        account = Mock()
        account.get_available_access_token.side_effect = ["synthetic", AssertionError("replayed generation")]
        account.get_account.return_value = {}
        account.refresh_access_token.return_value = None
        with patch.object(conv, "account_service", account), \
             patch.object(conv, "OpenAIBackendAPI", return_value=backend), \
             patch.object(conv, "stream_image_outputs", side_effect=RuntimeError("access token expired")):
            with self.assertRaises(conv.ImageGenerationError):
                conv._generate_single_image_impl(request, 1, 1)
        account.refresh_access_token.assert_not_called()
        account.remove_invalid_token.assert_not_called()
        account.get_available_access_token.assert_called_once()

    def test_upload_does_not_forward_bearer_and_restores_it_on_failure(self):
        backend = self.backend()
        def upload(*args, **kwargs):
            self.assertNotIn("Authorization", backend.session.headers)
            self.assertFalse(kwargs["allow_redirects"])
            raise RuntimeError("synthetic storage failure")
        backend._request_image_transfer.side_effect = upload
        with self.assertRaisesRegex(RuntimeError, "storage failure"):
            self.run_stream(backend, [reference_image()])
        self.assertEqual("Bearer synthetic", backend.session.headers["Authorization"])
        self.assertEqual(["/backend-api/files"], [path for _, path, _ in backend.session.calls])

    def test_stream_timeout_and_cancellation_release_connection(self):
        for failure in (None, Timeout("synthetic timeout"), RequestException("synthetic timeout", code=28)):
            with self.subTest(failure=type(failure).__name__):
                backend = self.backend()
                backend.session.curl_options[CurlOpt.TIMEOUT_MS] = 42
                original = backend.session.post
                def post(url, **kwargs):
                    response = original(url, **kwargs)
                    if url.endswith("/f/conversation") and failure is not None:
                        response.iter_lines.side_effect = failure
                    return response
                backend.session.post = post
                stream = backend._stream_picture_conversation("test prompt", "gpt-image-2", [])
                if failure is None:
                    next(stream)
                    stream.close()
                else:
                    with self.assertRaises(ImageGenerationTimeoutError):
                        list(stream)
                self.assertTrue(backend._webimage_generation_sent)
                self.assertEqual(42, backend.session.curl_options[CurlOpt.TIMEOUT_MS])
                backend.session.responses[-1].close.assert_called_once()

    def test_ordinary_headers_never_mutate_another_voice_instance(self):
        ordinary, voice = self.backend(), self.backend()
        voice.image_transport = "voice"
        before = dict(voice.session.headers)
        self.run_stream(ordinary)
        self.assertEqual(before, voice.session.headers)
        self.assertNotIn("Originator", ordinary.session.headers)

    def test_rejects_backend_already_bound_to_voice(self):
        backend = self.backend()
        backend._voice_image_session = Mock()
        with self.assertRaisesRegex(RuntimeError, "independent conversation"):
            self.run_stream(backend)
        self.assertEqual([], backend.session.calls)


if __name__ == "__main__":
    unittest.main()
