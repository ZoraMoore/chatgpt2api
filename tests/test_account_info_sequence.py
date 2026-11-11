from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import Mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")

from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI


class AccountInfoSequenceTests(unittest.TestCase):
    def setUp(self):
        self.backend = object.__new__(OpenAIBackendAPI)
        self.backend.access_token = "synthetic-info-sequence-token"
        self.backend._get_me = Mock(return_value={"email": "sample@example.invalid", "id": "sample-user"})
        self.backend._get_conversation_init = Mock(return_value={
            "limits_progress": [{"feature_name": "image_gen", "remaining": 120,
                                 "reset_after": "2026-10-17T10:00:00+00:00"}],
            "default_model_slug": "sample-model",
        })
        self.backend._get_default_account = Mock(return_value={"plan_type": "edu_plus"})

    def test_account_queries_share_the_callers_session_thread(self):
        caller_thread = threading.get_ident()
        observed = []
        for method in (self.backend._get_me, self.backend._get_conversation_init,
                       self.backend._get_default_account):
            result = method.return_value
            method.side_effect = lambda payload=result: observed.append(threading.get_ident()) or payload
        self.backend.get_user_info()
        self.assertEqual([caller_thread] * 3, observed)

    def test_success_preserves_existing_information_and_quota_contract(self):
        result = self.backend.get_user_info()
        self.assertEqual("sample@example.invalid", result["email"])
        self.assertEqual("sample-user", result["user_id"])
        self.assertEqual("edu_plus", result["type"])
        self.assertEqual(120, result["quota"])
        self.assertEqual("正常", result["status"])
        self.assertEqual("2026-10-17T10:00:00+00:00", result["restore_at"])

    def test_invalid_login_stops_before_additional_account_requests(self):
        self.backend._get_me.side_effect = InvalidAccessTokenError("synthetic invalid login")
        with self.assertRaises(InvalidAccessTokenError):
            self.backend.get_user_info()
        self.backend._get_conversation_init.assert_not_called()
        self.backend._get_default_account.assert_not_called()

    def test_quota_query_failure_stops_before_plan_query(self):
        self.backend._get_conversation_init.side_effect = RuntimeError("conversation/init failed: HTTP 403")
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            self.backend.get_user_info()
        self.backend._get_default_account.assert_not_called()


if __name__ == "__main__":
    unittest.main()
