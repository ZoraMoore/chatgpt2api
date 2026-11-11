from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-final-delivery")

from PIL import Image
from services.config import config
from services.openai_backend_api import OpenAIBackendAPI, ChatRequirements
from services.protocol.conversation import ConversationRequest, ImageGenerationError, stream_image_outputs
from services.protocol.conversation import ImageOutput
from services.image_task_service import ImageTaskService


CID = "11111111-1111-4111-8111-111111111111"


def png():
    out = io.BytesIO()
    Image.new("RGB", (8, 6), "blue").save(out, format="PNG")
    return out.getvalue()


def message(mid, role="assistant", *, content=None, recipient="all", name=None, channel=None, end_turn=False, metadata=None):
    return {"id": mid, "author": {"role": role, "name": name}, "recipient": recipient,
            "status": "finished_successfully", "channel": channel, "end_turn": end_turn,
            "content": content or {"content_type": "text", "parts": [""]}, "metadata": metadata or {}}


def call(mid, tool="python"):
    return message(mid, recipient=tool, channel="analysis", content={"content_type": "code", "text": "synthetic_call()"})


def output(mid, text, parent):
    return message(mid, "tool", name="python", content={"content_type": "execution_output", "text": text},
                   metadata={"parent_id": parent, "is_complete": True, "aggregate_result": {"status": "success", "in_kernel_exception": None,
                             "system_exception": None, "timeout_triggered": False, "code": "synthetic_call()",
                             "messages": [{"message_type": "stream", "stream_name": "stdout", "text": text}], "jupyter_messages": []}})


def image_message(mid="image-result"):
    return message(mid, "tool", name="synthetic.image", content={"content_type": "multimodal_text", "parts": [
        {"content_type": "image_asset_pointer", "asset_pointer": "sediment://file-final", "width": 8, "height": 6}]},
                   metadata={"image_gen_title": "Synthetic image"})


def history(post=False):
    rows = [message("submitted", "user", content={"content_type": "text", "parts": ["synthetic request"]})]
    if not post:
        rows += [call("inspect-call"), output("inspect-result", "(64, 64)", "inspect-call")]
    rows += [call("image-call", "synthetic.image"), image_message()]
    if post:
        rows += [call("edit-call"), output("edit-result", "/mnt/data/final.png\n", "edit-call")]
    rows += [message("final", channel="final", end_turn=True)]
    return rows


class TerminalImageIntegrationTests(unittest.TestCase):
    def backend(self, rows):
        backend = Mock()
        backend._image_submitted_message_id = "submitted"
        backend.stream_conversation.return_value = iter([
            *(json.dumps({"conversation_id": CID, "message": m}) for m in rows), "[DONE]"])
        backend._get_conversation.return_value = {"conversation_id": CID, "messages": rows}
        backend._resolve_image_urls.return_value = ["https://example.test/final.png"]
        backend.download_image_bytes.return_value = [png()]
        backend.download_final_sandbox_images.return_value = [png()]
        return backend

    def run_result(self, backend, *, mode="conversation"):
        return list(stream_image_outputs(backend, ConversationRequest(
            model="gpt-image-2", prompt="synthetic", images=["synthetic input"], image_transport=mode)))

    def test_preinspection_recovers_exact_last_generated_image(self):
        backend = self.backend(history())
        result = self.run_result(backend)[-1]
        self.assertEqual("result", result.kind)
        self.assertEqual(png(), base64.b64decode(result.data[0]["b64_json"]))
        self.assertEqual("image_tool_terminal", result.execution["result_source"])
        backend._resolve_image_urls.assert_called_once_with(CID, [], ["file-final"])
        backend.resolve_conversation_image_urls.assert_not_called()
        backend.download_final_sandbox_images.assert_not_called()
        backend._get_conversation.assert_not_called()

    def test_terminal_python_file_uses_its_own_message_id(self):
        backend = self.backend(history(post=True))
        result = self.run_result(backend)[-1]
        self.assertEqual("result", result.kind)
        self.assertEqual("python_terminal", result.execution["result_source"])
        backend.download_final_sandbox_images.assert_called_once_with(CID, "edit-result", ["/mnt/data/final.png"])
        backend._resolve_image_urls.assert_not_called()
        backend.resolve_conversation_image_urls.assert_not_called()

    def test_existing_explicit_final_link_stays_authoritative(self):
        rows = history(post=True)
        rows[-1]["content"]["parts"] = ["[PNG](sandbox:/mnt/data/explicit.png)"]
        backend = self.backend(rows)
        self.run_result(backend)
        backend.download_final_sandbox_images.assert_called_once_with(CID, "final", ["/mnt/data/explicit.png"])
        backend._resolve_image_urls.assert_not_called()

    def test_required_postprocess_cannot_accept_native_image(self):
        backend = self.backend(history())
        with patch.object(config, "data", {**config.data, "image_require_reference_postprocess": True}):
            with self.assertRaises(ImageGenerationError):
                self.run_result(backend)
        backend._resolve_image_urls.assert_not_called()
        backend.download_final_sandbox_images.assert_not_called()

    def test_invalid_download_never_falls_back_to_another_image(self):
        backend = self.backend(history())
        backend.download_image_bytes.return_value = [b"<html>not an image</html>"]
        with self.assertRaises(ImageGenerationError):
            self.run_result(backend)
        backend._resolve_image_urls.assert_called_once()
        backend.resolve_conversation_image_urls.assert_not_called()

    def test_voice_transport_keeps_existing_delivery_rules(self):
        backend = self.backend(history())
        with self.assertRaises(ImageGenerationError):
            self.run_result(backend, mode="voice")
        backend._resolve_image_urls.assert_not_called()

    def test_readback_missing_known_tool_work_cannot_recover_old_image(self):
        rows = history(post=True)
        backend = self.backend(rows)
        # 直播确实截断在最终回复前，才能验证读回缺少已观察工具时拒绝交付。
        backend.stream_conversation.return_value = iter([
            *(json.dumps({"conversation_id": CID, "message": row}) for row in rows[:-1]), "[DONE]"])
        backend._get_conversation.return_value["messages"] = [rows[0], rows[1], rows[2], rows[-1]]
        with self.assertRaises(ImageGenerationError):
            self.run_result(backend)
        backend._resolve_image_urls.assert_not_called()
        backend.download_final_sandbox_images.assert_not_called()
        self.assertEqual(4, backend._get_conversation.call_count)
        self.assertTrue(all(call.args == (CID,) and 0 < call.kwargs["timeout_secs"] <= 3.0
                            for call in backend._get_conversation.call_args_list))
        # 补查只重读原会话，不重新发起生成。
        backend.stream_conversation.assert_called_once()

    def test_readback_missing_late_unanswered_tool_call_cannot_recover_old_image(self):
        rows = history()
        late = call("late-call")
        late["status"] = "in_progress"
        # 最终回复从未到达，且最后一次工具调用仍未完成。
        backend = self.backend([*rows[:-1], late])
        backend._get_conversation.return_value["messages"] = rows
        with self.assertRaises(ImageGenerationError):
            self.run_result(backend)
        backend._resolve_image_urls.assert_not_called()
        backend.download_final_sandbox_images.assert_not_called()

    def test_submit_saves_actual_user_message_id_without_extra_request(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend._image_generation_payload = Mock(return_value={"messages": [{"id": "submitted"}]})
        backend._post_image_conversation = Mock()
        backend._start_image_generation("test", ChatRequirements(token="test"), "test", "gpt-image-2")
        self.assertEqual("submitted", getattr(backend, "_image_submitted_message_id", None))
        backend._post_image_conversation.assert_called_once()

    def test_resume_keeps_stored_mode_and_unknown_history_cannot_use_new_recovery(self):
        for original, current in [("conversation", "voice"), ("voice", "conversation"), (None, "conversation")]:
            with self.subTest(original=original), tempfile.TemporaryDirectory() as directory:
                service = ImageTaskService(Path(directory) / "tasks.json")
                service._log_call = Mock()
                task = {"id": "task", "owner_id": "owner", "status": "error", "conversation_id": CID,
                        "resume_account_ref": "a" * 64, "mode": "edit", "model": "gpt-image-2"}
                if original is not None:
                    task["image_transport"] = original
                service._tasks["owner:task"] = task
                backend, accounts = Mock(), Mock()
                accounts.acquire_image_resume_token.return_value = "token"
                accounts.refresh_access_token.return_value = "token"
                with patch.object(config, "data", {**config.data, "image_transport": current}), \
                     patch("services.image_task_service.account_service", accounts), \
                     patch("services.openai_backend_api.OpenAIBackendAPI", return_value=backend), \
                     patch("services.protocol.conversation.stream_image_outputs", return_value=iter([
                         ImageOutput(kind="result", model="gpt-image-2", index=1, total=1,
                                     data=[{"url": "https://example.test/final.png"}])])) as stream:
                    service._run_resume_poll("owner:task", CID, 10, {"id": "owner"}, "edit", "gpt-image-2")
                self.assertEqual(original or current, stream.call_args.args[1].image_transport)
                self.assertIs(original == "conversation", stream.call_args.kwargs.get("allow_terminal_recovery"))
                backend.stream_conversation.assert_not_called()

    def test_disabled_recovery_stays_disabled_after_resume_recheck(self):
        rows = history(post=True)
        backend = self.backend(rows)
        before = {"conversation_id": CID, "messages": [rows[0], call("image-call", "synthetic.image"), image_message()]}
        backend._get_conversation.side_effect = [before, {"conversation_id": CID, "messages": rows}]
        backend.resolve_conversation_image_urls.return_value = ["https://example.test/native.png"]
        with self.assertRaises(ImageGenerationError):
            list(stream_image_outputs(backend, ConversationRequest(model="gpt-image-2", images=["reference"], image_transport="conversation"),
                                      resume_conversation_id=CID, poll_timeout_secs=10, allow_terminal_recovery=False))
        backend.download_final_sandbox_images.assert_not_called()
