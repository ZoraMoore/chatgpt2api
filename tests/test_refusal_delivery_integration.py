"""上游已明确拒绝的终态不能继续补查或下载草图。"""
import unittest
from unittest.mock import patch
from tests import test_image_final_contract as fixtures
from services.protocol.conversation import ImageGenerationError

POLICY_TEXT = '非常抱歉，生成的图片可能违反了关于与第三方内容相似性的防护限制。如果你认为此判断有误，请重试或修改提示语。'
QUALITY_TEXT = '当前素材无法在可靠保护圣诞树枝叶、树形轮廓及黑色花盆原始像素的前提下完成无痕清理，因此不输出不合格成品。'

class RefusalDeliveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ImageFinalContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_terminal_policy_without_tool_result_is_400_and_never_readbacks(self):
        rows = [fixtures.user_message(), fixtures.image_call(1), fixtures.final_message(POLICY_TEXT)]
        rows[-1]['channel'] = 'commentary'
        backend = self.fixture.backend(rows)
        with self.assertRaises(ImageGenerationError) as caught:
            self.fixture.run_result(backend)
        error = caught.exception
        self.assertEqual((400, 'invalid_request_error', 'content_policy_violation'),
                         (error.status_code, error.error_type, error.code))
        self.assertEqual('content_policy_rejected', error.image_execution[0]['selection_reason'])
        backend._get_conversation.assert_not_called()
        backend._request_image_transfer.assert_not_called()
        self.assertEqual(1, backend.stream_conversation.call_count)

    def test_final_quality_refusal_never_returns_existing_generated_draft(self):
        rows = fixtures.revisions(1)
        rows[-1] = fixtures.final_message(QUALITY_TEXT)
        backend = self.fixture.backend(rows)
        with self.assertRaises(ImageGenerationError) as caught:
            self.fixture.run_result(backend)
        self.assertEqual('image_quality_rejected', caught.exception.code)
        self.assertEqual('quality_rejected', caught.exception.image_execution[0]['selection_reason'])
        backend._get_conversation.assert_not_called()
        backend._request_image_transfer.assert_not_called()
        self.assertEqual(1, backend.stream_conversation.call_count)

    def test_policy_discovered_in_readback_stops_at_first_confirmed_refusal(self):
        rows = [fixtures.user_message(), fixtures.image_call(1), fixtures.final_message(POLICY_TEXT)]
        backend = self.fixture.backend(rows)
        import json
        backend.stream_conversation.return_value = iter([
            *(json.dumps({'conversation_id': fixtures.CID, 'message': m}) for m in rows[:-1]), '[DONE]'])
        with patch('services.protocol.conversation.time.sleep'):
            with self.assertRaises(ImageGenerationError) as caught:
                self.fixture.run_result(backend)
        self.assertEqual('content_policy_violation', caught.exception.code)
        self.assertEqual(1, backend._get_conversation.call_count)
        backend._request_image_transfer.assert_not_called()
        self.assertEqual(1, backend.stream_conversation.call_count)

if __name__ == '__main__':
    unittest.main()
