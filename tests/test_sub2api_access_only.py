import json
import tempfile
import time
import unittest
import asyncio
from pathlib import Path
from unittest.mock import Mock, patch

from test_sub2api_full_import import make_service, token
from services.account_service import AccountService
from services.sub2api_service import Sub2APIConfig
from api.support import sanitize_sub2api_server


class AccessOnlyTests(unittest.TestCase):
    def record(self, now=None):
        now = now or int(time.time())
        return {"access_token": token(now, now + 3600), "refresh_token": "never-store-rt", "id_token": "never-store-id",
                "password": "never-store-password", "source_type": "codex", "name": "源站名称",
                "sub2api": {"id": "7", "server_id": "fixture", "name": "源站名称", "extra": {"refresh_token": "nested-rt"}}}

    def test_existing_source_accounts_remove_refresh_credentials_on_load(self):
        service, storage = make_service([self.record()])
        row = service.list_accounts()[0]
        self.assertEqual("sub2api", row.get("refresh_owner"))
        self.assertFalse(row["auto_refresh_token"])
        for value in ["never-store-rt", "never-store-id", "never-store-password", "nested-rt"]:
            self.assertNotIn(value, json.dumps(row))
        storage.save_accounts.assert_called_once()

    def test_source_account_cannot_locally_refresh_even_when_forced(self):
        service, _ = make_service([self.record()])
        access = service.list_tokens()[0]
        with patch.object(service, "_request_access_token_refresh") as refresh:
            self.assertEqual(access, service.refresh_access_token(access, force=True))
            refresh.assert_not_called()

    def test_latest_source_token_updates_same_record_and_keeps_local_state(self):
        now = int(time.time())
        old = {**self.record(now - 60), "success": 9, "status": "禁用", "proxy": "http://fixture.test:8080"}
        service, _ = make_service([old])
        ref = service.list_accounts()[0]["management_id"]
        new = self.record(now)
        result = service.add_account_items([new])
        row = service.list_accounts()[0]
        self.assertEqual(0, result["added"])
        self.assertEqual(new["access_token"], row["access_token"])
        self.assertEqual((ref, 9, "禁用"), (row["management_id"], row["success"], row["status"]))
        self.assertNotIn("refresh_token", row)

    def test_config_encrypts_secret_and_api_never_returns_it(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Sub2APIConfig(Path(directory) / "sources.json")
            server = store.add_server(name="fixture", base_url="https://fixture.test", email="admin@fixture.test",
                                      password="fixture-password", api_key="", group_id="24")
            seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
            server = store.update_server(server["id"], {"totp_secret": seed, "auto_sync_enabled": True})
            self.assertNotIn(seed, (Path(directory) / "sources.json").read_text())
            public = sanitize_sub2api_server(server)
            self.assertTrue(public.get("has_totp_secret"))
            self.assertNotIn("totp_secret_encrypted", public)
            self.assertNotIn("totp_secret", public)
            self.assertEqual(0o600, (Path(directory) / "sources.json").stat().st_mode & 0o777)

    def test_validation_error_does_not_echo_totp_input(self):
        from fastapi import FastAPI
        from fastapi.exceptions import RequestValidationError
        from starlette.requests import Request
        from api.errors import install_exception_handlers
        app = FastAPI()
        install_exception_handlers(app)
        request = Request({"type": "http", "scheme": "https", "server": ("fixture.test", 443),
                           "headers": [], "path": "/api/sub2api/servers/fixture"})
        error = RequestValidationError([{"type": "string_too_long", "loc": ["body", "totp_secret"],
                                        "msg": "Input is too long", "input": "secret-never-echo"}])
        result = asyncio.run(app.exception_handlers[RequestValidationError](request, error))
        self.assertNotIn(b"secret-never-echo", result.body)

    def test_new_source_at_recovers_expired_account_without_deleting_binding(self):
        now = int(time.time())
        old = {**self.record(now - 60), "status": "异常", "last_invalid_at": "2026-10-23T00:00:00Z"}
        service, _ = make_service([old])
        service.add_account_items([self.record(now)])
        self.assertEqual("正常", service.list_accounts()[0]["status"])

    def test_source_binding_rejects_changed_identity_before_updates(self):
        service, storage = make_service([self.record()])
        changed = self.record(int(time.time()) + 1)
        changed["access_token"] = token(int(time.time()) + 1, int(time.time()) + 3600)
        # 用不透明 AT 和明确不同身份模拟源站 ID 被错误复用。
        changed.update(access_token="opaque-fixture", chatgpt_user_id="different-user", chatgpt_account_id="different-workspace")
        before = service.list_tokens()
        with self.assertRaises(ValueError):
            service.add_account_items([changed])
        self.assertEqual(before, service.list_tokens())

    def test_different_source_ids_do_not_overwrite_each_others_names(self):
        now = int(time.time())
        first = self.record(now - 1)
        second = self.record(now)
        second["sub2api"] = {"server_id": "fixture", "id": "8", "name": "另一条原站记录"}
        service, _ = make_service([first])
        service.add_account_items([second])
        self.assertEqual(2, len(service.list_accounts()))
        self.assertEqual({"源站名称", "另一条原站记录"}, {a["name"] for a in service.list_accounts()})

    def test_older_source_at_binds_existing_account_without_rolling_back_token(self):
        now = int(time.time())
        current = {"access_token": token(now, now + 7200), "source_type": "web", "success": 9,
                   "web_session_token": "existing-web-session-fixture"}
        service, _ = make_service([current])
        ref = service.list_accounts()[0]["management_id"]
        service.add_account_items([self.record(now - 60)])
        row = service.list_accounts()[0]
        self.assertEqual(current["access_token"], row["access_token"])
        self.assertEqual("7", row["sub2api"]["id"])
        self.assertEqual("sub2api", row["refresh_owner"])
        self.assertEqual((ref, 9), (row["management_id"], row["success"]))
        self.assertEqual(current["web_session_token"], row["web_session_token"])

    def test_identical_source_duplicates_keep_all_source_ids_without_duplicate_token(self):
        first = self.record()
        second = {**first, "sub2api": {**first["sub2api"], "id": "8"}}
        service, _ = make_service()
        service.add_account_items([first, second])
        rows = service.list_accounts()
        self.assertEqual(1, len(rows))
        self.assertEqual(["7", "8"], rows[0]["sub2api"]["alias_ids"])
