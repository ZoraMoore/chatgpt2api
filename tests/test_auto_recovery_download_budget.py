"""后台恢复文件阶段共用截止时间，不为解析和下载重复发放预算。"""
import unittest
from unittest.mock import Mock, patch

from curl_cffi.requests.exceptions import Timeout
from services.openai_backend_api import OpenAIBackendAPI


class AutoRecoveryDownloadBudgetTests(unittest.TestCase):
    def setUp(self):
        self.backend = object.__new__(OpenAIBackendAPI)
        self.backend.base_url = 'https://chatgpt.com'
        self.backend._headers = Mock(return_value={})
        response = Mock(status_code=200)
        response.json.return_value = {'download_url': 'https://files.oaiusercontent.com/final.png'}
        self.backend.session = Mock()
        self.backend.session.get.return_value = response

    def test_ordinary_requests_keep_original_timeouts(self):
        helper = getattr(self.backend, '_image_download_timeout', None)
        self.assertTrue(callable(helper))
        self.assertEqual(60, helper(60))

    def test_file_and_attachment_resolve_respect_remaining_shared_budget(self):
        self.backend._image_download_deadline = 105.0
        with patch('services.openai_backend_api.time.monotonic', return_value=100.0):
            self.backend._get_file_download_url('file-output')
            self.backend._get_attachment_download_url('conversation', 'image-output')
        for call in self.backend.session.get.call_args_list:
            self.assertLessEqual(call.kwargs['timeout'], 5.0)

    def test_expired_deadline_does_not_start_resolve_request(self):
        self.backend._image_download_deadline = 99.0
        with patch('services.openai_backend_api.time.monotonic', return_value=100.0):
            with self.assertRaises(Timeout):
                self.backend._get_file_download_url('file-output')
        self.backend.session.get.assert_not_called()

    def test_each_native_download_uses_remaining_budget_without_reset(self):
        self.backend._image_download_deadline = 130.0
        now = [110.0]
        def transfer(*args, **kwargs):
            remaining = kwargs.get('timeout_secs', 120)
            self.assertLessEqual(remaining, 130 - now[0])
            now[0] += 4
            return Mock(content=b'image-' + str(now[0]).encode())
        self.backend._request_image_transfer = Mock(side_effect=transfer)
        with patch('services.openai_backend_api.time.monotonic', side_effect=lambda: now[0]):
            images = self.backend.download_image_bytes(['https://files.oaiusercontent.com/a.png',
                                                      'https://files.oaiusercontent.com/b.png'])
        self.assertEqual(2, len(images))
        self.assertEqual([20.0, 16.0], [c.kwargs['timeout_secs'] for c in self.backend._request_image_transfer.call_args_list])

    def test_sandbox_resolution_uses_same_shorter_deadline(self):
        self.backend._image_download_deadline = 105.0
        def resolve(*args, **kwargs):
            self.assertLessEqual(kwargs['image_deadline'], 105.0)
            raise Timeout('synthetic expiry after resolution')
        self.backend._resolve_editable_download_url = Mock(side_effect=resolve)
        with patch('services.openai_backend_api.time.monotonic', return_value=100.0):
            with self.assertRaises(Timeout):
                self.backend.download_final_sandbox_images('conversation', 'message', ['/mnt/data/final.png'])


if __name__ == '__main__':
    unittest.main()
