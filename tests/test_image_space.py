from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from api import image_space
from services.image_space_service import (
    DELETE_PATH, ImageSpaceClient, ImageSpaceError, ImageSpaceService, account_ref, clear_snapshot,
)


def row(key):
    return {"id": key, "file_id": "file-" + key, "file_name": key + ".png"}


def item(key):
    return {"library_file_id": key, "file_id": "file-" + key,
            "parent_directory_id": "", "file_name": key + ".png"}


class ScanTests(unittest.TestCase):
    def client(self, pages):
        client = object.__new__(ImageSpaceClient)
        client.post = Mock(side_effect=pages)
        return client

    def test_empty_page_continues_and_duplicates_are_deduplicated(self):
        client = self.client([{"items": [], "cursor": "next"},
                              {"items": [row("a"), row("a")], "cursor": None}])
        self.assertEqual(["a"], list(client.scan()))
        self.assertEqual("next", client.post.call_args_list[1].args[1]["cursor"])
        self.assertEqual(["images"], client.post.call_args.args[1]["categories"])
        self.assertFalse(client.post.call_args.args[1]["trashed_only"])

    def test_invalid_or_incomplete_list_never_becomes_empty_success(self):
        for pages in ([{}], [{"items": [row("bad/id")]}], [{"items": "wrong"}],
                      [{"items": [], "cursor": "a"}, {"items": [], "cursor": "b"},
                       {"items": [], "cursor": "a"}],
                      [{"items": [], "cursor": i} for i in range(1, 2)]):
            with self.subTest(pages=pages), self.assertRaises(ImageSpaceError):
                self.client(pages).scan()

    def test_page_limit_fails_closed(self):
        with self.assertRaises(ImageSpaceError):
            self.client([{"items": [], "cursor": str(i)} for i in range(100)]).scan()

    def test_trashed_items_are_excluded(self):
        self.assertEqual({}, self.client([{"items": [{**row("a"), "trashed_at": "date"}]}]).scan())


class DeleteTests(unittest.TestCase):
    def test_delayed_index_and_conflicts_are_verified_without_deleting_new_images(self):
        snapshot = {"a": item("a")}
        client = Mock()
        client.post.side_effect = [{}, {"files": [{"library_file_id": "a", "status_code": 200}]}]
        client.scan.side_effect = [{"a": item("a"), "new": item("new")}, {"new": item("new")}]
        progress = {}
        clear_snapshot(client, snapshot, lambda **fields: progress.update(fields), sleep=lambda _: None)
        self.assertEqual(1, progress["verified"])
        self.assertEqual(0, progress["remaining"])
        self.assertEqual(1, progress["accepted"])
        self.assertEqual(2, client.post.call_count)
        for call in client.post.call_args_list:
            self.assertEqual([item("a")], call.args[1]["files"])

    def test_http_success_never_counts_as_verified(self):
        client = Mock()
        client.post.return_value = {"success": True}
        client.scan.return_value = {"a": item("a")}
        progress = {}
        with self.assertRaises(ImageSpaceError):
            clear_snapshot(client, {"a": item("a")}, lambda **f: progress.update(f), sleep=lambda _: None)
        self.assertEqual(4, client.post.call_count)
        self.assertEqual(8, client.scan.call_count)
        self.assertEqual(0, progress["verified"])
        self.assertEqual(1, progress["accepted"])

    def test_batch_size_and_final_scan(self):
        client = Mock()
        client.post.return_value = {}
        client.scan.return_value = {}
        clear_snapshot(client, {str(i): item(str(i)) for i in range(51)}, lambda **_: None, sleep=lambda _: None)
        self.assertEqual([25, 25, 1], [len(c.args[1]["files"]) for c in client.post.call_args_list])
        client.scan.assert_called_once()

    def test_incomplete_verification_is_error(self):
        client = Mock()
        client.post.return_value = {"success": True}
        client.scan.side_effect = ImageSpaceError("图片列表格式异常")
        progress = {}
        with self.assertRaises(ImageSpaceError):
            clear_snapshot(client, {"a": item("a")}, lambda **f: progress.update(f), sleep=lambda _: None)
        self.assertNotIn("verified", progress)


class RequestTests(unittest.TestCase):
    def client(self, statuses):
        client = object.__new__(ImageSpaceClient)
        client.deadline = time.monotonic() + 100
        client.headers = {}
        client.session = Mock()
        client.session.post.side_effect = [Mock(status_code=s, json=Mock(return_value={"items": []})) for s in statuses]
        return client

    def test_auth_failure_stops_without_retry_or_raw_body(self):
        for status in (401, 403):
            client = self.client([status])
            with self.assertRaisesRegex(ImageSpaceError, str(status)):
                client.post(DELETE_PATH, {})
            self.assertEqual(1, client.session.post.call_count)

    def test_conflict_can_be_rescanned_and_rate_limit_retried(self):
        self.assertEqual({}, self.client([409]).post(DELETE_PATH, {}))
        client = self.client([429, 200])
        with patch("services.image_space_service.time.sleep"):
            self.assertEqual({"items": []}, client.post(DELETE_PATH, {}))
        self.assertEqual(2, client.session.post.call_count)
        self.assertFalse(client.session.post.call_args.kwargs["allow_redirects"])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.account = {"access_token": "private-token", "email": "test@example.test", "source_type": "web"}
        self.accounts = Mock()
        self.accounts.list_accounts.return_value = [self.account]
        self.accounts.get_account.return_value = self.account
        self.client = Mock()
        self.client.scan.return_value = {"a": item("a")}
        self.service = ImageSpaceService(self.accounts, Mock(return_value=self.client))

    def wait_done(self):
        deadline = time.monotonic() + 3
        while self.service.active and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertFalse(self.service.active)

    def test_preview_no_delete_then_execute_only_once(self):
        job_id = self.service.preview([account_ref(self.account)])
        self.wait_done()
        job = self.service.get(job_id)
        self.assertEqual("ready", job["status"])
        self.assertEqual(1, job["accounts"][0]["total"])
        self.client.post.assert_not_called()
        self.assertNotIn("private-token", str(job) + str(self.service.choices()))
        with patch("services.image_space_service.clear_snapshot") as clear:
            self.service.execute(job_id)
            self.wait_done()
            clear.assert_called_once()
        self.assertEqual("completed", self.service.get(job_id)["status"])
        with self.assertRaises(ImageSpaceError):
            self.service.execute(job_id)

    def test_bad_preview_not_executable_and_exception_is_redacted(self):
        self.client.scan.side_effect = RuntimeError("private-token proxy-password")
        job_id = self.service.preview([account_ref(self.account)])
        self.wait_done()
        job = self.service.get(job_id)
        self.assertEqual("failed", job["status"])
        self.assertNotIn("private-token", str(job))
        with self.assertRaises(ImageSpaceError):
            self.service.execute(job_id)

    def test_expiration_busy_and_adobe_exclusion(self):
        self.service.active = True
        with self.assertRaises(ImageSpaceError):
            self.service.preview([account_ref(self.account)])
        self.service.active = False
        job_id = self.service.preview([account_ref(self.account)])
        self.wait_done()
        self.service.jobs[job_id]["updated_at"] -= 1801
        with self.assertRaises(KeyError):
            self.service.execute(job_id)
        self.assertNotIn(job_id, self.service.snapshots)
        self.account["source_type"] = "adobe"
        self.assertEqual([], self.service.choices())
        with self.assertRaises(ImageSpaceError):
            self.service.preview([account_ref(self.account)])


class ApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(image_space.create_router())
        self.client = TestClient(app)
        self.auth = {"Authorization": "Bearer test-admin-key"}
        self.job = "a" * 32

    def test_all_api_routes_require_admin(self):
        for method, path, body in [("GET", "accounts", None), ("POST", "preview", {"account_ids": ["a" * 64]}),
                                   ("GET", "jobs/"+self.job, None),
                                   ("POST", "jobs/"+self.job+"/execute", {"confirm": True})]:
            with self.subTest(path=path):
                response = self.client.request(method, "/api/accounts/image-space/"+path, json=body)
                self.assertEqual(401, response.status_code)
                with patch("api.support.auth_service.authenticate", return_value={"role": "user"}):
                    response = self.client.request(method, "/api/accounts/image-space/"+path, json=body,
                                                   headers={"Authorization": "Bearer user-key"})
                    self.assertEqual(403, response.status_code)

    def test_validation_and_explicit_confirmation(self):
        for refs in ([], ["bad"], ["a" * 64] * 1001):
            response = self.client.post("/api/accounts/image-space/preview", json={"account_ids": refs}, headers=self.auth)
            self.assertEqual(422, response.status_code)
        with patch.object(image_space.image_space_service, "execute") as execute:
            for body in ({}, {"confirm": False}):
                response = self.client.post("/api/accounts/image-space/jobs/"+self.job+"/execute", json=body, headers=self.auth)
                self.assertEqual(422, response.status_code)
            execute.assert_not_called()
            response = self.client.post("/api/accounts/image-space/jobs/"+self.job+"/execute", json={"confirm": True}, headers=self.auth)
            self.assertEqual(202, response.status_code)
            execute.assert_called_once_with(self.job)

    def test_missing_task_and_static_assets(self):
        response = self.client.get("/api/accounts/image-space/jobs/"+self.job, headers=self.auth)
        self.assertEqual(404, response.status_code)
        response = self.client.get("/accounts/image-space", follow_redirects=False)
        self.assertEqual(307, response.status_code)
        self.assertEqual("/accounts", response.headers["location"])
        self.assertEqual(200, self.client.get("/account-image-space.js").status_code)


if __name__ == "__main__":
    unittest.main()
