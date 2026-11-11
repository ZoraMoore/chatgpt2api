from __future__ import annotations

import base64
import copy
import io
import json
import os
import unittest
from unittest.mock import Mock, PropertyMock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "synthetic-test-key")

from PIL import Image

from services.config import config
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    collect_image_outputs,
    stream_image_outputs,
)


CID = "22222222-2222-4222-8222-222222222222"
SUBMITTED = "synthetic-submitted-user"
INPUT_ID = "file_00000000" + "a" * 24
ALIAS_ID = "file_00000000" + "b" * 24
FINAL_PATH = "/mnt/data/final.png"


def png(color):
    output = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(output, format="PNG")
    return output.getvalue()


INPUT_BYTES = png("red")


def file_id(index):
    return "file_00000000" + f"{index:024x}"


def message(mid, role="assistant", *, content=None, recipient="all", name=None,
            channel=None, end_turn=False, metadata=None, status="finished_successfully"):
    return {
        "id": mid,
        "author": {"role": role, "name": name},
        "recipient": recipient,
        "channel": channel,
        "end_turn": end_turn,
        "status": status,
        "content": content or {"content_type": "text", "parts": [""]},
        "metadata": metadata or {},
    }


def user_message():
    return message(
        SUBMITTED,
        "user",
        content={"content_type": "multimodal_text", "parts": [
            {"content_type": "image_asset_pointer", "asset_pointer": "sediment://" + INPUT_ID},
            "synthetic image edit request",
        ]},
        metadata={"attachments": [{"id": INPUT_ID, "mime_type": "image/png"}]},
    )


def image_call(index, *, reference_id=""):
    arguments = {"prompt": "synthetic edit", "n": 1}
    if reference_id:
        arguments["referenced_image_ids"] = [reference_id]
    return message(
        f"image-call-{index}",
        recipient="synthetic.image",
        channel="analysis",
        content={"content_type": "code", "text": json.dumps(arguments)},
    )


def image_result(index, *ids):
    return message(
        f"image-result-{index}",
        "tool",
        name="synthetic.image",
        content={"content_type": "multimodal_text", "parts": [
            {"content_type": "image_asset_pointer", "asset_pointer": "sediment://" + fid,
             "width": 8, "height": 6}
            for fid in ids
        ]},
        metadata={"async_task_type": "image_gen", "image_gen_title": "Synthetic image", "is_complete": True},
    )


def final_message(text="", *, end_turn=True, metadata=None):
    return message(
        "synthetic-final",
        channel="final",
        end_turn=end_turn,
        content={"content_type": "text", "parts": [text]},
        metadata=metadata,
    )


def revisions(count):
    rows = [user_message()]
    for index in range(1, count + 1):
        rows.extend([image_call(index), image_result(index, file_id(index))])
    return [*rows, final_message()]


class ImageFinalContractTests(unittest.TestCase):
    def setUp(self):
        # 配置仅来自隔离容器的临时 data；测试显式关闭存储和会话清理副作用。
        for name in ("image_management_enabled", "image_remove_conversation_after_result",
                     "image_require_reference_postprocess"):
            self.enterContext(patch.object(type(config), name, new_callable=PropertyMock, return_value=False))
        self.enterContext(patch("services.protocol.conversation.account_service", Mock()))

    def backend(self, rows, *, image_bytes=None, sandbox_bytes=None):
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "synthetic-access-token"
        backend.base_url = "https://example.invalid"
        backend.image_conversation_id = CID
        backend._image_submitted_message_id = SUBMITTED
        payloads = [json.dumps({"conversation_id": CID, "message": rows[0]})]
        payloads.append(json.dumps({"type": "server_ste_metadata", "conversation_id": CID,
                                   "metadata": {"tool_invoked": True, "turn_use_case": "image gen"}}))
        payloads.extend(json.dumps({"conversation_id": CID, "message": row}) for row in rows[1:])
        payloads.append("[DONE]")
        backend.stream_conversation = Mock(return_value=iter(payloads))
        backend._get_conversation = Mock(return_value={"conversation_id": CID, "messages": copy.deepcopy(rows)})
        backend._query_backend_tasks = Mock(return_value=[])
        backend._get_file_download_url = Mock(side_effect=lambda fid: f"https://example.invalid/{fid}.png")
        backend._get_attachment_download_url = Mock(side_effect=lambda _cid, fid: f"https://example.invalid/{fid}.png")
        images = {INPUT_ID: INPUT_BYTES, **(image_bytes or {})}
        backend._request_image_transfer = Mock(side_effect=lambda _method, url, _context, **_kw:
                                             Mock(content=images[url.rsplit("/", 1)[-1][:-4]]))
        files = sandbox_bytes or {FINAL_PATH: png("blue")}
        backend.download_final_sandbox_images = Mock(side_effect=lambda _cid, _mid, paths:
                                                    [files[path] for path in paths])
        return backend

    def run_result(self, backend):
        request = ConversationRequest(
            model="gpt-image-2.6-sunburst", prompt="synthetic image edit request",
            images=[base64.b64encode(INPUT_BYTES).decode("ascii")], n=1,
            image_transport="conversation", response_format="b64_json",
        )
        return collect_image_outputs(stream_image_outputs(backend, request))

    def assert_only_image(self, result, expected):
        self.assertEqual(1, len(result["data"]), "一次子任务必须只交付可确认的最终图片")
        self.assertEqual(expected, base64.b64decode(result["data"][0]["b64_json"]))

    def test_three_revisions_deliver_only_last_image_after_complete_empty_final(self):
        images = {file_id(i): png((i * 30, 40, 100)) for i in range(1, 4)}
        self.assert_only_image(self.run_result(self.backend(revisions(3), image_bytes=images)), images[file_id(3)])

    def test_five_revisions_deliver_only_last_image_after_complete_empty_final(self):
        images = {file_id(i): png((i * 30, 40, 100)) for i in range(1, 6)}
        self.assert_only_image(self.run_result(self.backend(revisions(5), image_bytes=images)), images[file_id(5)])

    def test_input_referenced_again_in_assistant_tool_arguments_is_not_an_output(self):
        rows = [user_message(), image_call(1, reference_id=INPUT_ID), image_result(1, file_id(1)), final_message()]
        expected = png("blue")
        self.assert_only_image(self.run_result(self.backend(rows, image_bytes={file_id(1): expected})), expected)

    def test_output_alias_with_exact_input_bytes_cannot_be_delivered(self):
        rows = [user_message(), image_call(1), image_result(1, ALIAS_ID), final_message()]
        backend = self.backend(rows, image_bytes={ALIAS_ID: INPUT_BYTES})
        with self.assertRaises(ImageGenerationError):
            self.run_result(backend)

    def test_explicit_final_sandbox_link_does_not_merge_old_metadata_paths(self):
        rows = revisions(1)
        rows[-1] = final_message(
            "[最终图](sandbox:/mnt/data/final.png)",
            metadata={"model_dil_v2": {"fallbackMarkdown": "[旧图](sandbox:/mnt/data/old.png)",
                                       "appData": {"url": "sandbox:/mnt/data/older.png"}},
                      "content_references": [{"url": "sandbox:/mnt/data/reference.png"}]},
        )
        files = {FINAL_PATH: png("blue"), "/mnt/data/old.png": png("yellow"),
                 "/mnt/data/older.png": png("green"), "/mnt/data/reference.png": INPUT_BYTES}
        backend = self.backend(rows, image_bytes={file_id(1): png("yellow")}, sandbox_bytes=files)
        self.assert_only_image(self.run_result(backend), files[FINAL_PATH])

    def test_final_explicitly_declining_qualified_delivery_is_a_failure(self):
        rows = revisions(1)
        rows[-1] = final_message("本次生成未能满足保持原图构图与细节不变的要求，因此不能作为合格结果交付。")
        backend = self.backend(rows, image_bytes={file_id(1): png("blue")})
        with self.assertRaises(ImageGenerationError):
            self.run_result(backend)

    def test_done_without_end_turn_cannot_deliver_a_candidate(self):
        rows = revisions(1)
        rows[-1] = final_message(end_turn=False)
        backend = self.backend(rows, image_bytes={file_id(1): png("blue")})
        with self.assertRaises(ImageGenerationError):
            self.run_result(backend)

    def test_two_pointers_in_last_tool_are_ambiguous_for_one_image(self):
        rows = [user_message(), image_call(1), image_result(1, file_id(1), file_id(2)), final_message()]
        backend = self.backend(rows, image_bytes={file_id(1): png("blue"), file_id(2): png("green")})
        with self.assertRaises(ImageGenerationError):
            self.run_result(backend)

    def test_failed_postprocess_cannot_fall_back_to_earlier_generated_image(self):
        rows = revisions(1)[:-1]
        rows.extend([
            message("postprocess-call", recipient="python", channel="analysis",
                    content={"content_type": "code", "text": "synthetic_postprocess()"}),
            message("postprocess-result", "tool", name="python", status="error",
                    content={"content_type": "execution_output", "text": "Synthetic postprocess failed"},
                    metadata={"parent_id": "postprocess-call", "is_complete": True,
                              "aggregate_result": {"status": "error", "error": "synthetic failure"}}),
            final_message(),
        ])
        backend = self.backend(rows, image_bytes={file_id(1): png("blue")})
        with self.assertRaises(ImageGenerationError):
            self.run_result(backend)
        backend._request_image_transfer.assert_not_called()
        backend.download_final_sandbox_images.assert_not_called()


if __name__ == "__main__":
    unittest.main()
