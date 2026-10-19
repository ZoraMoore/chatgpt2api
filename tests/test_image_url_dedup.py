from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.openai_backend_api import OpenAIBackendAPI


class ImageDownloadURLDedupTests(unittest.TestCase):
    def setUp(self):
        self.backend = object.__new__(OpenAIBackendAPI)
        self.backend._closed = True
        self.backend._get_file_download_url = Mock(return_value="https://storage.example.test/file")
        self.backend._get_attachment_download_url = Mock(return_value="https://storage.example.test/attachment")

    def test_same_file_uses_one_successful_resolution(self):
        result = self.backend._resolve_image_urls("conversation", ["file_result"], ["file_result"])
        self.assertEqual(["https://storage.example.test/file"], result)
        self.backend._get_file_download_url.assert_called_once_with("file_result")
        self.backend._get_attachment_download_url.assert_not_called()

    def test_failed_file_lookup_keeps_attachment_fallback(self):
        self.backend._get_file_download_url.side_effect = RuntimeError("file lookup unavailable")
        result = self.backend._resolve_image_urls("conversation", ["file_result"], ["file_result"])
        self.assertEqual(["https://storage.example.test/attachment"], result)
        self.backend._get_attachment_download_url.assert_called_once_with("conversation", "file_result")

    def test_empty_file_url_keeps_attachment_fallback(self):
        self.backend._get_file_download_url.return_value = ""
        result = self.backend._resolve_image_urls("conversation", ["file_result"], ["file_result"])
        self.assertEqual(["https://storage.example.test/attachment"], result)
        self.backend._get_attachment_download_url.assert_called_once_with("conversation", "file_result")

    def test_different_ids_are_resolved_independently_in_order(self):
        result = self.backend._resolve_image_urls("conversation", ["file_first"], ["file_first", "file_second"])
        self.assertEqual(["https://storage.example.test/file", "https://storage.example.test/attachment"], result)
        self.backend._get_file_download_url.assert_called_once_with("file_first")
        self.backend._get_attachment_download_url.assert_called_once_with("conversation", "file_second")

    def test_attachment_only_result_is_preserved(self):
        result = self.backend._resolve_image_urls("conversation", [], ["file_result"])
        self.assertEqual(["https://storage.example.test/attachment"], result)
        self.backend._get_file_download_url.assert_not_called()
        self.backend._get_attachment_download_url.assert_called_once_with("conversation", "file_result")

    def test_repeated_successful_ids_do_not_repeat_requests(self):
        result = self.backend._resolve_image_urls(
            "conversation", ["file_first", "file_first"], ["file_first", "file_second", "file_second"],
        )
        self.assertEqual(["https://storage.example.test/file", "https://storage.example.test/attachment"], result)
        self.backend._get_file_download_url.assert_called_once_with("file_first")
        self.backend._get_attachment_download_url.assert_called_once_with("conversation", "file_second")

    def test_shared_url_from_different_ids_is_still_deduplicated(self):
        self.backend._get_attachment_download_url.return_value = "https://storage.example.test/file"
        result = self.backend._resolve_image_urls("conversation", ["file_first"], ["file_second"])
        self.assertEqual(["https://storage.example.test/file"], result)
        self.backend._get_attachment_download_url.assert_called_once_with("conversation", "file_second")

    def test_failed_attachment_does_not_prevent_other_results(self):
        self.backend._get_attachment_download_url.side_effect = [RuntimeError("attachment unavailable"), "https://storage.example.test/second"]
        result = self.backend._resolve_image_urls("conversation", [], ["file_first", "file_second"])
        self.assertEqual(["https://storage.example.test/second"], result)
        self.assertEqual([call("conversation", "file_first"), call("conversation", "file_second")],
                         self.backend._get_attachment_download_url.call_args_list)

    def test_without_conversation_only_file_results_are_returned(self):
        result = self.backend._resolve_image_urls("", ["file_first"], ["file_second"])
        self.assertEqual(["https://storage.example.test/file"], result)
        self.backend._get_attachment_download_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
