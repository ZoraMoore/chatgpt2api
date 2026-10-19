from __future__ import annotations

import base64
import json
import os
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "isolated-sentinel-test-key")

from services.openai_backend_api import OpenAIBackendAPI
from utils.browser_fingerprint import browser_fingerprint
from utils.turnstile import TurnstileChallengeError, solve_turnstile_token


def encrypted(program, key="fixture-key"):
    raw = json.dumps(program, separators=(",", ":"))
    return base64.b64encode(bytes(ord(char) ^ ord(key[index % len(key)]) for index, char in enumerate(raw))).decode()


class TurnstileTests(unittest.TestCase):
    def solve(self, program, **kwargs):
        token = solve_turnstile_token(encrypted(program), "fixture-key", strict=True, **kwargs)
        return base64.b64decode(token).decode("latin1")

    def test_result_and_javascript_latin1_encoding(self):
        self.assertEqual("working", self.solve([[2, 100, "working"], [7, 3, 100]]))
        self.assertEqual("é", self.solve([[3, "é"]]))

    def test_replaced_instruction_queue_is_executed(self):
        nested = json.dumps([[3, "replacement"]])
        self.assertEqual("replacement", self.solve([[2, 100, nested], [14, 9, 100], [3, "old queue"]]))

    def test_try_call_uses_literal_arguments(self):
        self.assertEqual("literal", self.solve([[13, 110, 2, 100, "literal"], [7, 3, 100]]))

    def test_conditional_calls_use_literal_arguments(self):
        self.assertEqual("equal", self.solve([[2, 100, 1], [2, 101, 1], [20, 100, 101, 2, 102, "equal"], [7, 3, 102]]))

    def test_defined_null_is_different_from_missing_register(self):
        program = [[2, 100, None], [2, 101, "before"], [23, 100, 2, 101, "defined null"],
                   [23, 999, 2, 101, "wrong"], [7, 3, 101]]
        self.assertEqual("defined null", self.solve(program))

    def test_subroutine_restores_remaining_queue(self):
        self.assertEqual("after", self.solve([[2, 100, "before"], [22, 110, [[2, 100, "after"]]], [7, 3, 100]]))

    def test_function_arguments_return_value_and_await(self):
        program = [[30, 100, 102, [101], [[8, 102, 101]]], [2, 103, "async result"],
                   [17, 104, 100, 103], [34, 105, 104], [7, 3, 105]]
        self.assertEqual("async result", self.solve(program))

    def test_native_method_binding_and_object_json(self):
        program = [[2, 100, "Object"], [6, 101, 10, 100], [2, 102, "keys"], [24, 103, 101, 102],
                   [2, 104, {"a": 1}], [17, 105, 103, 104], [15, 106, 105], [7, 3, 106]]
        self.assertEqual('["a"]', self.solve(program))

    def test_script_lookup_supports_current_opcode(self):
        program = [[2, 100, "sdk\\.js$"], [11, 101, 100], [7, 3, 101]]
        self.assertEqual("sdk.js", self.solve(program, context={"script_sources": ["https://example.invalid/sdk.js"]}))

    def test_child_timezone_does_not_mutate_service_environment(self):
        old = os.environ.get("TZ")
        program = [[2, 100, "Date"], [6, 101, 10, 100], [2, 102, "Reflect"], [6, 103, 10, 102],
                   [2, 104, "construct"], [24, 105, 103, 104], [2, 106, [1784116800000]],
                   [17, 107, 105, 101, 106], [2, 108, "toString"], [24, 109, 107, 108],
                   [17, 110, 109], [7, 3, 110]]
        self.assertIn("GMT-0400", self.solve(program, context={"timezone": "America/New_York"}))
        self.assertEqual(old, os.environ.get("TZ"))

    def test_rejected_unknown_and_unresolved_programs_do_not_return_tokens(self):
        for program, code in [([[4, "rejected"]], "rejected"), ([[999]], "unsupported_opcode"), ([[2, 100, "unused"]], "no_result")]:
            with self.subTest(code=code), self.assertRaises(TurnstileChallengeError) as caught:
                self.solve(program)
            self.assertEqual(code, caught.exception.code)
        self.assertIsNone(solve_turnstile_token(encrypted([[999]]), "fixture-key"))

    def test_budget_failures_cannot_be_converted_into_success(self):
        program = [[22, 100, [[999]]], [3, "must not succeed"]]
        with self.assertRaises(TurnstileChallengeError) as caught:
            self.solve(program)
        self.assertEqual("unsupported_opcode", caught.exception.code)

    def test_loop_is_stopped_within_worker_budget(self):
        program = [[2, 100, "[[14,9,100]]"], [14, 9, 100]]
        started = time.monotonic()
        with self.assertRaises(TurnstileChallengeError):
            self.solve(program)
        self.assertLess(time.monotonic() - started, 2.5)

    def test_worker_does_not_inherit_service_credentials(self):
        response = Mock(returncode=0, stdout='{"token":"eA=="}')
        with patch.dict(os.environ, {"CHATGPT2API_AUTH_KEY": "fixture-secret-never-forward"}), \
             patch("utils.turnstile.subprocess.run", return_value=response) as worker:
            self.assertEqual("x", self.solve([[3, "x"]]))
        environment = worker.call_args.kwargs["env"]
        self.assertEqual({"TZ", "LANG", "PYTHONDONTWRITEBYTECODE"}, set(environment))
        self.assertEqual(1.5, worker.call_args.kwargs["timeout"])

    def test_home_navigation_omits_bearer_and_always_restores_it(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend.base_url = "https://chatgpt.com"
        backend.session = Mock()
        backend.session.headers = {"Authorization": "Bearer fixture", "Accept": "text/html"}
        backend._bootstrap_headers = lambda: {"Accept": "text/html"}
        def get(*args, **kwargs):
            self.assertNotIn("Authorization", backend.session.headers)
            return Mock(status_code=200, text="<html></html>")
        backend.session.get.side_effect = get
        backend._bootstrap()
        self.assertEqual("Bearer fixture", backend.session.headers["Authorization"])
        backend.session.get.side_effect = RuntimeError("fixture navigation failure")
        with self.assertRaises(RuntimeError):
            backend._bootstrap()
        self.assertEqual("Bearer fixture", backend.session.headers["Authorization"])

    def test_invalid_challenges_are_rejected_before_worker(self):
        for dx, key in [(None, "key"), ("x" * 1048577, "key"), ("not-base64", "key"), (encrypted([[3, "x"]]), "")]:
            with self.subTest(key_empty=not key), patch("utils.turnstile.subprocess.run") as worker:
                self.assertIsNone(solve_turnstile_token(dx, key))
                worker.assert_not_called()

    def test_required_challenge_failure_stops_before_finalize(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "fixture-access"
        backend.base_url = "https://chatgpt.com"
        backend.user_agent = "fixture-agent"
        backend.pow_script_sources = []
        backend.pow_data_build = ""
        backend._headers = lambda _, extra=None: dict(extra or {})
        backend.session = Mock()
        backend.session.post.return_value = Mock(status_code=200, json=lambda: {
            "prepare_token": "fixture-prepare", "turnstile": {"required": True, "dx": "invalid"}})
        with patch("services.openai_backend_api.solve_turnstile_token", side_effect=TurnstileChallengeError("unsupported_opcode")):
            with self.assertRaises(TurnstileChallengeError):
                backend._get_chat_requirements()
        self.assertEqual(1, backend.session.post.call_count)

    def test_required_empty_output_is_rejected(self):
        backend = object.__new__(OpenAIBackendAPI)
        with patch("services.openai_backend_api.solve_turnstile_token", return_value=None), \
             patch.object(backend, "_sentinel_context", return_value={}):
            with self.assertRaises(TurnstileChallengeError) as caught:
                backend._required_turnstile({"turnstile": {"required": True, "dx": "fixture"}}, "fixture-key")
        self.assertEqual("no_result", caught.exception.code)

    def test_bootstrap_resolves_relative_script_sources(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend.base_url = "https://chatgpt.com"
        backend.session = Mock()
        backend.session.headers = {}
        backend._bootstrap_headers = lambda: {}
        backend.session.get.return_value = Mock(status_code=200, text='<script src="/backend-api/sentinel/sdk.js"></script>')
        backend._bootstrap()
        self.assertEqual(["https://chatgpt.com/backend-api/sentinel/sdk.js"], backend.pow_script_sources)

    def requirements_backend(self, responses):
        backend = object.__new__(OpenAIBackendAPI)
        backend.base_url = "https://chatgpt.com"
        backend.access_token = "fixture-access"
        backend.user_agent = "fixture-agent"
        backend.pow_script_sources = []
        backend.pow_data_build = ""
        backend._headers = lambda _, extra=None: dict(extra or {})
        backend._sentinel_context = lambda: {}
        backend.session = Mock()
        backend.session.post.side_effect = [Mock(status_code=200, json=lambda value=value: value) for value in responses]
        return backend

    def test_invalid_prepare_response_stops_before_finalize(self):
        for data in [None, {"prepare_token": []}, {"prepare_token": "bad\r\n"},
                     {"prepare_token": "fixture", "proofofwork": "invalid"},
                     {"prepare_token": "fixture", "arkose": {"required": "true"}}]:
            with self.subTest(data_type=type(data).__name__):
                backend = self.requirements_backend([data])
                with self.assertRaises(TurnstileChallengeError):
                    backend._get_chat_requirements()
                self.assertEqual(1, backend.session.post.call_count)

    def test_invalid_final_tokens_cannot_reach_generation_headers(self):
        for data in [None, {"token": "bad\n"}, {"token": ["fixture"]},
                     {"token": "fixture", "so_token": "bad\r\n"},
                     {"token": "fixture", "so_token": {"secret": "fixture"}}]:
            with self.subTest(data_type=type(data).__name__):
                backend = self.requirements_backend([{"prepare_token": "fixture"}, data])
                with self.assertRaises(TurnstileChallengeError) as caught:
                    backend._get_chat_requirements()
                self.assertEqual("invalid_requirements_token", caught.exception.code)
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(2, backend.session.post.call_count)


class BrowserFingerprintTests(unittest.TestCase):
    def test_default_versions_are_coherent(self):
        fp = browser_fingerprint({"email": "fixture@example.invalid"}, "fixture-token")
        major = __import__('re').search(r"\d+", fp["impersonate"]).group(0)
        self.assertIn("Chrome/" + major + ".", fp["user-agent"])
        self.assertIn('v="' + major + '"', fp["sec-ch-ua"])
        self.assertNotIn("Edg/", fp["user-agent"])

    def test_devices_are_stable_and_scoped_to_account_and_site(self):
        a = browser_fingerprint({"email": "a@example.invalid"}, "fixture-a", "site-a")
        b = browser_fingerprint({"email": "a@example.invalid"}, "fixture-rotated", "site-a")
        c = browser_fingerprint({"email": "b@example.invalid"}, "fixture-b", "site-a")
        d = browser_fingerprint({"email": "a@example.invalid"}, "fixture-a", "site-b")
        self.assertEqual(a["oai-device-id"], b["oai-device-id"])
        self.assertNotEqual(a["oai-device-id"], c["oai-device-id"])
        self.assertNotEqual(a["oai-device-id"], d["oai-device-id"])

    def test_existing_valid_device_id_is_preserved(self):
        value = "e6c29ffd-5fe2-4bfb-bc06-4f2ecc1373be"
        fp = browser_fingerprint({"fp": {"oai-device-id": value}}, "fixture-token")
        self.assertEqual(value, fp["oai-device-id"])

    def test_malformed_profile_and_header_input_are_normalized(self):
        fp = browser_fingerprint({"fp": {"impersonate": "invalid", "user-agent": "bad\r\nheader"}}, "fixture-token")
        self.assertNotIn("\r", fp["user-agent"])
        self.assertNotEqual("invalid", fp["impersonate"])


if __name__ == "__main__":
    unittest.main()
