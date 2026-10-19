import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "isolated-test-key")
from services.log_service import LogService


class BeijingLogTests(unittest.TestCase):
    def test_history_and_explicit_offsets_are_filtered_by_beijing_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs.jsonl"
            rows = [{"time": value, "type": "call", "summary": str(i)} for i, value in enumerate([
                "2026-10-15 16:01:02", "2026-10-16T00:02:03+08:00", "2026-10-15T15:59:59Z"])]
            original = "\n".join(json.dumps(row) for row in rows) + "\n"
            path.write_text(original)
            service = LogService(path)
            result = service.list(start_date="2026-10-16", end_date="2026-10-16")
            self.assertEqual(["2026-10-16 00:02:03", "2026-10-16 00:01:02"], [r["time"] for r in result])
            self.assertTrue(all(r["timezone"] == "Asia/Shanghai" for r in result))
            self.assertEqual(original, path.read_text())
            self.assertEqual(1, service.delete([result[-1]["id"]])["removed"])
            self.assertEqual(1, len(service.list(start_date="2026-10-16")))

    def test_new_logs_store_timezone_aware_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs.jsonl"
            service = LogService(path)
            service.add("account", "test")
            stored = datetime.fromisoformat(json.loads(path.read_text())["time"])
            self.assertIsNotNone(stored.tzinfo)
            self.assertLess(abs((datetime.now(timezone.utc) - stored).total_seconds()), 3)
