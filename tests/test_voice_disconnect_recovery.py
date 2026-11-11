from __future__ import annotations

import copy
import json
import os
from io import BytesIO
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")

from services.voice_image_transport import VoiceImageError, VoiceImageSession, stream_voice_image
from services.voice_protocol import pack_event


CID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
USER = {"id": "submitted-user", "author": {"role": "user"},
        "status": "finished_successfully", "content": {"content_type": "text", "parts": ["画猫"]}}


def document():
    return {"conversation_id": CID, "current_node": "final", "messages": [copy.deepcopy(USER),
        {"id": "image", "author": {"role": "tool", "name": "image_gen"},
         "status": "finished_successfully", "content": {"content_type": "multimodal_text", "parts": [
             {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file-output"}]}},
        {"id": "final", "author": {"role": "assistant"}, "channel": "final", "recipient": "all",
         "status": "finished_successfully", "end_turn": True,
         "content": {"content_type": "text", "parts": ["[图片](sandbox:/mnt/data/final.png)"]}}]}


class VoiceDisconnectRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.backend = SimpleNamespace(account={}, image_conversation_id="", _report_progress=Mock(),
            _upload_image=Mock(), _image_model_slug=Mock(return_value="auto"),
            _image_generation_payload=Mock(return_value={"messages": [copy.deepcopy(USER)]}),
            _get_conversation=Mock(return_value=document()), session=Mock())
        self.session = VoiceImageSession(self.backend, USER, "auto", 30)
        self.session.sent = True
        self.session._received(pack_event("conversation_update", {"conversation_id": CID}))
        self.session._received(pack_event("relay_message_processed", {"message_id": USER["id"]}))
        self.session.stream = Mock(side_effect=VoiceImageError("connection_failed", CID))
        self.session.close = Mock()
        self.profile = patch("services.proxy_service.proxy_settings.get_profile",
                             return_value=SimpleNamespace(proxy_url=""))
        self.profile.start()
        self.addCleanup(self.profile.stop)

    def run_stream(self):
        with patch("services.voice_image_transport.VoiceImageSession", return_value=self.session):
            return list(stream_voice_image(self.backend, "画猫", "gpt-image-2.5", [], 30))

    def test_matching_receipt_recovers_original_conversation_without_resubmission(self):
        events = self.run_stream()
        self.assertEqual("[DONE]", events[-1])
        parsed = [json.loads(item) for item in events[:-1]]
        self.assertEqual("voice_turn_complete", parsed[-1]["type"])
        self.assertEqual(USER["id"], self.backend._image_submitted_message_id)
        self.assertEqual(["submitted-user", "image", "final"],
                         [item["message"]["id"] for item in parsed if "message" in item])
        self.assertTrue(self.session.diagnostic_snapshot()["recovery_complete"])
        self.session.stream.assert_called_once()
        self.session.close.assert_called_once()
        self.backend.session.post.assert_not_called()
        self.backend._get_conversation.assert_called_once()
        self.assertEqual(CID, self.backend._get_conversation.call_args.args[0])

    def test_local_send_and_wrong_receipt_cannot_authorize_recovery(self):
        for receipt in (None, "other-user"):
            with self.subTest(receipt=receipt):
                self.session = VoiceImageSession(self.backend, USER, "auto", 30)
                self.session.sent = True
                self.session._received(pack_event("conversation_update", {"conversation_id": CID}))
                if receipt:
                    self.session._received(pack_event("relay_message_processed", {"message_id": receipt}))
                self.session.stream = Mock(side_effect=VoiceImageError("connection_failed", CID))
                with self.assertRaises(VoiceImageError):
                    self.run_stream()
                self.backend._get_conversation.assert_not_called()

    def test_receipt_before_submission_is_ignored(self):
        self.session = VoiceImageSession(self.backend, USER, "auto", 30)
        self.session._received(pack_event("relay_message_processed", {"message_id": USER["id"]}))
        self.session.sent = True
        self.session._received(pack_event("conversation_update", {"conversation_id": CID}))
        self.session.stream = Mock(side_effect=VoiceImageError("connection_failed", CID))
        with self.assertRaises(VoiceImageError):
            self.run_stream()
        self.backend._get_conversation.assert_not_called()

    def test_non_connection_errors_keep_original_failure(self):
        for code in ("handshake_rejected", "upstream_error", "invalid_event", "deadline_exceeded"):
            with self.subTest(code=code):
                self.session.stream.side_effect = VoiceImageError(code, CID)
                with self.assertRaises(VoiceImageError) as raised:
                    self.run_stream()
                self.assertEqual(code, raised.exception.code)
                self.backend._get_conversation.assert_not_called()

    def test_incomplete_turn_waits_for_final_on_same_conversation(self):
        pending = document()
        pending["messages"] = pending["messages"][:-1]
        pending["current_node"] = "image"
        self.backend._get_conversation.side_effect = [pending, document()]
        with patch("services.voice_image_transport.time.sleep"):
            self.assertEqual("[DONE]", self.run_stream()[-1])
        self.assertEqual([CID, CID], [c.args[0] for c in self.backend._get_conversation.call_args_list])

    def test_foreign_conversation_or_user_branch_is_never_delivered(self):
        for field in ("conversation", "submitted", "new_user", "partial", "active_tool"):
            with self.subTest(field=field):
                doc = document()
                if field == "conversation":
                    doc["conversation_id"] = OTHER
                elif field == "submitted":
                    doc["messages"][0]["id"] = "other-user"
                elif field == "new_user":
                    doc["messages"].insert(1, dict(USER, id="later-user"))
                elif field == "partial":
                    doc["page_info"] = {"has_previous_page": True}
                else:
                    doc["messages"][1]["status"] = "in_progress"
                self.backend._get_conversation.side_effect = [doc, document()]
                with patch("services.voice_image_transport.time.sleep"):
                    events = self.run_stream()
                self.assertEqual(2, self.backend._get_conversation.call_count)
                self.assertEqual("[DONE]", events[-1])
                self.backend._get_conversation.reset_mock()

    def test_transport_timeout_retries_only_read_only_lookup(self):
        self.backend._get_conversation.side_effect = [TimeoutError(), document()]
        with patch("services.voice_image_transport.time.sleep"):
            self.assertEqual("[DONE]", self.run_stream()[-1])
        self.assertEqual(2, self.backend._get_conversation.call_count)
        self.backend.session.post.assert_not_called()

    def test_expired_original_deadline_does_not_start_another_lookup(self):
        self.session.deadline = time.monotonic() - 1
        with self.assertRaises(VoiceImageError) as raised:
            self.run_stream()
        self.assertEqual("deadline_exceeded", raised.exception.code)
        self.backend._get_conversation.assert_not_called()

    def test_healthy_voice_stream_keeps_existing_path(self):
        self.session.stream.side_effect = None
        self.session.stream.return_value = iter(["[DONE]"])
        self.assertEqual(["[DONE]"], self.run_stream())
        self.backend._get_conversation.assert_not_called()

    def test_recovery_preserves_quota_and_auth_failures(self):
        from utils.helper import UpstreamHTTPError
        self.backend._get_conversation.side_effect = UpstreamHTTPError("lookup", 403, "redacted")
        with self.assertRaises(VoiceImageError):
            self.run_stream()
        self.backend._get_conversation.assert_called_once()
        self.backend.session.post.assert_not_called()

    def test_recovered_events_deliver_valid_final_png_through_existing_pipeline(self):
        from PIL import Image
        from services.protocol.conversation import ConversationRequest, stream_image_outputs
        data = BytesIO()
        Image.new("RGB", (8, 8), "white").save(data, format="PNG")
        self.backend.stream_conversation = Mock(side_effect=lambda **kwargs: iter(self.run_stream()))
        self.backend.download_final_sandbox_images = Mock(return_value=[data.getvalue()])
        self.backend._get_conversation.side_effect = lambda *args, **kwargs: document()
        request = ConversationRequest(model="gpt-image-2.5", prompt="画猫", image_transport="voice",
                                      response_format="b64_json")
        with patch("services.protocol.conversation._remove_image_conversation_later"), \
                patch("services.protocol.conversation.account_service.bind_image_probe_conversation"):
            outputs = list(stream_image_outputs(self.backend, request))
        results = [o for o in outputs if o.kind == "result"]
        self.assertEqual(1, len(results))
        self.assertEqual(CID, results[0].conversation_id)
        self.assertTrue(results[0].data[0]["b64_json"])
        self.backend.download_final_sandbox_images.assert_called_once_with(CID, "final", ["/mnt/data/final.png"])
        self.backend.session.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
