from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import asyncio
from unittest.mock import patch
from pathlib import Path

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.log_service import LogService, LoggedCall
from services.protocol.conversation import ImageOutput, ImageGenerationError, collect_image_outputs


class LogServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "logs.jsonl"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_reads_newest_entries_across_rotated_files(self) -> None:
        service = LogService(self.path, max_bytes=300, backup_count=8)
        for index in range(12):
            service.add("call" if index % 2 == 0 else "account", f"item-{index}")

        items = service.list(limit=12)

        self.assertEqual([f"item-{index}" for index in range(11, -1, -1)], [item["summary"] for item in items])
        self.assertEqual(6, len(service.list(type="call", limit=20)))
        self.assertTrue(self.path.with_name("logs.jsonl.1").exists())

    def test_list_skips_malformed_lines_without_loading_whole_file(self) -> None:
        valid = {"time": "2026-09-15 10:00:00", "type": "call", "summary": "valid", "detail": {}}
        self.path.write_text("not-json\n" + json.dumps(valid) + "\n", encoding="utf-8")
        service = LogService(self.path)

        items = service.list(limit=10)

        self.assertEqual(1, len(items))
        self.assertEqual("valid", items[0]["summary"])
        self.assertTrue(items[0]["id"])

    def test_delete_streams_and_removes_entries_from_rotated_files(self) -> None:
        service = LogService(self.path, max_bytes=240, backup_count=8)
        for index in range(10):
            service.add("call", f"item-{index}")
        items = service.list(limit=10)
        target = next(item for item in items if item["summary"] == "item-2")

        result = service.delete([target["id"]])

        self.assertEqual({"removed": 1}, result)
        self.assertNotIn("item-2", [item["summary"] for item in service.list(limit=20)])

    def test_concurrent_adds_produce_complete_json_lines(self) -> None:
        service = LogService(self.path, max_bytes=10 * 1024 * 1024, backup_count=2)

        def writer(worker: int) -> None:
            for index in range(100):
                service.add("call", f"{worker}-{index}")

        threads = [threading.Thread(target=writer, args=(worker,)) for worker in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(800, len(lines))
        self.assertTrue(all(isinstance(json.loads(line), dict) for line in lines))

    def test_image_execution_is_logged_but_not_leaked_into_api_response(self):
        summary = {"conversation_id": "test-conversation", "tools": {"container.exec": 2},
                   "review_requested": True, "result_source": "sandbox"}
        result = collect_image_outputs([ImageOutput(kind="result", model="gpt-image-2.5", index=1, total=1,
            conversation_id="test-conversation", data=[{"b64_json": "test"}], execution=summary)])
        service = LogService(self.path)
        with patch("services.log_service.log_service", service):
            response = asyncio.run(LoggedCall({"id": "test"}, "/v1/images/edits", "gpt-image-2.5", "test").run(lambda: result))
        self.assertNotIn("_image_execution", response)
        self.assertNotIn("_conversation_id", response)
        detail = service.list()[0]["detail"]
        self.assertEqual("test-conversation", detail["conversation_id"])
        self.assertEqual([summary], detail["image_execution"])

    def test_missing_final_image_error_preserves_tool_evidence_in_log(self):
        summary = {"tools": {"container.exec": 2}, "result_source": "unavailable"}
        service = LogService(self.path)
        def handler():
            raise ImageGenerationError("missing final image", conversation_id="test", image_execution=[summary])
        with patch("services.log_service.log_service", service):
            response = asyncio.run(LoggedCall({"id": "test"}, "/v1/images/edits", "gpt-image-2.5", "test").run(handler))
        self.assertEqual(502, response.status_code)
        self.assertEqual([summary], service.list()[0]["detail"]["image_execution"])

    def test_streamed_image_execution_is_logged_and_removed_from_client_chunks(self):
        summary = {"tools": {"python": 1}, "result_source": "sandbox"}
        service = LogService(self.path)
        chunks = [{"_image_execution": [summary], "_conversation_id": "test", "data": [{"url": "https://example.test/image"}]}]
        with patch("services.log_service.log_service", service):
            public = list(LoggedCall({"id": "test"}, "/v1/images/edits", "gpt-image-2.5", "test").stream(iter(chunks)))
        self.assertNotIn("_image_execution", public[0])
        self.assertEqual([summary], service.list()[0]["detail"]["image_execution"])


if __name__ == "__main__":
    unittest.main()
