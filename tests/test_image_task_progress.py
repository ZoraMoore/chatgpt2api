from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


class ImageTaskProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.config = types.SimpleNamespace(image_retention_days=30)

        # 仅加载待测服务；屏蔽模块级生产配置、账号、日志和协议初始化。
        modules = {name: types.ModuleType(name) for name in (
            "services", "services.config", "services.content_filter", "services.log_service", "services.protocol",
        )}
        modules["services"].__path__ = []
        modules["services.config"].DATA_DIR = self.root
        modules["services.config"].config = self.config
        modules["services.content_filter"].request_text = lambda prompt: prompt
        modules["services.log_service"].LOG_TYPE_CALL = "call"
        modules["services.log_service"].log_service = Mock()
        for name in ("openai_v1_image_edit", "openai_v1_image_generations"):
            setattr(modules["services.protocol"], name, types.SimpleNamespace(handle=Mock()))
        source = Path(__file__).resolve().parents[1] / "services" / "image_task_service.py"
        spec = importlib.util.spec_from_file_location("isolated_image_task_service", source)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", modules):
            spec.loader.exec_module(self.module)
        self.path = self.root / "test_tasks.json"
        self.service = self.module.ImageTaskService(self.path, retention_days_getter=lambda: 30)
        self.service._log_call = Mock()

    def submit(self, *, owner="user-a", task_id="same-task", mode="generate"):
        with patch.object(self.module.threading, "Thread"):
            method = self.service.submit_edit if mode == "edit" else self.service.submit_generation
            return method({"id": owner}, client_task_id=task_id, prompt="保持原提示词", model="gpt-image-2.5", size=None)

    def public(self, owner="user-a", task_id="same-task"):
        return self.service.list_tasks({"id": owner}, [task_id])["items"][0]

    def run_handler(self, handler, *, mode="generate"):
        self.submit(mode=mode)
        self.service.generation_handler = handler
        self.service.edit_handler = handler
        self.service._run_task("user-a:same-task", mode, {"prompt": "保持原提示词"}, {"id": "user-a"}, "gpt-image-2.5")
        return self.public()

    def test_observed_progress_is_deduplicated_bounded_and_restored(self):
        def handler(payload):
            self.assertEqual("保持原提示词", payload["prompt"])
            callback = payload["progress_callback"]
            for step in ("getting_account", "generating", "generating", "thinking", "thinking", "image_generated"):
                callback(step)
            return {"data": [{"url": "https://example.test/image.png"}]}

        public = self.run_handler(handler)
        self.assertEqual("success", public["status"])
        self.assertEqual(["getting_account", "generating", "thinking", "image_generated"],
                         [entry["stage"] for entry in public["progress_history"]])
        self.assertEqual("image_generated", public["progress"])
        restored = self.module.ImageTaskService(self.path)
        self.assertEqual(public["progress_history"], restored.list_tasks({"id": "user-a"}, ["same-task"])["items"][0]["progress_history"])

    def test_repeated_and_unknown_progress_does_not_rewrite_task(self):
        self.submit()
        self.service._update_task("user-a:same-task", progress="thinking")
        with patch.object(self.service, "_save_locked") as save:
            for step in ("thinking", "arbitrary upstream text", {"stage": "thinking"}, ""):
                self.service._update_task("user-a:same-task", progress=step)
            save.assert_not_called()
        self.assertEqual(1, len(self.public()["progress_history"]))

    def test_history_keeps_only_latest_bounded_stage_transitions(self):
        self.submit()
        for index in range(self.module.MAX_PROGRESS_HISTORY + 12):
            self.service._update_task("user-a:same-task", progress="thinking" if index % 2 else "running_code")
        history = self.public()["progress_history"]
        self.assertEqual(self.module.MAX_PROGRESS_HISTORY, len(history))
        self.assertTrue(all(set(entry) == {"stage", "at"} for entry in history))

    def test_summary_whitelist_removes_private_and_malformed_fields(self):
        summary = {
            "conversation_id": "private-conversation", "account_email": "private@example.test",
            "private_notes": "private reasoning", "code": "private code", "unexpected": {"secret": "private"},
            "tools": {"container.exec": 4, "container.open_image": 2, "python": True,
                      "image_tool": -1, "image_gen": 10001, "unrecognized.secret": 8},
            "thought_message_count": 2, "upstream_model_slug": "gpt-6-thinking",
            "code_tool_after_image": True, "stream_complete": True,
            "skipped_mainline": "false", "result_source": "sandbox",
        }
        public = self.run_handler(lambda _: {"data": [{"url": "https://example.test/image.png"}], "_image_execution": [summary]})
        expected = {
            "tools": {"container.exec": 4, "container.open_image": 2}, "thought_message_count": 2,
            "upstream_model_slug": "gpt-6-thinking", "code_tool_after_image": True,
            "stream_complete": True, "result_source": "sandbox",
        }
        self.assertEqual([expected], public["image_execution"])
        self.assertNotIn("private", self.path.read_text())
        public["image_execution"][0]["tools"]["container.exec"] = 999
        self.assertEqual(4, self.public()["image_execution"][0]["tools"]["container.exec"])

    def test_failure_and_empty_result_preserve_safe_execution_evidence(self):
        summary = {"tools": {"python": 1}, "result_source": "unavailable", "conversation_id": "private"}

        def handler(_):
            error = RuntimeError("图片未完整交付")
            error.image_execution = [summary]
            raise error

        public = self.run_handler(handler)
        self.assertEqual("error", public["status"])
        self.assertEqual([{"tools": {"python": 1}, "result_source": "unavailable"}], public["image_execution"])
        self.service._tasks.clear()
        public = self.run_handler(lambda _: {"data": [], "message": "没有最终图片", "_image_execution": [summary]})
        self.assertEqual("error", public["status"])
        self.assertEqual("unavailable", public["image_execution"][0]["result_source"])

    def test_terminal_task_ignores_delayed_progress(self):
        self.run_handler(lambda _: {"data": [{"url": "https://example.test/image.png"}]})
        with patch.object(self.service, "_save_locked") as save:
            self.service._update_task("user-a:same-task", progress="running_code")
            self.service._update_task("user-a:same-task", progress="generating", started_ts=1)
            save.assert_not_called()
        self.assertNotIn("progress_history", self.public())

    def test_loading_filters_untrusted_history_and_execution(self):
        self.submit()
        records = json.loads(self.path.read_text())
        records["tasks"][0].update({
            "status": "success", "progress": "unknown raw text", "progress_history": [
                {"stage": "thinking", "at": "2026-01-01 00:00:00", "text": "secret"},
                {"stage": "thinking", "at": "2026-01-01 00:00:01"},
                {"stage": "running_code", "at": "2026-13-01 00:00:00"},
                {"stage": "checking_image", "at": "2026-01-01 00:00:00 trailing secret"},
                {"stage": "private stage", "at": "2026-01-01 00:00:00"},
                {"stage": "finalizing_image", "at": "2026-01-01 00:00:02"},
            ],
            "image_execution": [{"account_email": "private", "tools": {"python": 1},
                                 "thought_message_count": False, "result_source": ["sandbox"],
                                 "private_notes": "private text", "upstream_model_slug": "secret token"}],
        })
        self.path.write_text(json.dumps(records))
        restored = self.module.ImageTaskService(self.path)
        public = restored.list_tasks({"id": "user-a"}, ["same-task"])["items"][0]
        self.assertEqual("finalizing_image", public["progress"])
        self.assertEqual(["thinking", "finalizing_image"], [entry["stage"] for entry in public["progress_history"]])
        self.assertEqual([{"tools": {"python": 1}}], public["image_execution"])
        self.assertNotIn("secret", json.dumps(public))
        self.assertNotIn("private", json.dumps(public))

    def test_recovery_keeps_histories_isolated_by_owner(self):
        for owner, stage in (("user-a", "thinking"), ("user-b", "running_code")):
            self.submit(owner=owner)
            self.service._update_task(f"{owner}:same-task", status="running", progress=stage)
        restored = self.module.ImageTaskService(self.path)
        for owner, expected in (("user-a", "thinking"), ("user-b", "running_code")):
            items = restored.list_tasks({"id": owner}, [])["items"]
            self.assertEqual(1, len(items))
            self.assertEqual("error", items[0]["status"])
            self.assertEqual([expected], [entry["stage"] for entry in items[0]["progress_history"]])
        self.assertEqual({"items": [], "missing_ids": ["same-task"]}, restored.list_tasks({"id": "user-c"}, ["same-task"]))

    def test_invalid_summary_types_and_counts_are_rejected(self):
        invalid = [{"upstream_model_slug": "x" * 81, "thought_message_count": -1, "stream_complete": 1},
                   {"thought_message_count": 10001, "tools": {"python": 1.5}}, None, "secret"]
        self.assertEqual([{"tools": {}}], self.module._safe_image_execution(invalid))
        self.assertEqual([], self.module._safe_image_execution({"tools": {"python": 1}}))
        summaries = [{"result_source": "image_tool"}] * 100
        self.assertEqual(self.module.MAX_IMAGE_EXECUTIONS, len(self.module._safe_image_execution(summaries)))

    def test_default_resume_and_other_owner_behavior_remain_unchanged(self):
        self.submit(mode="edit")
        self.service._update_task("user-a:same-task", status="error", error="生图超时", conversation_id="private")
        with patch.object(self.module.threading, "Thread") as thread:
            with self.assertRaisesRegex(ValueError, "not found"):
                self.service.resume_poll({"id": "user-b"}, "same-task")
            thread.assert_not_called()
            task = self.service.resume_poll({"id": "user-a"}, "same-task")
            self.assertEqual("running", task["status"])
            thread.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
