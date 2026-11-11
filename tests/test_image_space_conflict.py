from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.image_space_service import ImageSpaceError, clear_snapshot


def item(key):
    return {"library_file_id": key, "file_id": "file-" + key,
            "parent_directory_id": "", "file_name": key + ".png"}


def result(key, status, success):
    return {"library_file_id": key, "status_code": status, "success": success}


class BatchConflictTests(unittest.TestCase):
    def test_overall_failure_with_only_conflicts_is_verified(self):
        client = Mock()
        client.post.return_value = {"success": False, "files": [result("a", 409, False)]}
        client.scan.return_value = {}
        progress = {}
        try:
            clear_snapshot(client, {"a": item("a")}, lambda **fields: progress.update(fields), sleep=lambda _: None)
        except ImageSpaceError as exc:
            self.fail(f"完整的 409 结果应交给复扫：{exc}")
        client.scan.assert_called_once()
        self.assertEqual(0, progress["accepted"])
        self.assertEqual(1, progress["verified"])

    def test_24_successes_and_one_conflict_do_not_stop_later_batches(self):
        client = Mock()
        responses = [{"success": False, "files": [result("0", 409, False)] +
                     [result(str(i), 200, True) for i in range(1, 25)]},
                     {"success": True}, {"success": True}]
        client.post.side_effect = responses
        client.scan.return_value = {}
        progress = {}
        try:
            clear_snapshot(client, {str(i): item(str(i)) for i in range(51)},
                           lambda **fields: progress.update(fields), sleep=lambda _: None)
        except ImageSpaceError as exc:
            self.fail(f"409 不应中止其他正常图片的批次：{exc}")
        self.assertEqual([25, 25, 1], [len(c.args[1]["files"]) for c in client.post.call_args_list])
        self.assertEqual(50, progress["accepted"])
        self.assertEqual(51, progress["verified"])

    def test_persistent_conflicts_have_bounded_retries_and_are_never_verified(self):
        client = Mock()
        client.post.return_value = {"success": False, "files": [result("a", 409, False)]}
        client.scan.return_value = {"a": item("a")}
        progress = {}
        with self.assertRaises(ImageSpaceError):
            clear_snapshot(client, {"a": item("a")}, lambda **fields: progress.update(fields), sleep=lambda _: None)
        self.assertEqual(4, client.post.call_count)
        self.assertEqual(9, client.scan.call_count)
        self.assertEqual(0, progress["accepted"])
        self.assertEqual(0, progress["verified"])
        self.assertEqual(1, progress["remaining"])

    def test_conflict_retries_never_include_images_outside_snapshot(self):
        client = Mock()
        client.post.side_effect = [{"success": False, "files": [result("a", 409, False)]},
                                   {"success": True, "files": [result("a", 200, True)]}]
        client.scan.side_effect = [{"a": item("a"), "new": item("new")},
                                   {"a": item("a"), "new": item("new")}, {"new": item("new")}]
        progress = {}
        try:
            clear_snapshot(client, {"a": item("a")}, lambda **fields: progress.update(fields), sleep=lambda _: None)
        except ImageSpaceError as exc:
            self.fail(f"409 应在原快照内有限重试：{exc}")
        self.assertEqual(2, client.post.call_count)
        self.assertTrue(all(c.args[1]["files"] == [item("a")] for c in client.post.call_args_list))
        self.assertEqual(1, progress["verified"])

    def test_mixed_permission_failure_stops_before_later_batches(self):
        client = Mock()
        client.post.return_value = {"success": False, "files": [result("0", 409, False),
            result("1", 403, False)] + [result(str(i), 200, True) for i in range(2, 25)]}
        with self.assertRaisesRegex(ImageSpaceError, "403"):
            clear_snapshot(client, {str(i): item(str(i)) for i in range(51)}, lambda **_: None, sleep=lambda _: None)
        client.post.assert_called_once()
        client.scan.assert_not_called()

    def test_ambiguous_or_other_failures_are_not_hidden_by_conflicts(self):
        bad_rows = [None, [], "wrong", [result("a", 409, False)],
                    [result("a", 409, False), result("a", 200, True)],
                    [result("a", 409, False), result("foreign", 200, True)],
                    [result("a", 409, False), "wrong"],
                    [result("a", 409, False), {"library_file_id": "b", "success": True}],
                    [result("a", 409, False), result("b", "200", True)],
                    [result("a", 409, False), result("b", True, True)],
                    [result("a", 409, False), result("b", 200, "false")],
                    [result("a", 409, 1), result("b", 200, True)],
                    [result("a", 409, False), result("b", 200, False)],
                    [result("a", 409, False), result("b", 403, False)],
                    [result("a", 409, False), result("b", 500, False)],
                    [result("a", 200, True), result("b", 200, True)],
                    [result("a", 409, True), result("b", 200, True)],
                    [result("a", 409, False), result(["b"], 200, True)]]
        for rows in bad_rows:
            with self.subTest(rows=rows):
                client = Mock()
                client.post.return_value = {"success": False, "files": rows}
                client.scan.return_value = {}
                with self.assertRaises(ImageSpaceError):
                    clear_snapshot(client, {"a": item("a"), "b": item("b")}, lambda **_: None, sleep=lambda _: None)
                client.post.assert_called_once()
                client.scan.assert_not_called()

    def test_conflict_cannot_count_as_accepted_even_with_success_flag(self):
        client = Mock()
        client.post.return_value = {"files": [result("a", 409, True)]}
        client.scan.return_value = {}
        progress = {}
        clear_snapshot(client, {"a": item("a")}, lambda **fields: progress.update(fields), sleep=lambda _: None)
        self.assertEqual(0, progress["accepted"])
        self.assertEqual(1, progress["verified"])


if __name__ == "__main__":
    unittest.main()
