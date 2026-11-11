"""真实异步图片路由与任务执行器必须传递内部恢复检查点。"""
from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

from fastapi import FastAPI

from api import ai
from services import image_task_service as tasks
from tests.test_delivery_checkpoint_tasks import CONTEXT
from tests.test_image_final_contract import INPUT_BYTES


def checkpoint_event(index=1, total=1, *, phase="pending"):
    context = copy.deepcopy(CONTEXT)
    cid = f"original-conversation-{index}"
    context["submitted_message_id"] = f"submitted-{index}"
    evidence = context["observed_evidence"]
    evidence.update(conversation_id=cid, tool_ids=[f"image-result-{index}"],
                    tool_calls={f"image-call-{index}": "image.generate"},
                    asset_ids_by_message={f"image-result-{index}": [f"file-output-{index}"]})
    return {"phase": phase, "index": index, "total": total, "conversation_id": cid,
            "resume_account_ref": str(index) * 64, "delivery_context": context,
            **({"data": [{"url": f"https://example.test/final-{index}.png"}]} if phase == "success" else {})}


class RecoveryApiWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "tasks.json"
        self.service = tasks.ImageTaskService(self.path)
        self.service._log_call = Mock()
        self.identity = {"id": "authenticated-owner", "name": "synthetic user"}
        self.enterContext(patch.object(ai, "image_task_service", self.service))
        self.auth = self.enterContext(patch.object(ai, "require_identity", return_value=self.identity))
        self.enterContext(patch.object(ai, "check_request", Mock()))
        self.enterContext(patch.object(ai.LoggedCall, "log", Mock()))
        self.enterContext(patch.object(ai, "new_uuid", return_value="api-call"))
        self.enterContext(patch.object(type(ai.config), "image_transport",
                                       new_callable=PropertyMock, return_value="conversation"))
        self.app = FastAPI()
        self.app.include_router(ai.create_router())

    async def request_image(self, mode, *, complete=False):
        captured = {}
        task_ids = {}
        expected = {"created": 123, "data": [
            {"url": "https://example.test/final-1.png"}, {"url": "https://example.test/final-2.png"}]}

        def handle(payload):
            captured.update(payload)
            callback = payload["_delivery_checkpoint_callback"]
            # 先登记第二张，再完成第一张，模拟多个子任务的不同执行顺序。
            for index in (2, 1):
                task_ids[index] = callback(checkpoint_event(index, total=2))
            if complete:
                for index in (1, 2):
                    callback(checkpoint_event(index, total=2, phase="success"))
            return copy.deepcopy(expected)

        body = {"prompt": "synthetic request", "model": "gpt-image-2", "n": 2,
                "owner_id": "external-owner", "resume_account_ref": "f" * 64,
                "delivery_context": {"submitted_message_id": "external-user"},
                "_delivery_checkpoint_callback": "external-callback"}
        if mode == "edit":
            body["images"] = [{"b64_json": base64.b64encode(INPUT_BYTES).decode("ascii")}]
        module = ai.openai_v1_image_edit if mode == "edit" else ai.openai_v1_image_generations
        route = "/v1/images/edits" if mode == "edit" else "/v1/images/generations"
        encoded = json.dumps(body).encode("utf-8")
        messages = []

        async def receive():
            return {"type": "http.request", "body": encoded, "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {"type": "http", "http_version": "1.1", "method": "POST", "scheme": "https",
                 "path": route, "raw_path": route.encode("ascii"), "query_string": b"",
                 "headers": [(b"host", b"example.test"), (b"content-type", b"application/json"),
                             (b"authorization", b"Bearer synthetic-auth")],
                 "server": ("example.test", 443), "client": ("127.0.0.1", 12345),
                 "asgi": {"version": "3.0"}}
        with patch.object(module, "handle", side_effect=handle) as handler:
            await self.app(scope, receive, send)
        start = next(message for message in messages if message["type"] == "http.response.start")
        response_body = b"".join(message.get("body", b"") for message in messages
                                 if message["type"] == "http.response.body")
        self.assertEqual(200, start["status"])
        self.assertEqual(expected, json.loads(response_body), "增加内部恢复能力不能改变原成功 JSON")
        handler.assert_called_once()
        self.auth.assert_called_once_with("Bearer synthetic-auth")
        return captured, task_ids

    def assert_private_owner_binding(self, captured, task_ids, mode):
        self.assertTrue(callable(captured.get("_delivery_checkpoint_callback")))
        for name in ("owner_id", "resume_account_ref", "delivery_context"):
            self.assertNotIn(name, captured, "外部 JSON 不能向内部恢复链注入身份或证据")
        restored = tasks.ImageTaskService(self.path)
        records = {item["id"]: item for item in json.loads(self.path.read_text(encoding="utf-8"))["tasks"]}
        self.assertEqual(set(task_ids.values()), set(records))
        for index, task_id in task_ids.items():
            expected = checkpoint_event(index, total=2)
            self.assertEqual("authenticated-owner", records[task_id]["owner_id"])
            self.assertEqual(mode, records[task_id]["mode"])
            self.assertEqual(expected["conversation_id"], records[task_id]["conversation_id"])
            self.assertEqual(expected["resume_account_ref"], records[task_id]["resume_account_ref"])
            self.assertEqual(expected["delivery_context"], records[task_id]["delivery_context"])
            public = restored.list_tasks(self.identity, [task_id])["items"][0]
            self.assertNotIn("resume_account_ref", public)
            self.assertNotIn("delivery_context", public)
        self.assertEqual([], restored.list_tasks({"id": "external-owner"}, list(task_ids.values()))["items"])

    async def test_generation_route_binds_internal_checkpoint_and_preserves_success_json(self):
        captured, task_ids = await self.request_image("generate")
        self.assert_private_owner_binding(captured, task_ids, "generate")

    async def test_edit_route_parses_input_and_binds_internal_checkpoint_without_identity_injection(self):
        captured, task_ids = await self.request_image("edit")
        self.assert_private_owner_binding(captured, task_ids, "edit")
        self.assertEqual(1, len(captured["images"]))
        self.assertEqual(INPUT_BYTES, captured["images"][0][0])

    async def test_n_result_callbacks_keep_each_original_account_and_final_artifact(self):
        captured, task_ids = await self.request_image("generate", complete=True)
        self.assert_private_owner_binding(captured, task_ids, "generate")
        self.assertNotEqual(task_ids[1], task_ids[2])
        restored = tasks.ImageTaskService(self.path)
        for index, task_id in task_ids.items():
            public = restored.list_tasks(self.identity, [task_id])["items"][0]
            self.assertEqual("success", public["status"])
            self.assertEqual([{"url": f"https://example.test/final-{index}.png"}], public["data"])

    def test_async_task_checkpoint_is_reloadable_before_handler_returns(self):
        for mode in ("generate", "edit"):
            with self.subTest(mode=mode):
                task_id = f"async-{mode}"
                submit = self.service.submit_generation if mode == "generate" else self.service.submit_edit
                with patch.object(tasks, "image_work_pool"):
                    submit(self.identity, client_task_id=task_id, prompt="synthetic request",
                           model="gpt-image-2", size=None, image_transport="conversation")
                observed = []

                def handle(payload):
                    public_id = payload["_delivery_checkpoint_callback"](checkpoint_event())
                    # 在 handler 尚未返回、尚未进入异常保存逻辑时重载，证明检查点已落盘。
                    restored = tasks.ImageTaskService(self.path)
                    observed.append((public_id, copy.deepcopy(restored._tasks.get(
                        f"authenticated-owner:{task_id}", {}))))
                    return {"data": [{"url": "https://example.test/final-1.png"}]}

                handler = Mock(side_effect=handle)
                if mode == "generate":
                    self.service.generation_handler = handler
                else:
                    self.service.edit_handler = handler
                self.service._run_task(f"authenticated-owner:{task_id}", mode,
                                       {"prompt": "synthetic request", "_image_transport": "conversation"},
                                       self.identity, "gpt-image-2")
                handler.assert_called_once()
                self.assertEqual(1, len(observed), "handler 内的 checkpoint 必须立即写入原异步任务")
                public_id, recovered = observed[0]
                expected = checkpoint_event()
                self.assertEqual(task_id, public_id)
                self.assertEqual(expected["conversation_id"], recovered.get("conversation_id"))
                self.assertEqual(expected["resume_account_ref"], recovered.get("resume_account_ref"))
                self.assertEqual(expected["delivery_context"], recovered.get("delivery_context"))
                final = self.service.list_tasks(self.identity, [task_id])["items"][0]
                self.assertEqual("success", final["status"])


if __name__ == "__main__":
    unittest.main()
