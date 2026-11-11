from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "isolated-test-key")

from services import image_task_service as tasks


CONTEXT = {
    "submitted_message_id": "user-message_1",
    "input_asset_ids": ["file-input_1", "asset:input.2"],
    "input_image_hashes": ["a" * 64],
}


class ImageTaskDeliveryContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "tasks.json"
        self.service = self.make_service()
        self.identity = {"id": "owner"}

    def make_service(self):
        service = tasks.ImageTaskService(self.path, retention_days_getter=lambda: 30)
        service._log_call = Mock()
        return service

    def submit(self, task_id="task"):
        with patch.object(tasks, "image_work_pool"):
            self.service.submit_generation(
                self.identity, client_task_id=task_id, prompt="synthetic", model="gpt-image-2", size=None,
                image_transport="conversation",
            )
        return "owner:" + task_id

    def fail_generation(self, context, task_id="task"):
        key = self.submit(task_id)
        error = RuntimeError("synthetic incomplete delivery")
        error.conversation_id = "original-conversation"
        error.resume_account_ref = "b" * 64
        error.delivery_context = context
        self.service.generation_handler = Mock(side_effect=error)
        self.service._run_task(key, "generate", {"prompt": "synthetic"}, self.identity, "gpt-image-2")
        return key

    def run_resume(self, service, key, *, error=None):
        backend = Mock()
        account = Mock()
        account.acquire_image_resume_token.return_value = "synthetic-token"
        account.refresh_access_token.return_value = "synthetic-token"
        result = {"data": [{"url": "https://example.test/final.png"}]}
        with patch.object(tasks, "account_service", account), \
             patch("services.openai_backend_api.OpenAIBackendAPI", return_value=backend), \
             patch("services.protocol.conversation.ConversationRequest", side_effect=lambda **kw: SimpleNamespace(**kw)) as request, \
             patch("services.protocol.conversation.stream_image_outputs", side_effect=error, return_value=iter(())) as stream, \
             patch("services.protocol.conversation.collect_image_outputs", return_value=result):
            service._run_resume_poll(key, "original-conversation", 30, self.identity, "generate", "gpt-image-2")
        return request, stream, account, backend

    def test_failed_generation_persists_private_context_and_copies_lists(self):
        context = copy.deepcopy(CONTEXT)
        key = self.fail_generation(context)
        self.assertEqual(CONTEXT, self.service._tasks[key].get("delivery_context"))
        persisted = json.loads(self.path.read_text(encoding="utf-8"))["tasks"][0]
        self.assertEqual(CONTEXT, persisted.get("delivery_context"))
        context["input_asset_ids"].append("later-mutated-input")
        self.assertEqual(CONTEXT, self.service._tasks[key]["delivery_context"])
        public = self.service.list_tasks(self.identity, ["task"])["items"][0]
        self.assertNotIn("delivery_context", public)
        self.assertNotIn("submitted_message_id", json.dumps(public))
        self.assertNotIn("input_image_hashes", json.dumps(public))

    def test_invalid_or_credential_context_is_not_persisted(self):
        invalid = [
            None, [], "invalid", {}, {"submitted_message_id": "incomplete-context"},
            {"access_token": "synthetic-secret-marker"},
            {**CONTEXT, "authorization": "synthetic-secret-marker"},
            {**CONTEXT, "submitted_message_id": ""},
            {**CONTEXT, "submitted_message_id": "x" * 129},
            {**CONTEXT, "submitted_message_id": "bad\nidentifier"},
            {**CONTEXT, "submitted_message_id": True},
            {**CONTEXT, "input_asset_ids": "file-input"},
            {**CONTEXT, "input_asset_ids": ["file-input"] * 21},
            {**CONTEXT, "input_asset_ids": ["file/input"]},
            {**CONTEXT, "input_asset_ids": ["x" * 129]},
            {**CONTEXT, "input_asset_ids": [""]},
            {**CONTEXT, "input_asset_ids": [1]},
            {**CONTEXT, "input_image_hashes": ["a" * 64] * 11},
            {**CONTEXT, "input_image_hashes": "a" * 64},
            {**CONTEXT, "input_image_hashes": ["g" * 64]},
            {**CONTEXT, "input_image_hashes": ["a" * 63]},
            {**CONTEXT, "input_image_hashes": [False]},
        ]
        for index, context in enumerate(invalid):
            with self.subTest(index=index):
                key = self.fail_generation(context, "invalid-" + str(index))
                self.assertNotIn("delivery_context", self.service._tasks[key])
        self.assertNotIn("synthetic-secret-marker", self.path.read_text(encoding="utf-8"))

    def test_valid_boundary_values_are_preserved(self):
        context = {
            "submitted_message_id": "x" * 128,
            "input_asset_ids": ["asset-" + str(index) for index in range(20)],
            "input_image_hashes": ["A" * 64] * 10,
        }
        key = self.fail_generation(context)
        self.assertEqual(context, self.service._tasks[key].get("delivery_context"))

    def test_reload_restores_context_without_public_exposure(self):
        key = self.fail_generation(copy.deepcopy(CONTEXT))
        restored = self.make_service()
        self.assertEqual(CONTEXT, restored._tasks[key].get("delivery_context"))
        public = restored.list_tasks(self.identity, ["task"])["items"][0]
        self.assertNotIn("delivery_context", public)

    def test_reload_rejects_untrusted_persisted_context(self):
        self.submit()
        records = json.loads(self.path.read_text(encoding="utf-8"))
        records["tasks"][0].update({"status": "error", "delivery_context": {**CONTEXT, "cookie": "synthetic-secret-marker"}})
        self.path.write_text(json.dumps(records), encoding="utf-8")
        restored = self.make_service()
        self.assertNotIn("delivery_context", restored._tasks["owner:task"])

    def test_resume_receives_restored_context_and_only_reads_original_conversation(self):
        key = self.fail_generation(copy.deepcopy(CONTEXT))
        restored = self.make_service()
        restored.generation_handler = Mock(side_effect=AssertionError("generation must not be replayed"))
        request, stream, account, backend = self.run_resume(restored, key)
        self.assertEqual(CONTEXT, request.call_args.kwargs.get("delivery_context"))
        self.assertEqual("original-conversation", stream.call_args.kwargs["resume_conversation_id"])
        account.acquire_image_resume_token.assert_called_once_with("b" * 64, conversation_id="original-conversation")
        account.release_image_slot.assert_called_once_with("synthetic-token", resume=True)
        restored.generation_handler.assert_not_called()
        backend.stream_conversation.assert_not_called()
        self.assertEqual("success", restored._tasks[key]["status"])

    def test_legacy_task_resumes_with_empty_context(self):
        key = self.fail_generation(None)
        request, _, _, _ = self.run_resume(self.service, key)
        self.assertEqual({}, request.call_args.kwargs.get("delivery_context"))
        self.assertEqual("success", self.service._tasks[key]["status"])

    def test_failed_resume_keeps_original_context_when_exception_has_none_or_invalid(self):
        for index, context in enumerate((None, {**CONTEXT, "cookie": "synthetic-secret-marker"})):
            with self.subTest(index=index):
                key = self.fail_generation(copy.deepcopy(CONTEXT), "retry-" + str(index))
                error = RuntimeError("synthetic still pending")
                if context is not None:
                    error.delivery_context = context
                self.run_resume(self.service, key, error=error)
                self.assertEqual("error", self.service._tasks[key]["status"])
                self.assertEqual(CONTEXT, self.service._tasks[key].get("delivery_context"))
                restored = self.make_service()
                self.assertEqual(CONTEXT, restored._tasks[key].get("delivery_context"))

    def test_failed_resume_saves_new_valid_context_from_exception(self):
        key = self.fail_generation(None)
        error = RuntimeError("synthetic still pending")
        error.delivery_context = copy.deepcopy(CONTEXT)
        self.run_resume(self.service, key, error=error)
        self.assertEqual(CONTEXT, self.service._tasks[key].get("delivery_context"))


if __name__ == "__main__":
    unittest.main()
