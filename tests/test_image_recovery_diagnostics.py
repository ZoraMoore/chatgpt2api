"""只用合成文档和离线响应验证恢复原因，诊断不得携带原始请求数据。"""
from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")

from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests.exceptions import RequestException, Timeout

from services import image_generation_stream as stream
from services.protocol.final_image import FinalImageTracker
from tests.test_image_stream_recovery import CID, document


SECRET_MARKER = "secretmarker-do-not-log"


def sensitive_document():
    value = document()
    value["messages"][0]["content"]["parts"] = [SECRET_MARKER]
    value["access_token"] = SECRET_MARKER
    return value


class CompletedMessagesDiagnosticTests(unittest.TestCase):
    def test_original_complete_turn_has_safe_diagnostic(self):
        diagnostic = {"reason": "old_failure", "old_secret": SECRET_MARKER}
        result = stream._completed_messages(
            sensitive_document(), CID, "submitted", diagnostic=diagnostic)
        self.assertEqual(["submitted", "image", "final"], [m["id"] for m in result])
        self.assertEqual("complete", diagnostic["reason"])
        self.assertNotIn("old_secret", diagnostic)
        self.assertNotIn(SECRET_MARKER, json.dumps(diagnostic))

    def test_rejected_documents_report_the_exact_scope_or_completion_reason(self):
        for reason in (
            "conversation_mismatch", "submission_mismatch", "invalid_branch",
            "tool_incomplete", "final_incomplete", "observed_tools_missing", "calls_missing",
        ):
            with self.subTest(reason=reason):
                value = sensitive_document()
                options = {}
                if reason == "conversation_mismatch":
                    value["conversation_id"] = "another-conversation"
                elif reason == "submission_mismatch":
                    value["messages"][0]["id"] = "another-submission"
                elif reason == "invalid_branch":
                    value["current_node"] = "missing-branch-node"
                elif reason == "tool_incomplete":
                    value["messages"][1]["status"] = "in_progress"
                elif reason == "final_incomplete":
                    value["messages"][-1]["end_turn"] = False
                elif reason == "observed_tools_missing":
                    observed = FinalImageTracker()
                    observed.update({"message": {**value["messages"][1], "id": "newer-image"}})
                    options["observed"] = observed
                elif reason == "calls_missing":
                    options["required_tool_calls"] = {"newer-call": "python"}
                diagnostic = {"reason": "complete", "old_secret": SECRET_MARKER}
                result = stream._completed_messages(
                    value, CID, "submitted", diagnostic=diagnostic, **options)
                self.assertIsNone(result)
                self.assertEqual(reason, diagnostic["reason"])
                self.assertNotIn("old_secret", diagnostic)
                self.assertNotIn(SECRET_MARKER, json.dumps(diagnostic))

    def test_pending_status_cannot_mask_wrong_conversation_or_user(self):
        for reason in ('conversation_mismatch', 'submission_mismatch'):
            value = document()
            value['async_status'] = 3
            if reason == 'conversation_mismatch':
                value['conversation_id'] = 'other-conversation'
            else:
                value['messages'][0]['id'] = 'other-user'
            diagnostic = {}
            self.assertIsNone(stream._completed_messages(value, CID, 'submitted', diagnostic=diagnostic))
            self.assertEqual(reason, diagnostic['reason'])

    def test_new_document_check_cannot_reuse_previous_success(self):
        diagnostic = {}
        self.assertIsNotNone(stream._completed_messages(
            document(), CID, "submitted", diagnostic=diagnostic))
        self.assertEqual("complete", diagnostic["reason"])
        self.assertIsNone(stream._completed_messages(
            document(), CID, "another-submission", diagnostic=diagnostic))
        self.assertEqual("submission_mismatch", diagnostic["reason"])


class ReadCompletedTurnDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = SimpleNamespace(
            image_conversation_id=CID,
            base_url=f"https://{SECRET_MARKER}.invalid",
            access_token=SECRET_MARKER,
        )

    def make_session(self, *, status=200, body=None, error=None):
        if body is None:
            body = json.dumps(sensitive_document()).encode()
        response = SimpleNamespace(status_code=status, content=b"", aclose=AsyncMock())
        response.json = lambda: json.loads(response.content)

        async def get(_url, **kwargs):
            if error is not None:
                raise error
            collected = kwargs["content_callback"](body)
            if collected == CURL_WRITEFUNC_ERROR:
                raise RequestException(f"write aborted: {SECRET_MARKER}", code=23)
            return response

        return SimpleNamespace(get=AsyncMock(side_effect=get)), response

    async def read(self, session, diagnostic):
        return await stream._read_completed_turn(
            session, self.backend, "submitted", 1.0, diagnostic=diagnostic)

    def assert_safe_diagnostic(self, diagnostic, reason, status=None):
        self.assertEqual(reason, diagnostic["reason"])
        self.assertIsInstance(diagnostic["duration_ms"], (int, float))
        self.assertGreaterEqual(diagnostic["duration_ms"], 0)
        if status is not None:
            self.assertEqual(status, diagnostic["http_status"])
        self.assertNotIn("old_secret", diagnostic)
        self.assertNotIn(SECRET_MARKER, json.dumps(diagnostic))

    async def test_success_retains_http_status_and_duration_without_payload(self):
        session, response = self.make_session()
        diagnostic = {"reason": "old_failure", "old_secret": SECRET_MARKER,
                      "exception_type": "Timeout", "exception_code": 28}
        result = await self.read(session, diagnostic)
        self.assertEqual(["submitted", "image", "final"], [m["id"] for m in result])
        self.assert_safe_diagnostic(diagnostic, "complete", 200)
        self.assertNotIn("exception_type", diagnostic)
        self.assertNotIn("exception_code", diagnostic)
        response.aclose.assert_awaited_once()

    async def test_http_403_reports_status_without_response_body(self):
        session, response = self.make_session(status=403, body=SECRET_MARKER.encode())
        diagnostic = {"reason": "complete", "old_secret": SECRET_MARKER}
        self.assertIsNone(await self.read(session, diagnostic))
        self.assert_safe_diagnostic(diagnostic, "http_status", 403)
        response.aclose.assert_awaited_once()

    async def test_invalid_json_reports_only_exception_class(self):
        session, response = self.make_session(body=SECRET_MARKER.encode())
        diagnostic = {"reason": "complete", "old_secret": SECRET_MARKER}
        self.assertIsNone(await self.read(session, diagnostic))
        self.assert_safe_diagnostic(diagnostic, "invalid_json", 200)
        self.assertEqual("JSONDecodeError", diagnostic["exception_type"])
        response.aclose.assert_awaited_once()

    async def test_transport_timeout_reports_safe_exception_code(self):
        session, _ = self.make_session(error=Timeout(f"timeout: {SECRET_MARKER}", code=28))
        diagnostic = {"reason": "complete", "http_status": 200, "old_secret": SECRET_MARKER}
        self.assertIsNone(await self.read(session, diagnostic))
        self.assert_safe_diagnostic(diagnostic, "transport_error")
        self.assertEqual("Timeout", diagnostic["exception_type"])
        self.assertEqual(28, diagnostic["exception_code"])
        self.assertNotIn("http_status", diagnostic)

    async def test_document_limit_is_distinguished_from_transport_failure(self):
        session, _ = self.make_session(body=(SECRET_MARKER * 4).encode())
        diagnostic = {"reason": "complete", "old_secret": SECRET_MARKER}
        with patch.object(stream, "_MAX_DOCUMENT_BYTES", 32):
            self.assertIsNone(await self.read(session, diagnostic))
        self.assert_safe_diagnostic(diagnostic, "document_too_large")
        self.assertEqual("RequestException", diagnostic["exception_type"])
        self.assertEqual(23, diagnostic["exception_code"])

    async def test_new_http_probe_cannot_reuse_previous_success(self):
        diagnostic = {}
        success, _ = self.make_session()
        self.assertIsNotNone(await self.read(success, diagnostic))
        self.assert_safe_diagnostic(diagnostic, "complete", 200)
        failure, _ = self.make_session(error=Timeout(SECRET_MARKER, code=28))
        self.assertIsNone(await self.read(failure, diagnostic))
        self.assert_safe_diagnostic(diagnostic, "transport_error")
        self.assertNotIn("http_status", diagnostic)


if __name__ == "__main__":
    unittest.main()
