from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-image-mode-key")

from api import ai, image_tasks
from services.config import config
from services.image_task_service import ImageTaskService


class CapturedCall:
    bodies = []
    def __init__(self, *args, **kwargs):
        pass
    async def run(self, handler, body, **kwargs):
        self.bodies.append(body)
        return {"data": []}


class ImageTransportEntrypointTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_and_compat_endpoints_use_global_not_client_override(self):
        router = ai.create_router()
        cases = [("/v1/images/generations", ai.ImageGenerationRequest(prompt="test"), True),
                 ("/v1/chat/completions", ai.ChatCompletionRequest(model="gpt-image-2", messages=[], _image_transport="conversation"), False),
                 ("/v1/responses", ai.ResponseCreateRequest(model="gpt-image-2", input="test", _image_transport="conversation"), False)]
        with patch.object(config, "data", {**config.data, "image_transport": "voice"}), \
             patch.object(ai, "require_identity", return_value={}), patch.object(ai, "LoggedCall", CapturedCall), \
             patch.object(ai, "filter_or_log", AsyncMock()), patch.object(ai, "resolve_image_base_url", return_value="https://example.test"):
            for path, body, needs_request in cases:
                with self.subTest(path=path):
                    endpoint = next(r.endpoint for r in router.routes if r.path == path)
                    args = {"body": body, "authorization": "Bearer test"}
                    if needs_request:
                        args["request"] = Mock()
                    await endpoint(**args)
                    self.assertEqual("voice", CapturedCall.bodies[-1].get("_image_transport"))

    async def test_edit_captures_mode_before_async_input_processing(self):
        endpoint = next(r.endpoint for r in ai.create_router().routes if r.path == "/v1/images/edits")
        async def parse(_):
            config.data["image_transport"] = "conversation"
            return {"prompt": "test", "model": "gpt-image-2", "_image_transport": "conversation"}, [], []
        with patch.object(config, "data", {**config.data, "image_transport": "voice"}), \
             patch.object(ai, "require_identity", return_value={}), patch.object(ai, "LoggedCall", CapturedCall), \
             patch.object(ai, "filter_or_log", AsyncMock()), patch.object(ai, "parse_image_edit_request", parse), \
             patch.object(ai, "read_image_sources", AsyncMock(return_value=[])), \
             patch.object(ai, "resolve_image_base_url", return_value="https://example.test"):
            await endpoint(Mock(), "Bearer test")
            self.assertEqual("voice", CapturedCall.bodies[-1].get("_image_transport"))

    async def test_async_generation_route_captures_mode_before_filter_wait(self):
        endpoint = next(r.endpoint for r in image_tasks.create_router().routes if r.path == "/api/image-tasks/generations")
        service = Mock()
        service.submit_generation.return_value = {"status": "queued"}
        async def check(*args):
            config.data["image_transport"] = "conversation"
        with patch.object(config, "data", {**config.data, "image_transport": "voice"}), \
             patch.object(image_tasks, "require_identity", return_value={}), patch.object(image_tasks, "image_task_service", service), \
             patch.object(image_tasks, "filter_or_log", check), patch.object(image_tasks, "resolve_image_base_url", return_value="https://example.test"):
            await endpoint(image_tasks.ImageGenerationTaskRequest(client_task_id="test", prompt="test"), Mock(), "Bearer test")
            self.assertEqual("voice", service.submit_generation.call_args.kwargs.get("image_transport"))


class ImageTransportTaskTests(unittest.TestCase):
    def test_queued_tasks_keep_submission_mode_after_global_switch_and_reload(self):
        for operation in ("generate", "edit"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                handler = Mock(return_value={"data": [{"url": "https://example.test/result.png"}]})
                service = ImageTaskService(Path(directory) / "tasks.json", generation_handler=handler, edit_handler=handler)
                service._log_call = Mock()
                with patch.object(config, "data", {**config.data, "image_transport": "voice"}), \
                     patch("services.image_task_service.image_work_pool") as pool:
                    submit = service.submit_generation if operation == "generate" else service.submit_edit
                    task = submit({"id": "owner"}, client_task_id="test", prompt="test", model="gpt-image-2", size=None)
                    self.assertEqual("voice", task.get("image_transport"))
                    config.data["image_transport"] = "conversation"
                    duplicate = submit({"id": "owner"}, client_task_id="test", prompt="changed", model="gpt-image-2", size=None)
                    self.assertEqual("voice", duplicate.get("image_transport"))
                    pool.submit.assert_called_once()
                    run, *args = pool.submit.call_args.args
                    run(*args)
                    self.assertEqual("voice", handler.call_args.args[0].get("_image_transport"))
                reloaded = ImageTaskService(service.path)
                self.assertEqual("voice", reloaded.list_tasks({"id": "owner"}, ["test"])["items"][0].get("image_transport"))
