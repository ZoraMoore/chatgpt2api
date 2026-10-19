#!/usr/bin/env python3
"""网页版图片模型注册与路由自测（不联网、不写真实数据）。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CHATGPT2API_AUTH_KEY", "image-model-selftest-placeholder-key")

from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from services.protocol import openai_v1_models
from utils.helper import (
    WEB_IMAGE_MODELS,
    is_codex_image_model,
    is_firefly_image_model,
    is_supported_image_model,
    split_image_model,
)


class ImageModelRoutingTest(unittest.TestCase):
    def test_gpt_image_25_uses_unrestricted_web_route(self) -> None:
        self.assertIn("gpt-image-2.5", WEB_IMAGE_MODELS)
        self.assertEqual((None, "gpt-image-2.5"), split_image_model("gpt-image-2.5"))
        self.assertTrue(is_supported_image_model("gpt-image-2.5"))
        self.assertFalse(is_codex_image_model("gpt-image-2.5"))
        self.assertFalse(is_firefly_image_model("gpt-image-2.5"))

    def test_web_image_aliases_use_captured_picture_protocol(self) -> None:
        backend = object.__new__(OpenAIBackendAPI)
        for model in ("gpt-image-2", "gpt-image-2.5"):
            with self.subTest(model=model):
                self.assertEqual("gpt-6-thinking", backend._image_model_slug(model))
                payload = backend._image_generation_payload("test prompt", model)
                self.assertEqual("success", payload["client_prepare_state"])
                self.assertEqual(["picture_v2"], payload["system_hints"])
                self.assertEqual("extended", payload["thinking_effort"])
                self.assertEqual(["picture_v2"], payload["messages"][0]["metadata"]["system_hints"])
                self.assertNotIn("model_response_contracts", payload)

    def test_image_headers_forward_all_sentinel_tokens(self) -> None:
        backend = object.__new__(OpenAIBackendAPI)
        backend._headers = lambda _path, extra=None: dict(extra or {})
        headers = backend._image_headers(
            "/backend-api/f/conversation",
            ChatRequirements(token="chat", proof_token="proof", turnstile_token="turnstile", so_token="so"),
            accept="text/event-stream",
        )
        self.assertEqual("chat", headers["OpenAI-Sentinel-Chat-Requirements-Token"])
        self.assertEqual("proof", headers["OpenAI-Sentinel-Proof-Token"])
        self.assertEqual("turnstile", headers["OpenAI-Sentinel-Turnstile-Token"])
        self.assertEqual("so", headers["OpenAI-Sentinel-SO-Token"])
        self.assertNotIn("X-Conduit-Token", headers)

    def test_chat_requirements_finalize_uses_captured_field_names(self) -> None:
        class FakeResponse:
            status_code = 200
            headers = {}
            text = ""

            def __init__(self, payload: dict) -> None:
                self.payload = payload

            def json(self) -> dict:
                return self.payload

        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[dict] = []
                self.responses = [
                    FakeResponse({
                        "prepare_token": "prepare",
                        "proofofwork": {"required": False},
                        "turnstile": {"required": False},
                    }),
                    FakeResponse({"token": "requirements"}),
                ]

            def post(self, _url: str, **kwargs):
                self.calls.append(kwargs)
                return self.responses.pop(0)

        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "test-access-token"
        backend.base_url = "https://chatgpt.com"
        backend.user_agent = "test-user-agent"
        backend.pow_script_sources = []
        backend.pow_data_build = ""
        backend.session = FakeSession()
        backend._headers = lambda _path, extra=None: dict(extra or {})

        requirements = backend._get_chat_requirements()

        self.assertEqual("requirements", requirements.token)
        self.assertEqual(
            {"prepare_token": "prepare", "proofofwork": "", "turnstile": ""},
            backend.session.calls[1]["json"],
        )

    def test_image_poll_uses_captured_plural_conversation_path(self) -> None:
        class FakeResponse:
            status_code = 200
            headers = {}
            text = ""

            def json(self) -> dict:
                return {"conversation_id": "conversation-id"}

        class FakeSession:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def get(self, url: str, **_kwargs):
                self.urls.append(url)
                return FakeResponse()

        backend = object.__new__(OpenAIBackendAPI)
        backend.base_url = "https://chatgpt.com"
        backend.session = FakeSession()
        backend._headers = lambda _path, extra=None: dict(extra or {})

        result = backend._get_conversation("conversation-id")

        self.assertEqual("conversation-id", result["conversation_id"])
        self.assertEqual(
            ["https://chatgpt.com/backend-api/conversations/conversation-id"],
            backend.session.urls,
        )

    def test_model_list_exposes_all_web_image_models_when_accounts_exist(self) -> None:
        accounts = [{"access_token": "test-token", "status": "正常", "quota": 1, "source_type": "web"}]
        with (
            patch.object(OpenAIBackendAPI, "list_models", return_value={"object": "list", "data": []}),
            patch.object(OpenAIBackendAPI, "close", return_value=None),
            patch.object(openai_v1_models.account_service, "list_accounts", return_value=accounts),
        ):
            result = openai_v1_models.list_models()

        model_ids = {item["id"] for item in result["data"]}
        self.assertTrue(WEB_IMAGE_MODELS.issubset(model_ids))


if __name__ == "__main__":
    unittest.main()
