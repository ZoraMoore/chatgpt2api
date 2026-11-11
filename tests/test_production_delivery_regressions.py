"""生产交付故障的去敏回归：终态 4 与已完成的图片工具技术失败。"""
from __future__ import annotations

import base64
import json
import os
import time
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-delivery-fix")

from curl_cffi import CurlOpt, requests
from PIL import Image

from services import image_generation_stream as image_stream
from services.protocol import conversation as conversation
from services.protocol.final_image import FinalImageTracker
from services.protocol.terminal_image_delivery import select_terminal_image_delivery
from tests.test_image_generation_stream import LocalStreamServer
from tests.test_terminal_image_delivery import python_pair


CONVERSATION_ID = "production-regression-conversation"
SUBMITTED_ID = "production-regression-user"
FAILURE_TEXT = "We experienced an error when generating images."


def message(message_id, role, content, **fields):
    return {"id": message_id, "author": {"role": role}, "content": content,
            "status": "finished_successfully", "recipient": "all", "metadata": {}, **fields}


def user_message(message_id=SUBMITTED_ID):
    return message(message_id, "user", {"content_type": "text", "parts": ["synthetic request"]})


def image_message(message_id="generated-image"):
    return message(message_id, "tool", {"content_type": "multimodal_text", "parts": [
        {"content_type": "image_asset_pointer", "asset_pointer": "sediment://synthetic-final"}]},
        author={"role": "tool", "name": "image_gen"}, metadata={"image_gen_title": "Synthetic image"})


def failure_message(message_id="image-failure", *, text=FAILURE_TEXT):
    return message(message_id, "tool", {"content_type": "text", "parts": [text]},
                   author={"role": "tool", "name": "image_gen"})


def final_message(*, text="", finished=True):
    return message("final", "assistant", {"content_type": "text", "parts": [text]},
                   channel="final", status="finished_successfully" if finished else "in_progress",
                   end_turn=finished)


def event(item):
    return {"conversation_id": CONVERSATION_ID, "message": item}


def document(*, failed=False):
    return {"conversation_id": CONVERSATION_ID, "async_status": 4, "current_node": "final",
            "messages": [user_message(), failure_message() if failed else image_message(),
                         final_message(text="The image service returned a technical error." if failed else "")]}


def track(messages):
    tracker = FinalImageTracker()
    for item in messages:
        tracker.update(event(item))
    return tracker


def select(doc):
    return select_terminal_image_delivery(doc, conversation_id=CONVERSATION_ID,
                                          submitted_message_id=SUBMITTED_ID)


def recover(doc, **kwargs):
    return image_stream._completed_messages(doc, CONVERSATION_ID, SUBMITTED_ID, **kwargs)


class TerminalAsyncStatusRegressions(unittest.TestCase):
    def test_completed_status_four_can_deliver_and_recover_native_image(self):
        doc = document()
        selected = select(doc)
        self.assertIsNotNone(selected)
        self.assertEqual(("native_image", "generated-image"), (selected.kind, selected.message_id))
        self.assertEqual(doc["messages"], recover(doc))

    def test_completed_status_four_after_preprocessing_uses_latest_native_image(self):
        doc = document()
        doc["messages"][1:1] = [message("precheck", "tool", {
            "content_type": "execution_output", "text": "synthetic inspection"},
            author={"role": "tool", "name": "container.exec"})]
        selected = select(doc)
        self.assertIsNotNone(selected)
        self.assertEqual("generated-image", selected.message_id)
        self.assertEqual(doc["messages"], recover(doc))

    def test_legacy_missing_and_null_async_status_remain_accepted(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                doc = document()
                if missing:
                    del doc["async_status"]
                else:
                    doc["async_status"] = None
                self.assertIsNotNone(select(doc))
                self.assertIsNotNone(recover(doc))

    def test_status_four_is_an_exact_integer_and_unknown_states_stay_rejected(self):
        for status in (True, False, 4.0, "4", 0, 1, 2, 3, 5, -1, "pending", {}, []):
            with self.subTest(status=status):
                doc = document()
                doc["async_status"] = status
                self.assertIsNone(select(doc))
                self.assertIsNone(recover(doc))

    def test_status_four_does_not_override_final_and_current_branch_validation(self):
        mutations = (
            lambda doc: doc["messages"][-1].update(status="in_progress", end_turn=False),
            lambda doc: doc["messages"][-1].update(end_turn=False),
            lambda doc: doc.update(current_node="generated-image"),
            lambda doc: doc["messages"][0].update(id="other-submitted-user"),
            lambda doc: doc["messages"].insert(-1, user_message("new-user-turn")),
            lambda doc: doc.update(conversation_id="other-conversation"),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                doc = document()
                mutate(doc)
                self.assertIsNone(select(doc))
                self.assertIsNone(recover(doc))

    def test_status_four_does_not_hide_unfinished_tool_after_generated_image(self):
        doc = document()
        doc["messages"].insert(-1, message("later-tool", "tool", {
            "content_type": "execution_output", "text": "working"},
            author={"role": "tool", "name": "container.exec"}, status="in_progress"))
        self.assertIsNone(select(doc))
        self.assertIsNone(recover(doc))

    def test_status_four_recovery_must_include_observed_tools_and_calls(self):
        observed = track([image_message("missing-live-image")])
        self.assertIsNone(recover(document(), observed=observed))
        self.assertIsNone(recover(document(), required_tool_calls={"missing-live-call": "python"}))


class ImageToolFailureRegressions(unittest.TestCase):
    def test_completed_image_tool_failure_is_recorded(self):
        tracker = track([failure_message(text=FAILURE_TEXT + " Synthetic diagnostic details.")])
        self.assertTrue(tracker.image_generation_failed)
        self.assertFalse(tracker.generated)

    def test_only_image_tool_failure_prefix_is_evidence(self):
        cases = []
        for role, name in (("user", "image_gen"), ("assistant", "image_gen"),
                           ("tool", "python"), ("tool", "container.exec"),
                           ("tool", "container.open_image"), ("tool", "open_image"),
                           ("tool", "container"), ("tool", "other.open_image")):
            item = failure_message()
            item["author"] = {"role": role, "name": name}
            cases.append(item)
        cases.append(failure_message(text="Quoted diagnostic: " + FAILURE_TEXT))
        item = failure_message()
        item["content"]["content_type"] = "execution_output"
        cases.append(item)
        for index, item in enumerate(cases):
            with self.subTest(case=index):
                self.assertFalse(track([item]).image_generation_failed)

    def test_aliased_tool_exact_failure_prefix_does_not_require_name_guessing(self):
        item = failure_message()
        item["author"]["name"] = "synthetic_alias.generate"
        item["metadata"] = {"is_error": True}
        self.assertTrue(track([item]).image_generation_failed)

    def test_invalid_tool_names_are_not_failure_evidence(self):
        for name in (None, [], {}, "", "bad\nname", "x" * 81):
            with self.subTest(name=name):
                item = failure_message()
                item["author"]["name"] = name
                self.assertFalse(track([item]).image_generation_failed)

    def test_successful_later_native_image_clears_prior_failure(self):
        tracker = track([failure_message(), image_message()])
        self.assertFalse(tracker.image_generation_failed)
        self.assertTrue(tracker.generated)

    def test_later_invalid_image_cannot_clear_prior_technical_failure(self):
        for metadata in ({"error": "failed"}, {"is_complete": False}, {"interrupted": True},
                         {"finish_details": {"type": "error"}}):
            with self.subTest(metadata=metadata):
                generated = image_message()
                generated["metadata"].update(metadata)
                tracker = track([failure_message(), generated, final_message()])
                self.assertTrue(tracker.image_generation_failed)

    def test_inspection_image_preview_cannot_clear_prior_technical_failure(self):
        for name in ("open_image", "other.open_image", "container.preview"):
            with self.subTest(name=name):
                preview = image_message("image-preview")
                preview["author"]["name"] = name
                preview["metadata"] = {}
                tracker = track([failure_message(), preview])
                self.assertTrue(tracker.image_generation_failed)

    def test_later_failure_does_not_leave_prior_image_as_a_current_success(self):
        tracker = track([image_message(), failure_message()])
        self.assertTrue(tracker.image_generation_failed)

    def test_new_user_turn_does_not_inherit_earlier_image_failure(self):
        tracker = track(document(failed=True)["messages"])
        self.assertTrue(tracker.image_generation_failed)
        tracker.update(event(user_message("next-user-turn")))
        self.assertFalse(tracker.image_generation_failed)

    def test_completed_failure_final_is_terminal_without_an_image(self):
        tracker = track(document(failed=True)["messages"])
        self.assertTrue(image_stream._terminal_reply(tracker))
        self.assertFalse(tracker.generated)
        self.assertEqual([], tracker.paths())

    def test_incomplete_failure_final_and_new_work_are_not_terminal(self):
        tracker = track([failure_message(), final_message(finished=False)])
        self.assertFalse(image_stream._terminal_reply(tracker))
        tracker = track(document(failed=True)["messages"])
        tracker.update(event(message("new-call", "assistant", {"content_type": "code", "text": "work()"},
                                     recipient="python")))
        self.assertFalse(image_stream._terminal_reply(tracker))

    def test_original_completed_failure_can_be_recovered_without_creating_a_delivery(self):
        doc = document(failed=True)
        self.assertEqual(doc["messages"], recover(doc))
        self.assertIsNone(select(doc), "失败终态允许释放流，但不能伪造图片产物")

    def test_explicit_failed_tool_metadata_allows_terminal_failure_recovery(self):
        doc = document(failed=True)
        doc["messages"][1]["author"]["name"] = "synthetic_alias.generate"
        doc["messages"][1]["metadata"]["is_error"] = True
        self.assertEqual(doc["messages"], recover(doc))
        self.assertIsNone(select(doc))

    def test_failed_metadata_without_exact_failure_evidence_stays_rejected(self):
        doc = document(failed=True)
        doc["messages"][1]["metadata"]["is_error"] = True
        doc["messages"][1]["content"]["parts"] = ["Unclassified diagnostic."]
        self.assertIsNone(recover(doc))

    def test_unfinished_aliased_failure_tool_cannot_be_recovered(self):
        doc = document(failed=True)
        doc["messages"][1]["author"]["name"] = "synthetic_alias.generate"
        doc["messages"][1]["metadata"]["is_error"] = True
        doc["messages"][1]["status"] = "in_progress"
        self.assertIsNone(recover(doc))


class ImageFailureDeliveryRegressions(unittest.TestCase):
    def backend(self):
        backend = SimpleNamespace(access_token="synthetic", _image_submitted_message_id=SUBMITTED_ID,
                                  resolve_conversation_image_urls=Mock(side_effect=AssertionError("失败终态仍调用了图片轮询")),
                                  download_image_bytes=Mock(return_value=[]),
                                  download_final_sandbox_images=Mock(return_value=[]),
                                  _get_conversation=Mock(return_value=document(failed=True)))
        return backend

    def outputs(self, backend, messages, *, raw_events=None):
        raw_events = [event(item) for item in messages] if raw_events is None else raw_events
        events = [{"raw": raw, "conversation_id": CONVERSATION_ID,
                   "type": "conversation.event"} for raw in raw_events]
        events.append({"conversation_id": CONVERSATION_ID, "done": True, "turn_use_case": "image gen"})
        request = conversation.ConversationRequest(model="gpt-image-2", prompt="synthetic request",
                                                   response_format="b64_json")
        with patch.object(conversation, "conversation_events", return_value=iter(events)), \
                patch.object(conversation.account_service, "bind_image_probe_conversation"), \
                patch.object(conversation, "_get_detailed_error_from_tasks", return_value=""), \
                patch.object(conversation, "_remove_image_conversation_later"):
            return list(conversation.stream_image_outputs(backend, request))

    def test_completed_failure_raises_classified_error_before_polling(self):
        backend = self.backend()
        with self.assertRaises(conversation.ImageGenerationError) as raised:
            self.outputs(backend, document(failed=True)["messages"])
        self.assertEqual("image_generation_failed", raised.exception.code)
        self.assertEqual(CONVERSATION_ID, raised.exception.conversation_id)
        self.assertFalse(conversation.is_model_text_reply_instead_of_image(str(raised.exception)))
        backend.resolve_conversation_image_urls.assert_not_called()
        backend.download_image_bytes.assert_not_called()

    def test_latest_failed_attempt_cannot_fall_back_to_an_earlier_image(self):
        backend = self.backend()
        messages = [user_message(), image_message(), failure_message(), final_message(text="Technical error.")]
        with self.assertRaises(conversation.ImageGenerationError) as raised:
            self.outputs(backend, messages)
        self.assertEqual("image_generation_failed", raised.exception.code)
        backend.resolve_conversation_image_urls.assert_not_called()

    def test_later_successful_native_image_can_be_delivered(self):
        backend = self.backend()
        backend.resolve_conversation_image_urls.side_effect = None
        backend.resolve_conversation_image_urls.return_value = ["https://example.invalid/synthetic.png"]
        png = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(png, format="PNG")
        backend.download_image_bytes.return_value = [png.getvalue()]
        outputs = self.outputs(backend, [user_message(), failure_message(), image_message(), final_message()])
        delivered = [output for output in outputs if output.kind == "result"]
        self.assertEqual(1, len(delivered))
        self.assertEqual(png.getvalue(), base64.b64decode(delivered[0].data[0]["b64_json"]))

    def test_valid_final_sandbox_image_can_supersede_an_earlier_tool_failure(self):
        backend = self.backend()
        png = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(png, format="PNG")
        backend.download_final_sandbox_images.return_value = [png.getvalue()]
        outputs = self.outputs(backend, [user_message(), failure_message(),
            final_message(text="[PNG](sandbox:/mnt/data/final.png)")])
        delivered = [output for output in outputs if output.kind == "result"]
        self.assertEqual(1, len(delivered))
        backend.resolve_conversation_image_urls.assert_not_called()

    def test_successful_python_output_can_supersede_a_later_failed_image_attempt(self):
        # 原图后的再次生图失败，不应覆盖随后已经验证成功的 Python 最终产物。
        backend = self.backend()
        messages = [user_message(), image_message(), failure_message(), *python_pair(), final_message()]
        doc = {"conversation_id": CONVERSATION_ID, "async_status": 4,
               "current_node": "final", "messages": messages}
        terminal = select(doc)
        self.assertIsNotNone(terminal)
        self.assertEqual("python_output", terminal.kind)
        backend._get_conversation.return_value = doc
        png = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(png, format="PNG")
        backend.download_final_sandbox_images.return_value = [png.getvalue()]
        outputs = self.outputs(backend, messages)
        delivered = [output for output in outputs if output.kind == "result"]
        self.assertEqual(1, len(delivered))
        self.assertEqual(png.getvalue(), base64.b64decode(delivered[0].data[0]["b64_json"]))
        backend.download_final_sandbox_images.assert_called_once_with(
            CONVERSATION_ID, "python-result", ["/mnt/data/final.png"])
        backend.resolve_conversation_image_urls.assert_not_called()

    def test_failed_image_attempt_cannot_reuse_earlier_sandbox_contract(self):
        backend = self.backend()
        messages = [user_message(), image_message(), *python_pair(), failure_message(), final_message()]
        backend._get_conversation.return_value = {
            "conversation_id": CONVERSATION_ID, "async_status": 4,
            "current_node": "final", "messages": messages}
        raw_events = [event(item) for item in messages]
        raw_events.insert(2, {"type": "image_review_started", "conversation_id": CONVERSATION_ID,
                              "expected_final_path": "/mnt/data/final.png"})
        png = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(png, format="PNG")
        backend.download_final_sandbox_images.return_value = [png.getvalue()]
        with self.assertRaises(conversation.ImageGenerationError) as raised:
            self.outputs(backend, messages, raw_events=raw_events)
        self.assertEqual("image_generation_failed", raised.exception.code)
        backend.download_final_sandbox_images.assert_not_called()
        backend.resolve_conversation_image_urls.assert_not_called()


class ProductionSilentTailRegressions(unittest.TestCase):
    def run_stream(self, messages, doc):
        server = LocalStreamServer([event(item) for item in messages], document=doc)
        self.addCleanup(server.finish)
        session = requests.Session(headers={"Authorization": "Bearer synthetic"}, retry=0,
                                   curl_options={CurlOpt.PROXY: ""})
        self.addCleanup(session.close)
        backend = SimpleNamespace(base_url=server.base_url, session=session, access_token="synthetic",
                                  image_conversation_id="", _image_submitted_message_id=SUBMITTED_ID)
        started = time.monotonic()
        with patch.object(image_stream, "IMAGE_STREAM_IDLE_SECS", .03), \
                patch.object(image_stream, "IMAGE_STREAM_RECOVERY_INTERVAL_SECS", .02), \
                patch.object(image_stream, "IMAGE_STREAM_FINAL_RECOVERY_SECS", .08):
            values = list(image_stream.stream_image_generation(backend, {"messages": [{"id": SUBMITTED_ID}]},
                {"Accept": "text/event-stream", "Authorization": "Bearer synthetic"}, .6))
        elapsed = time.monotonic() - started
        self.assertIn("[DONE]", values)
        self.assertLess(elapsed, .3, "已确认终态不应继续等上游静默尾流")
        self.assertEqual(1, server.post_count, "恢复原会话不能重复提交生成")
        self.assertTrue(server.peer_closed.wait(.35), "终态后必须释放连接和执行槽")
        return server, values

    def test_completed_technical_failure_closes_silent_tail_without_history_poll(self):
        doc = document(failed=True)
        server, _ = self.run_stream(doc["messages"][1:], doc)
        self.assertEqual([], server.get_paths)

    def test_status_four_document_recovers_final_image_from_silent_tail(self):
        # 初始用户消息只提供本次会话身份；图片及最终助手消息通过原会话 GET 恢复。
        doc = document()
        server, values = self.run_stream([user_message()], doc)
        self.assertTrue(server.get_paths)
        tracker = FinalImageTracker()
        for value in values:
            if value != "[DONE]":
                tracker.update(json.loads(value))
        self.assertTrue(tracker.generated)
        self.assertEqual("final", tracker.final["id"])


if __name__ == "__main__":
    unittest.main()
