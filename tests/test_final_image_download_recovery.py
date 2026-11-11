from __future__ import annotations

import io
import os
import unittest
from collections import deque
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")

from curl_cffi import CurlError
from PIL import Image

from services.openai_backend_api import EditableFileArtifact, OpenAIBackendAPI


CONVERSATION_ID = "original-conversation"
MESSAGE_ID = "original-final-message"
SANDBOX_PATH = "/mnt/data/修正版.png"
RESOLVER_URL = f"https://chatgpt.com/backend-api/conversation/{CONVERSATION_ID}/interpreter/download"
FILE_URL = "https://files.oaiusercontent.com/final.png"


def png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), "purple").save(buffer, format="PNG")
    return buffer.getvalue()


def response(status=200, *, url=None, content=b"", headers=None):
    return Mock(status_code=status, headers=dict(headers or {}), content=content,
                text="synthetic upstream response",
                json=Mock(return_value={"download_url": url} if url else {}))


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FinalImageDownloadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.backends = []
        for mocker in (
            patch("services.openai_backend_api.time.monotonic", self.clock.monotonic),
            patch("services.openai_backend_api.time.sleep", self.clock.sleep),
            patch("services.openai_backend_api.random.uniform", return_value=0),
        ):
            mocker.start()
            self.addCleanup(mocker.stop)

    def tearDown(self):
        for backend in self.backends:
            backend.session.post.assert_not_called()
            backend.session.put.assert_not_called()

    def backend(self, outcomes):
        backend = object.__new__(OpenAIBackendAPI)
        backend.base_url = "https://chatgpt.com"
        backend.session = Mock()
        backend.session.headers = {
            "Authorization": "Bearer synthetic-private-token",
            "Cookie": "synthetic-private-cookie=value",
            "ChatGPT-Account-Id": "synthetic-private-account",
            "User-Agent": "test-browser",
        }
        backend._headers = lambda _path, extra=None: {**backend.session.headers, **(extra or {})}
        remaining = deque(outcomes)
        records = []

        def get(url, **kwargs):
            # 必须观察发送时合并后的请求头，否则会漏掉 Session 默认头中的令牌。
            effective_headers = {key.lower(): value for key, value in backend.session.headers.items()}
            for key, value in (kwargs.get("headers") or {}).items():
                if value is None:
                    effective_headers.pop(key.lower(), None)
                else:
                    effective_headers[key.lower()] = value
            records.append({"url": url, **kwargs, "effective_headers": effective_headers})
            if not remaining:
                raise AssertionError("unexpected extra GET")
            outcome = remaining.popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome(url, kwargs) if callable(outcome) and not isinstance(outcome, Mock) else outcome

        backend.session.get.side_effect = get
        self.backends.append(backend)
        return backend, records

    def download(self, backend, paths=None):
        return backend.download_final_sandbox_images(
            CONVERSATION_ID, MESSAGE_ID, paths if paths is not None else [SANDBOX_PATH])

    def assert_closed(self, *responses):
        for item in responses:
            item.close.assert_called_once_with()

    def test_resolver_retries_transient_http_status_for_original_artifact(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.clock.sleeps.clear()
                busy, resolved, image = response(status), response(url=FILE_URL), response(content=png_bytes())
                backend, calls = self.backend([busy, resolved, image])
                self.assertEqual([png_bytes()], self.download(backend))
                self.assertEqual([RESOLVER_URL, RESOLVER_URL, FILE_URL], [item["url"] for item in calls])
                self.assertEqual(calls[0]["params"], calls[1]["params"])
                self.assertEqual({"message_id": MESSAGE_ID, "sandbox_path": SANDBOX_PATH}, calls[0]["params"])
                self.assertEqual([1], self.clock.sleeps)
                self.assert_closed(busy, resolved, image)

    def test_file_retries_same_url_without_resolving_or_generating_again(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.clock.sleeps.clear()
                resolved, busy, image = response(url=FILE_URL), response(status), response(content=png_bytes())
                backend, calls = self.backend([resolved, busy, image])
                self.assertEqual([png_bytes()], self.download(backend))
                self.assertEqual([RESOLVER_URL, FILE_URL, FILE_URL], [item["url"] for item in calls])
                self.assertEqual([1], self.clock.sleeps)
                self.assert_closed(resolved, busy, image)

    def test_transient_curl_errors_retry_only_the_failed_get(self):
        for stage in ("resolver", "file"):
            for code in (7, 18, 28, 52, 56):
                with self.subTest(stage=stage, code=code):
                    self.clock.sleeps.clear()
                    resolved, image = response(url=FILE_URL), response(content=png_bytes())
                    failure = CurlError("synthetic transport failure", code=code)
                    outcomes = [failure, resolved, image] if stage == "resolver" else [resolved, failure, image]
                    backend, calls = self.backend(outcomes)
                    self.assertEqual([png_bytes()], self.download(backend))
                    urls = [RESOLVER_URL, RESOLVER_URL, FILE_URL] if stage == "resolver" else [RESOLVER_URL, FILE_URL, FILE_URL]
                    self.assertEqual(urls, [item["url"] for item in calls])
                    self.assertEqual([1], self.clock.sleeps)
                    self.assert_closed(resolved, image)

    def test_permanent_http_errors_fail_immediately_and_close_response(self):
        for stage in ("resolver", "file"):
            for status in (401, 403, 404):
                with self.subTest(stage=stage, status=status):
                    self.clock.sleeps.clear()
                    resolved, failed = response(url=FILE_URL), response(status)
                    backend, calls = self.backend([failed] if stage == "resolver" else [resolved, failed])
                    with self.assertRaises(Exception):
                        self.download(backend)
                    self.assertEqual(1 if stage == "resolver" else 2, len(calls))
                    self.assertEqual([], self.clock.sleeps)
                    self.assert_closed(failed)
                    if stage == "file":
                        self.assert_closed(resolved)

    def test_certificate_error_is_not_retried_and_private_headers_are_restored(self):
        for stage in ("resolver", "file"):
            with self.subTest(stage=stage):
                resolved = response(url=FILE_URL)
                failure = CurlError("synthetic certificate failure", code=60)
                backend, calls = self.backend([failure] if stage == "resolver" else [resolved, failure])
                original_headers = dict(backend.session.headers)
                with self.assertRaises(CurlError):
                    self.download(backend)
                self.assertEqual(1 if stage == "resolver" else 2, len(calls))
                self.assertEqual([], self.clock.sleeps)
                self.assertEqual(original_headers, backend.session.headers)

    def test_each_stage_has_at_most_three_attempts(self):
        for stage in ("resolver", "file"):
            with self.subTest(stage=stage):
                self.clock.sleeps.clear()
                resolved = response(url=FILE_URL)
                failures = [response(503) for _ in range(3)]
                backend, calls = self.backend(failures if stage == "resolver" else [resolved, *failures])
                with self.assertRaises(Exception):
                    self.download(backend)
                self.assertEqual(3 if stage == "resolver" else 4, len(calls))
                self.assertEqual([1, 2], self.clock.sleeps)
                self.assert_closed(*failures)

    def test_resolver_and_file_share_twenty_seconds_with_ten_second_attempt_cap(self):
        resolved, busy, image = response(url=FILE_URL), response(503), response(content=png_bytes())

        def resolve(_url, _kwargs):
            self.clock.now += 8
            return resolved

        def temporary_failure(_url, _kwargs):
            self.clock.now += 8
            return busy

        backend, calls = self.backend([resolve, temporary_failure, image])
        self.assertEqual([png_bytes()], self.download(backend))
        self.assertEqual([10, 10, 3], [item["timeout"] for item in calls])
        self.assertEqual([1], self.clock.sleeps)
        self.assert_closed(resolved, busy, image)

    def test_multiple_paths_share_one_batch_deadline(self):
        responses = [response(url=FILE_URL), response(content=png_bytes()),
                     response(url=FILE_URL), response(content=png_bytes())]

        def spend(seconds, result):
            def get(_url, _kwargs):
                self.clock.now += seconds
                return result
            return get

        backend, calls = self.backend([spend(8, responses[0]), spend(8, responses[1]),
                                       spend(2, responses[2]), responses[3]])
        self.assertEqual([png_bytes(), png_bytes()], self.download(backend, [SANDBOX_PATH, "/mnt/data/second.png"]))
        self.assertEqual([10, 10, 4, 2], [item["timeout"] for item in calls])
        self.assert_closed(*responses)

    def test_expired_budget_prevents_sleep_retry_and_next_file(self):
        resolved, failed = response(url=FILE_URL), response(503)

        def resolve(_url, _kwargs):
            self.clock.now += 10
            return resolved

        def exhaust(_url, _kwargs):
            self.clock.now += 10
            return failed

        backend, calls = self.backend([resolve, exhaust])
        with self.assertRaises(Exception):
            self.download(backend, [SANDBOX_PATH, "/mnt/data/second.png"])
        self.assertEqual(2, len(calls))
        self.assertEqual([], self.clock.sleeps)
        self.assert_closed(resolved, failed)

    def test_retry_after_short_delay_is_honored(self):
        resolved, busy, image = response(url=FILE_URL), response(429, headers={"Retry-After": "3"}), response(content=png_bytes())
        backend, _calls = self.backend([resolved, busy, image])
        self.assertEqual([png_bytes()], self.download(backend))
        self.assertEqual([3], self.clock.sleeps)
        self.assert_closed(resolved, busy, image)

    def test_long_retry_after_fails_without_waiting(self):
        for stage in ("resolver", "file"):
            with self.subTest(stage=stage):
                resolved, failed = response(url=FILE_URL), response(429, headers={"Retry-After": "6"})
                backend, calls = self.backend([failed] if stage == "resolver" else [resolved, failed])
                with self.assertRaises(Exception):
                    self.download(backend)
                self.assertEqual(1 if stage == "resolver" else 2, len(calls))
                self.assertEqual([], self.clock.sleeps)
                self.assert_closed(failed)

    def test_retry_after_cannot_exceed_remaining_batch_budget(self):
        resolved, failed = response(url=FILE_URL), response(429, headers={"Retry-After": "4"})

        def resolve(_url, _kwargs):
            self.clock.now += 9
            return resolved

        def limited(_url, _kwargs):
            self.clock.now += 8
            return failed

        backend, calls = self.backend([resolve, limited])
        with self.assertRaises(Exception):
            self.download(backend)
        self.assertEqual(2, len(calls))
        self.assertEqual([], self.clock.sleeps)
        self.assert_closed(resolved, failed)

    def test_private_headers_are_used_only_for_chatgpt_origin(self):
        for url in (FILE_URL, "https://testaccount.blob.core.windows.net/result/final.png",
                    "https://chatgpt.com/backend-api/estuary/content"):
            with self.subTest(url=url):
                backend, calls = self.backend([response(url=url), response(content=png_bytes())])
                original_headers = dict(backend.session.headers)
                self.assertEqual([png_bytes()], self.download(backend))
                self.assertEqual(original_headers["Authorization"], calls[0]["effective_headers"]["authorization"])
                sent = calls[1]["effective_headers"]
                if url.startswith("https://chatgpt.com/"):
                    self.assertEqual(original_headers["Authorization"], sent.get("authorization"))
                else:
                    for name in ("authorization", "cookie", "chatgpt-account-id"):
                        self.assertNotIn(name, sent)
                    self.assertIs(False, calls[1].get("allow_redirects"))
                self.assertEqual(original_headers, backend.session.headers)

    def test_file_redirect_is_rejected_and_closed_without_following(self):
        resolved = response(url=FILE_URL)
        redirect = response(302, headers={"Location": "https://untrusted.example/final.png"})
        backend, calls = self.backend([resolved, redirect])
        original_headers = dict(backend.session.headers)
        with self.assertRaises(Exception):
            self.download(backend)
        self.assertEqual(2, len(calls))
        self.assertIs(False, calls[1].get("allow_redirects"))
        self.assertEqual(original_headers, backend.session.headers)
        self.assert_closed(resolved, redirect)

    def test_untrusted_file_urls_are_rejected_before_file_get(self):
        for url in ("https://untrusted.example/final.png", "https://files.oaiusercontent.com.evil.test/final.png",
                    "https://chatgpt.com.evil.test/final.png", "http://files.oaiusercontent.com/final.png",
                    "https://user:pass@files.oaiusercontent.com/final.png"):
            with self.subTest(url=url):
                resolved = response(url=url)
                backend, calls = self.backend([resolved])
                with self.assertRaises(Exception):
                    self.download(backend)
                self.assertEqual(1, len(calls))
                self.assert_closed(resolved)

    def test_empty_html_and_corrupt_png_are_not_retried_or_delivered(self):
        for content in (b"", b"<html>not a PNG</html>", png_bytes()[:25]):
            with self.subTest(size=len(content)):
                resolved, invalid = response(url=FILE_URL), response(content=content)
                backend, calls = self.backend([resolved, invalid])
                with self.assertRaises(Exception):
                    self.download(backend)
                self.assertEqual(2, len(calls))
                self.assertEqual([], self.clock.sleeps)
                self.assert_closed(resolved, invalid)

    def test_export_resolver_keeps_its_existing_default_timeout_and_no_retry(self):
        failed = response(503)
        backend, calls = self.backend([failed])
        artifact = EditableFileArtifact(sandbox_path=SANDBOX_PATH, message_id=MESSAGE_ID)
        self.assertEqual("", backend._resolve_editable_download_url(CONVERSATION_ID, artifact))
        self.assertEqual(1, len(calls))
        self.assertEqual(60, calls[0]["timeout"])
        self.assertEqual([], self.clock.sleeps)


if __name__ == "__main__":
    unittest.main()
