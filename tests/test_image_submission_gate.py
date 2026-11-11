import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from services.webimage_conversation_transport import WebImageConversationTransport
from services.image_quota import ImageQuotaExceeded

class ImageSubmissionGateTests(unittest.TestCase):
    def transport(self, guard):
        transport=object.__new__(WebImageConversationTransport)
        transport.backend=SimpleNamespace(_image_submission_guard=guard)
        transport.message_id='message'
        transport.headers=Mock(return_value={})
        return transport

    def test_gate_is_called_before_generation_transport(self):
        seen=[]
        transport=self.transport(lambda:seen.append('gate'))
        with patch('services.image_generation_stream.stream_image_generation',side_effect=lambda *a:(seen.append('send') or iter(()))):
            list(transport.generate({},None,'',5))
        self.assertEqual(['gate','send'],seen)

    def test_blocked_gate_never_sends_or_marks_submitted(self):
        transport=self.transport(Mock(side_effect=RuntimeError('blocked')))
        with patch('services.image_generation_stream.stream_image_generation') as send:
            with self.assertRaisesRegex(RuntimeError,'blocked'):
                list(transport.generate({},None,'',5))
            send.assert_not_called()
        self.assertFalse(getattr(transport.backend,'_webimage_generation_sent',False))

    def test_unknown_restore_time_has_valid_public_error(self):
        from services.protocol.conversation import image_quota_error
        error=ImageQuotaExceeded({'reason':'图片额度已达上限','restore_at':None,'next_check_at':200})
        public=image_quota_error(error)
        self.assertEqual(429,public.status_code)
        self.assertFalse(public.restore_at)

if __name__=='__main__':unittest.main()
