from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "isolated-browser-test-key")

from services.account_service import AccountService
from services.browser_sentinel import (
    BrowserSentinelContext, BrowserSentinelError, apply_web_session_cookies,
    has_web_session, normalize_web_session_token,
)


SESSION = "a" * 32 + ".." + "b" * 16 + "." + "c" * 16 + "." + "d" * 16
TICKET = "98d945c2-4688-4254-b058-3bc965de8403"


class BrowserSentinelTests(unittest.TestCase):
    def context(self):
        with patch.dict(os.environ, {"CHATGPT2API_SENTINEL_SOCKET": "/run/sentinel/test.sock"}):
            return BrowserSentinelContext(html="<html></html>", user_agent="fixture-agent",
                                          device_id=TICKET, timezone="UTC")

    def test_cookie_chunking_roundtrips_without_header_injection(self):
        for token, expected_chunks in [(SESSION, 1), (SESSION + "x" * 4500, 2)]:
            session = Mock()
            apply_web_session_cookies(session, token)
            calls = session.cookies.set.call_args_list
            self.assertEqual(expected_chunks, len(calls))
            self.assertEqual(token, "".join(call.args[1] for call in calls))
            for index, call in enumerate(calls):
                suffix = f".{index}" if expected_chunks > 1 else ""
                self.assertEqual("__Secure-next-auth.session-token" + suffix, call.args[0])
                self.assertLessEqual(len(call.args[1]), 3933)
                self.assertEqual("chatgpt.com", call.kwargs["domain"])

    def test_import_maps_session_field_and_rejects_malformed_values(self):
        result = AccountService._prepare_account_payload({"accessToken": "fixture-token", "sessionToken": SESSION})
        self.assertEqual(SESSION, result["web_session_token"])
        self.assertNotIn("sessionToken", result)
        for value in [123, {}, "invalid", SESSION + "\r\nsecret-fixture", "中" + SESSION, SESSION + "x" * 16384]:
            with self.assertRaises(ValueError) as caught:
                normalize_web_session_token(value)
            self.assertNotIn("secret-fixture", str(caught.exception))
            self.assertFalse(has_web_session({"web_session_token": value}))

    def test_worker_lease_released_when_upstream_prepare_fails(self):
        context = self.context()
        with patch.object(context, "_request", side_effect=[{"ticket": TICKET, "p": "gAAAAACe30="}, {"ok": True}]) as request:
            with self.assertRaisesRegex(RuntimeError, "fixture upstream failure"):
                with context:
                    raise RuntimeError("fixture upstream failure")
        self.assertEqual("/v1/release", request.call_args.args[0])
        self.assertEqual("", context.ticket)

    def test_invalid_worker_result_releases_context(self):
        context = self.context()
        with patch.object(context, "_request", side_effect=[{"ticket": TICKET, "p": "bad\nheader"}, {"ok": True}]) as request:
            with self.assertRaises(BrowserSentinelError):
                context.__enter__()
        self.assertEqual("/v1/release", request.call_args.args[0])

    def test_sdk_timeout_counter_cannot_be_used_as_challenge_token(self):
        context = self.context()
        context.ticket = TICKET
        with patch.object(context, "_request", return_value={"proof": "", "turnstile": "1024"}):
            with self.assertRaises(BrowserSentinelError):
                context.solve({"turnstile": {"required": True}})

    def test_native_account_preference_preserves_explicit_plan_filter(self):
        service = object.__new__(AccountService)
        service._accounts = {
            "pro-token": {"access_token": "pro-token", "type": "pro", "source_type": "web", "status": "正常", "quota": 10},
            "plus-token": {"access_token": "plus-token", "type": "plus", "source_type": "web", "status": "正常", "quota": 10,
                           "web_session_token": SESSION},
        }
        with patch.dict(os.environ, {"CHATGPT2API_SENTINEL_SOCKET": "/run/sentinel/test.sock"}):
            self.assertEqual(["plus-token"], service._list_ready_candidate_tokens())
            self.assertEqual(["pro-token"], service._list_ready_candidate_tokens(plan_type="pro"))
        with patch.dict(os.environ, {"CHATGPT2API_SENTINEL_SOCKET": ""}):
            self.assertEqual(["pro-token", "plus-token"], service._list_ready_candidate_tokens())


if __name__ == "__main__":
    unittest.main()
