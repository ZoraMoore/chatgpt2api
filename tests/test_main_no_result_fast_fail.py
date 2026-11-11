"""主站普通会话结束后的无图快速失败：全合成输入，不访问真实上游。"""
from __future__ import annotations

import asyncio
import base64
from contextlib import ExitStack
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-main-no-result")

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException, Timeout

from services import image_generation_stream as image_stream
from services.config import config
from services.openai_backend_api import ImagePollTimeoutError, OpenAIBackendAPI
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest, ImageGenerationError
from tests.test_terminal_image_integration import CID, image_message, message, png


class MainNoResultFastFailTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.sleep = self.stack.enter_context(patch.object(conversation.time, "sleep"))
        self.task_lookup = self.stack.enter_context(patch.object(
            conversation, "_get_detailed_error_from_tasks", return_value=""))
        self.stack.enter_context(patch.object(conversation, "account_service", Mock()))
        self.stack.enter_context(patch.object(conversation, "logger", Mock()))
        self.stack.enter_context(patch.object(conversation, "_remove_image_conversation_later"))
        self.stack.enter_context(patch.object(config, "data", {
            **config.data, "image_require_reference_postprocess": False,
        }))

    def backend(self, events):
        backend = Mock(spec=OpenAIBackendAPI)
        backend.access_token = "synthetic-no-result-account"
        backend._image_submitted_message_id = "submitted"
        backend.stream_conversation.return_value = iter([
            *(json.dumps(event) for event in events), "[DONE]",
        ])
        backend.resolve_conversation_image_urls.return_value = []
        backend._poll_image_results.return_value = ([], [])
        backend._get_conversation.return_value = {"conversation_id": CID, "messages": []}
        backend.find_conversation_by_prompt.return_value = ""
        backend.download_image_bytes.return_value = [png()]
        return backend

    def rows(self, *messages):
        return [{"conversation_id": CID, "message": item} for item in messages]

    def run_generation(self, backend, *, images=None, **kwargs):
        return list(conversation.stream_image_outputs(backend, ConversationRequest(
            model="gpt-image-2", prompt="synthetic no-result request", images=images or [],
            response_format="b64_json", image_transport="conversation",
        ), **kwargs))

    def assert_no_followup_or_replay(self, backend):
        backend._get_conversation.assert_not_called()
        backend._poll_image_results.assert_not_called()
        backend.find_conversation_by_prompt.assert_not_called()
        backend._post_image_conversation.assert_not_called()
        backend._start_image_generation.assert_not_called()
        self.task_lookup.assert_not_called()
        self.sleep.assert_not_called()
        backend.stream_conversation.assert_called_once()

    def assert_no_image_failure(self, backend, *, expected_id=CID, images=None):
        with self.assertRaises(ImageGenerationError) as raised:
            self.run_generation(backend, images=images)
        self.assertEqual(expected_id, raised.exception.conversation_id)
        self.assertRegex(str(raised.exception), r"[\u4e00-\u9fff]")
        self.assertIn("失败", str(raised.exception))
        self.assertRegex(str(raised.exception), r"图片|生图")
        backend.resolve_conversation_image_urls.assert_not_called()
        backend.download_image_bytes.assert_not_called()
        backend.download_final_sandbox_images.assert_not_called()
        self.assert_no_followup_or_replay(backend)

    def test_done_without_any_conversation_fails_immediately(self):
        self.assert_no_image_failure(self.backend([]), expected_id="")

    def test_done_with_empty_conversation_fails_immediately(self):
        self.assert_no_image_failure(self.backend([{"conversation_id": CID}]))

    def test_text_only_final_is_reported_as_image_generation_failure(self):
        final = message("final", channel="final", end_turn=True,
                        content={"content_type": "text", "parts": ["I can describe this image."]})
        self.assert_no_image_failure(self.backend(self.rows(final)))

    def test_reference_request_with_image_gen_marker_does_not_wait(self):
        events = [{"conversation_id": CID, "type": "server_ste_metadata",
                   "metadata": {"tool_invoked": True, "turn_use_case": "image gen"}}]
        self.assert_no_image_failure(self.backend(events), images=["synthetic reference"])

    def test_text_tool_arguments_do_not_trigger_history_polling(self):
        final = message("final", channel="final", end_turn=True, content={
            "content_type": "text",
            "parts": ['{"size":"1024x1024","referenced_image_ids":["reference"]}'],
        })
        self.assert_no_image_failure(self.backend(self.rows(final)), images=["synthetic reference"])

    def test_uploaded_reference_id_cannot_be_delivered_as_generated_image(self):
        user = message("submitted", "user", content={"content_type": "multimodal_text", "parts": [
            {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-reference"},
        ]})
        events = [{"conversation_id": CID, "type": "server_ste_metadata",
                   "metadata": {"tool_invoked": True, "turn_use_case": "image gen"}}, *self.rows(user)]
        self.assert_no_image_failure(self.backend(events), images=["synthetic reference"])

    def test_new_conversation_sets_no_result_policy_before_starting_stream(self):
        backend = self.backend([])

        def start_stream(**kwargs):
            self.assertIs(True, getattr(backend, "_image_fail_without_result_on_stream_end", None))
            return iter(["[DONE]"])

        backend.stream_conversation.side_effect = start_stream
        self.assert_no_image_failure(backend, expected_id="")

    def test_code_tool_without_image_does_not_start_snapshot_wait(self):
        tool = message("code-output", "tool", name="container.exec", content={
            "content_type": "execution_output", "text": "No output", "parts": [],
        })
        backend = self.backend(self.rows(tool, message("final", channel="final", end_turn=True)))
        self.assert_no_image_failure(backend)

    def test_generated_image_id_is_resolved_once_without_polling(self):
        backend = self.backend(self.rows(image_message(), message("final", channel="final", end_turn=True)))
        backend.resolve_conversation_image_urls.return_value = ["https://example.test/final.png"]
        results = [output for output in self.run_generation(backend) if output.kind == "result"]
        self.assertEqual(1, len(results))
        self.assertEqual(png(), base64.b64decode(results[0].data[0]["b64_json"]))
        backend.resolve_conversation_image_urls.assert_called_once()
        args, kwargs = backend.resolve_conversation_image_urls.call_args
        self.assertEqual((CID, [], ["file-final"]), args)
        self.assertIs(False, kwargs.get("poll"))
        backend.download_image_bytes.assert_called_once_with(["https://example.test/final.png"])
        self.assert_no_followup_or_replay(backend)

    def test_explicit_upstream_technical_failure_preserves_its_error(self):
        failure = message("failed-image", "tool", name="synthetic.image", content={
            "content_type": "text", "parts": ["We experienced an error when generating images."],
        })
        backend = self.backend(self.rows(failure, message("final", channel="final", end_turn=True)))
        with self.assertRaises(ImageGenerationError) as raised:
            self.run_generation(backend)
        self.assertEqual("image_generation_failed", raised.exception.code)
        self.assertIn("技术错误", str(raised.exception))
        backend.resolve_conversation_image_urls.assert_not_called()
        self.assert_no_followup_or_replay(backend)

    def test_explicit_resume_keeps_polling_the_original_pending_conversation(self):
        backend = self.backend([])
        with self.assertRaises(ImagePollTimeoutError):
            self.run_generation(backend, resume_conversation_id=CID, poll_timeout_secs=9)
        backend.resolve_conversation_image_urls.assert_called_once()
        args, kwargs = backend.resolve_conversation_image_urls.call_args
        self.assertEqual((CID, [], []), args)
        self.assertIsNot(False, kwargs.get("poll"))
        self.assertGreater(kwargs["poll_timeout_secs"], 0)
        self.assertLessEqual(kwargs["poll_timeout_secs"], 9)
        backend._get_conversation.assert_called_once()
        self.assertEqual(CID, backend._get_conversation.call_args.args[0])
        backend.stream_conversation.assert_not_called()
        backend._post_image_conversation.assert_not_called()
        backend._start_image_generation.assert_not_called()
        self.task_lookup.assert_not_called()

    def test_explicit_resume_still_delivers_the_original_completed_image(self):
        backend = self.backend([])
        backend._get_conversation.side_effect = [
            {"conversation_id": CID, "messages": []},
            {"conversation_id": CID, "messages": [image_message(), message("final", channel="final", end_turn=True)]},
        ]
        backend.resolve_conversation_image_urls.return_value = ["https://example.test/final.png"]
        results = [output for output in self.run_generation(
            backend, resume_conversation_id=CID, poll_timeout_secs=9) if output.kind == "result"]
        self.assertEqual(1, len(results))
        self.assertEqual(png(), base64.b64decode(results[0].data[0]["b64_json"]))
        backend.resolve_conversation_image_urls.assert_called_once()
        self.assertEqual(2, backend._get_conversation.call_count)
        for call in backend._get_conversation.call_args_list:
            self.assertEqual(CID, call.args[0])
        backend.stream_conversation.assert_not_called()
        backend._post_image_conversation.assert_not_called()
        backend._start_image_generation.assert_not_called()


class ScriptedImageSession:
    """只替代 HTTP 边界，保留流状态机、历史校验及连接回收的真实执行。"""

    def __init__(self, events, document, failure=None):
        self.cookies = requests.Cookies()
        self.response = requests.Response()
        self.response.status_code = 200
        self.response.aclose = AsyncMock()

        async def lines():
            for event in events:
                yield ("data: " + json.dumps(event)).encode()
            if failure is not None:
                raise failure

        async def read_document(url, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.aclose = AsyncMock()
            kwargs["content_callback"](json.dumps(document).encode())
            return response

        self.response.aiter_lines = lines
        self.post = AsyncMock(return_value=self.response)
        self.get = AsyncMock(side_effect=read_document)
        self.close = AsyncMock()


class MainNoResultStreamEndTests(unittest.TestCase):
    def run_stream(self, *, generated=False, fail_without_result=True, failure=None):
        submitted = message("submitted", "user", content={"content_type": "text", "parts": ["synthetic"]})
        complete = [submitted, image_message(), message("final", channel="final", end_turn=True)]
        document = {"conversation_id": CID, "async_status": 4, "current_node": "final", "messages": complete}
        observed = complete[:2] if generated else complete[:1]
        session = ScriptedImageSession(
            [{"conversation_id": CID, "message": item} for item in observed], document, failure)
        backend = SimpleNamespace(
            base_url="https://example.test", session=SimpleNamespace(cookies=requests.Cookies()),
            image_conversation_id="", _image_submitted_message_id="submitted")
        if fail_without_result:
            backend._image_fail_without_result_on_stream_end = True

        async def collect():
            return [value async for value in image_stream._stream(
                backend, {"messages": [{"id": "submitted"}]}, {"Accept": "text/event-stream"}, 600)]

        with patch.object(image_stream, "_make_async_session", return_value=session), \
                patch.object(image_stream, "logger", Mock()):
            # 真实请求预算仍为 600 秒；测试只容许立即完成或立即恢复，不能靠缩短配置掩盖问题。
            values = asyncio.run(asyncio.wait_for(collect(), timeout=0.5))
        session.post.assert_awaited_once()
        self.assertEqual("https://example.test/backend-api/f/conversation", session.post.call_args.args[0])
        self.assertEqual({"messages": [{"id": "submitted"}]}, session.post.call_args.kwargs["json"])
        session.response.aclose.assert_awaited_once()
        session.close.assert_awaited_once()
        return session, values

    def test_eof_without_image_does_not_start_history_recovery(self):
        session, values = self.run_stream()
        session.get.assert_not_awaited()
        self.assertFalse(any("file-final" in value for value in values))

    def test_interrupted_stream_without_image_does_not_start_history_recovery(self):
        for failure in (RequestException("synthetic stream reset", code=56), Timeout("synthetic read timeout", code=28)):
            with self.subTest(failure=type(failure).__name__):
                session, values = self.run_stream(failure=failure)
                session.get.assert_not_awaited()
                self.assertFalse(any("file-final" in value for value in values))

    def test_eof_after_generated_image_keeps_original_conversation_recovery(self):
        session, values = self.run_stream(generated=True)
        session.get.assert_awaited_once()
        self.assertTrue(session.get.call_args.args[0].endswith("/" + CID))
        self.assertIn("[DONE]", values)
        self.assertTrue(any('"id": "final"' in value for value in values))

    def test_legacy_stream_without_policy_keeps_original_conversation_recovery(self):
        session, values = self.run_stream(fail_without_result=False)
        session.get.assert_awaited_once()
        self.assertTrue(session.get.call_args.args[0].endswith("/" + CID))
        self.assertIn("[DONE]", values)
        self.assertTrue(any("file-final" in value for value in values))


if __name__ == "__main__":
    unittest.main()
