from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-image26-key")

from api import ai, image_tasks
from services.config import config
from services.image_task_service import ImageTaskService
from services.protocol.conversation import ConversationRequest
from services.protocol import openai_v1_models
from services.webimage_conversation_transport import WebImageConversationTransport
from services.protocol import openai_v1_chat_complete, openai_v1_image_edit, openai_v1_image_generations, openai_v1_response
from tests.test_image_transport_entrypoints import CapturedCall
from tests import test_webimage_conversation_transport as webimage_test
from utils.helper import UpstreamHTTPError, is_image_chat_request, is_supported_image_model, split_image_model

MODELS = ("gpt-image-2.6-sunburst", "gpt-image-2.6-flare")


class WebImage26RegistryTests(unittest.TestCase):
    def test_all_protocol_handlers_route_new_models_to_image_pool(self):
        class RequestCaptured(Exception):
            pass

        def capture(request):
            self.assertEqual(MODELS[0], request.model)
            self.assertEqual("conversation", request.image_transport)
            raise RequestCaptured()

        cases = [
            (openai_v1_image_generations, {"prompt": "draw"}),
            (openai_v1_image_edit, {"prompt": "draw", "images": [(b"ref", "ref.png", "image/png")]}),
            (openai_v1_chat_complete, {"messages": [{"role": "user", "content": "draw"}]}),
            (openai_v1_response, {"input": "draw"}),
        ]
        for module, body in cases:
            for streaming in (False, True):
                with self.subTest(protocol=module.__name__, stream=streaming), \
                     patch.object(module, "stream_image_outputs_with_pool", side_effect=capture), \
                     patch.object(module, "text_backend", side_effect=AssertionError("image model sent to text"),
                                  create=True):
                    with self.assertRaises(RequestCaptured):
                        result = module.handle({"model": MODELS[0], "_image_transport": "voice",
                                                "stream": streaming, **body})
                        if not isinstance(result, dict):
                            list(result)

    def test_exact_models_enter_image_routing_without_affecting_old_models(self):
        for model in (*MODELS, "gpt-image-2", "gpt-image-2.5", "plus-codex-gpt-image-2"):
            with self.subTest(model=model):
                self.assertTrue(is_supported_image_model(model))
                self.assertTrue(is_image_chat_request({"model": model}))
        for model in MODELS:
            self.assertEqual((None, model), split_image_model("  " + model.upper() + "  "))
            self.assertFalse(is_supported_image_model("plus-" + model))
        for value in ("gpt-image-2.6-unknown", "gpt-image-2.6-flare\nInjected", "gpt-image-2.60-flare"):
            self.assertFalse(is_supported_image_model(value))

    def test_model_catalog_exposes_both_new_slugs_with_existing_accounts(self):
        backend = Mock()
        backend.list_models.return_value = {"object": "list", "data": []}
        with patch.object(openai_v1_models, "OpenAIBackendAPI", return_value=backend), \
             patch.object(openai_v1_models.account_service, "list_accounts", return_value=[{"type": "Team"}]):
            models = {item["id"] for item in openai_v1_models.list_models()["data"]}
        self.assertTrue(set(MODELS) <= models)
        self.assertTrue({"gpt-image-2", "gpt-image-2.5"} <= models)
        backend.close.assert_called_once()

    def test_requests_select_supported_transport_without_mutating_global_settings(self):
        for configured in ("voice", "conversation"):
            with patch.object(config, "data", {**config.data, "image_transport": configured}):
                for model in MODELS:
                    request = ConversationRequest(model=model, image_transport=configured)
                    self.assertEqual("conversation", request.image_transport)
                self.assertEqual(configured, ConversationRequest(model="gpt-image-2.5").image_transport)
                self.assertEqual(configured, config.image_transport)

    def test_queued_generate_and_edit_tasks_store_effective_mode(self):
        for mode in ("generate", "edit"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                handler = Mock(return_value={"data": [{"url": "https://example.test/result.png"}]})
                service = ImageTaskService(Path(directory) / "tasks.json",
                                           generation_handler=handler, edit_handler=handler)
                service._log_call = Mock()
                with patch.object(config, "data", {**config.data, "image_transport": "voice"}), \
                     patch("services.image_task_service.image_work_pool") as pool:
                    submit = service.submit_generation if mode == "generate" else service.submit_edit
                    task = submit({"id": "owner"}, client_task_id="new-image26", prompt="test",
                                  model=MODELS[0], size=None)
                    self.assertEqual("conversation", task["image_transport"])
                    run, *args = pool.submit.call_args.args
                    run(*args)
                    self.assertEqual("conversation", handler.call_args.args[0]["_image_transport"])
                reloaded = ImageTaskService(service.path)
                self.assertEqual("conversation", reloaded.list_tasks({"id": "owner"}, ["new-image26"])["items"][0]["image_transport"])


class WebImage26ProtocolTests(unittest.TestCase):
    def setUp(self):
        factory = patch("services.image_generation_stream._make_async_session",
                        side_effect=lambda original, timeout: webimage_test.AsyncRecordingSession(original))
        factory.start()
        self.addCleanup(factory.stop)

    def backend(self, **kwargs):
        return webimage_test.WebImageConversationTransportTests.backend(self, **kwargs)

    def test_generation_and_edit_pass_exact_model_and_new_upstream_contract(self):
        for model in MODELS:
            for images in ([], [webimage_test.reference_image()]):
                for configured in ("voice", "conversation"):
                    with self.subTest(model=model, edit=bool(images), configured=configured):
                        backend = self.backend()
                        backend.image_transport = configured
                        with patch("services.voice_image_transport.stream_voice_image",
                                   side_effect=AssertionError("image26 opened a voice session")), \
                             patch("services.openai_backend_api.browser_sentinel_enabled", return_value=False):
                            self.assertEqual("[DONE]", list(backend._stream_picture_conversation("画猫", model, images))[-1])
                        calls = [kwargs for _, path, kwargs in backend.session.calls if "/f/conversation" in path]
                        self.assertEqual(2, len(calls))
                        prepare, generation = (call["json"] for call in calls)
                        for body in (prepare, generation):
                            self.assertEqual(model, body["model"])
                            self.assertEqual("client-created-root", body["parent_message_id"])
                            self.assertEqual("success", body["client_prepare_state"])
                            self.assertEqual(["picture_v2"], body["system_hints"])
                            self.assertNotIn("conversation_id", body)
                        self.assertEqual("markdown", generation["messages"][0]["metadata"]
                                         ["serialization_metadata"]["render_format"])
                        self.assertEqual("test-conduit", calls[1]["headers"]["X-Conduit-Token"])
                        message = generation["messages"][0]
                        if images:
                            self.assertEqual("multimodal_text", message["content"]["content_type"])
                            self.assertEqual("image/png", message["metadata"]["attachments"][0]["mimeType"])
                            self.assertEqual("file-service://file-reference", message["content"]["parts"][0]["asset_pointer"])
                        self.assertEqual("conversation", backend.image_transport)

    def test_failed_generation_is_never_replayed_or_downgraded(self):
        backend = self.backend(status=404)
        with patch("services.openai_backend_api.browser_sentinel_enabled", return_value=False):
            with self.assertRaises(UpstreamHTTPError):
                list(backend._stream_picture_conversation("画猫", MODELS[1], []))
        generations = [kwargs for _, path, kwargs in backend.session.calls if path == "/backend-api/f/conversation"]
        self.assertEqual(1, len(generations))
        self.assertEqual(MODELS[1], generations[0]["json"]["model"])
        self.assertTrue(backend._webimage_generation_sent)

    def test_new_models_accept_explicit_ok_null_conduit_without_sending_header(self):
        for model in MODELS:
            with self.subTest(model=model):
                backend = self.backend(conduit=None)
                original = backend.session.post

                def post(url, **kwargs):
                    response = original(url, **kwargs)
                    if url.endswith("/conversation/prepare"):
                        response.content = json.dumps({"status": "ok", "conduit_token": None}).encode()
                    return response

                backend.session.post = post
                with patch("services.openai_backend_api.browser_sentinel_enabled", return_value=False):
                    self.assertEqual("[DONE]", list(backend._stream_picture_conversation("draw", model, []))[-1])
                generations = [kwargs for _, path, kwargs in backend.session.calls if path == "/backend-api/f/conversation"]
                self.assertEqual(1, len(generations))
                self.assertNotIn("X-Conduit-Token", generations[0]["headers"])
                self.assertEqual(model, generations[0]["json"]["model"])

    def test_only_explicit_success_null_is_accepted_for_new_model(self):
        bodies = [
            {"conduit_token": None}, {"status": "ok"}, {"status": "error", "conduit_token": None},
            {"status": "ok", "conduit_token": ""}, {"status": "ok", "conduit_token": " "},
            {"status": "ok", "conduit_token": 123}, {"status": "ok", "conduit_token": "bad\r\nheader"},
        ]
        for body in bodies:
            with self.subTest(body=body):
                backend = self.backend()
                response = Mock(status_code=200, json=Mock(return_value=body))
                backend.session.post = Mock(return_value=response)
                with self.assertRaisesRegex(RuntimeError, "conduit token"):
                    WebImageConversationTransport(backend).prepare("draw", Mock(token="test"), MODELS[0])
                backend.session.post.assert_called_once()


class WebImage26EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_compat_entrypoints_capture_new_model_protocol(self):
        router = ai.create_router()
        cases = [
            ("/v1/images/generations", ai.ImageGenerationRequest(model=MODELS[0], prompt="test"), True),
            ("/v1/chat/completions", ai.ChatCompletionRequest(model=MODELS[0], messages=[], _image_transport="voice"), False),
            ("/v1/responses", ai.ResponseCreateRequest(model=MODELS[1], input="test", _image_transport="voice"), False),
        ]
        with patch.object(config, "data", {**config.data, "image_transport": "voice"}), \
             patch.object(ai, "require_identity", return_value={}), patch.object(ai, "LoggedCall", CapturedCall), \
             patch.object(ai, "filter_or_log", AsyncMock()), \
             patch.object(ai, "resolve_image_base_url", return_value="https://example.test"):
            for path, body, request_needed in cases:
                with self.subTest(path=path):
                    endpoint = next(route.endpoint for route in router.routes if route.path == path)
                    args = {"body": body, "authorization": "Bearer synthetic"}
                    if request_needed:
                        args["request"] = Mock()
                    await endpoint(**args)
                    self.assertEqual("conversation", CapturedCall.bodies[-1]["_image_transport"])

    async def test_edit_and_async_task_routes_capture_new_model_protocol(self):
        service = Mock()
        service.submit_generation.return_value = {"status": "queued"}
        service.submit_edit.return_value = {"status": "queued"}
        payload = {"prompt": "test", "model": MODELS[1], "client_task_id": "new-image26", "quality": "auto", "size": None}
        with patch.object(config, "data", {**config.data, "image_transport": "voice"}):
            with patch.object(ai, "require_identity", return_value={}), patch.object(ai, "LoggedCall", CapturedCall), \
                 patch.object(ai, "filter_or_log", AsyncMock()), \
                 patch.object(ai, "parse_image_edit_request", AsyncMock(return_value=(dict(payload), [], []))), \
                 patch.object(ai, "read_image_sources", AsyncMock(return_value=[])), \
                 patch.object(ai, "resolve_image_base_url", return_value="https://example.test"):
                endpoint = next(r.endpoint for r in ai.create_router().routes if r.path == "/v1/images/edits")
                await endpoint(Mock(), "Bearer synthetic")
                self.assertEqual("conversation", CapturedCall.bodies[-1]["_image_transport"])
            with patch.object(image_tasks, "require_identity", return_value={}), \
                 patch.object(image_tasks, "image_task_service", service), \
                 patch.object(image_tasks, "filter_or_log", AsyncMock()), \
                 patch.object(image_tasks, "parse_image_edit_request", AsyncMock(return_value=(dict(payload), [], []))), \
                 patch.object(image_tasks, "read_image_sources", AsyncMock(return_value=[])), \
                 patch.object(image_tasks, "resolve_image_base_url", return_value="https://example.test"):
                router = image_tasks.create_router()
                endpoint = next(r.endpoint for r in router.routes if r.path == "/api/image-tasks/generations")
                await endpoint(image_tasks.ImageGenerationTaskRequest(model=MODELS[0], prompt="test",
                               client_task_id="new-image26"), Mock(), "Bearer synthetic")
                self.assertEqual("conversation", service.submit_generation.call_args.kwargs["image_transport"])
                endpoint = next(r.endpoint for r in router.routes if r.path == "/api/image-tasks/edits")
                await endpoint(Mock(), "Bearer synthetic")
                self.assertEqual("conversation", service.submit_edit.call_args.kwargs["image_transport"])
