import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services import sub2api_service as sub
from services.sub2api_session_service import Sub2APISessionManager, Sub2APISessionError
from test_sub2api_full_import import make_service, token


def response(status, data=None):
    return Mock(status_code=status, ok=200 <= status < 300, json=Mock(return_value={"code": 0, "data": data}))


class AutomaticSyncTests(unittest.TestCase):
    def test_automatic_login_retries_once_with_next_totp_window(self):
        server = {"id": "fixture", "totp_secret_encrypted": "fixture-encrypted"}
        with patch.object(sub, "sub2api_config") as config, patch.object(sub, "sub2api_session_manager") as manager, \
             patch.object(sub, "list_remote_accounts", return_value=[]):
            config.totp.next_code.side_effect = ["111111", "222222"]
            manager.begin_login.return_value = {"status": "login_2fa_required"}
            manager.complete_login_2fa.side_effect = [Sub2APISessionError("LOGIN_2FA_INVALID", "reused", status_code=400), {}]
            self.assertEqual("authenticated", sub.sync_remote_accounts(server)["status"])
            self.assertEqual(2, manager.complete_login_2fa.call_count)
            self.assertEqual(["111111", "222222"], [call.args[1] for call in manager.complete_login_2fa.call_args_list])

    def test_scheduler_records_success_and_redacts_failures(self):
        class OneCycle:
            stopped = False
            def is_set(self): return self.stopped
            def wait(self, timeout): self.stopped = True
        server = {"id": "fixture", "auto_sync_enabled": True, "next_sync_at": 0, "sync_interval_minutes": 5}
        for failure in (False, True):
            config = Mock()
            config.list_servers.return_value = [server]
            with patch.object(sub, "sub2api_config", config), patch.object(sub, "sync_source_access_tokens") as sync:
                if failure:
                    sync.side_effect = RuntimeError("secret-never-log")
                else:
                    sync.return_value = {"count": 5, "added": 1, "failed": 0}
                thread = sub.start_sub2api_sync_scheduler(OneCycle())
                thread.join(2)
                self.assertFalse(thread.is_alive())
                sync.assert_called_once_with(server)
                fields = config.set_sync_state.call_args.kwargs
                self.assertNotIn("secret-never-log", str(fields))
                self.assertGreater(fields["next_sync_at"], time.time())
                self.assertEqual("SYNC_FAILED" if failure else "", fields["last_sync_error"])

    def test_login_stepup_export_and_rotation_store_only_latest_at(self):
        with tempfile.TemporaryDirectory() as directory:
            config = sub.Sub2APIConfig(Path(directory) / "sources.json")
            server = config.add_server(name="fixture", base_url="https://source.example.test", email="admin@example.test",
                password="fixture-password", api_key="", group_id="24", totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", auto_sync_enabled=True)
            accounts, storage = make_service()
            session = Mock()
            manager = Sub2APISessionManager(session_factory=lambda: session)
            now = int(time.time())
            source = {"id": 7, "name": "原站名字", "status": "active", "credentials": {
                "email": "fixture@example.test", "chatgpt_user_id": "user-id", "chatgpt_account_id": "account-id"}}
            with patch.object(sub, "sub2api_config", config), patch.object(sub, "account_service", accounts), \
                 patch.object(sub, "sub2api_session_manager", manager), \
                 patch.object(config.totp, "next_code", side_effect=["111111", "222222", "333333"]), \
                 patch.object(accounts, "_request_access_token_refresh") as local_refresh:
                first_ref = None
                for i in range(2):
                    session.post.side_effect = ([response(200, {"requires_2fa": True, "temp_token": "fixture-challenge"}),
                        response(200, {"access_token": "fixture-admin-token"})] if i == 0 else []) + [response(200, {"verified": True})]
                    exported = {"name": "原站名字", "platform": "openai", "type": "oauth", "credentials": {
                        "access_token": token(now + i, now + 3600), "refresh_token": "must-never-save", "id_token": "must-never-save-id"}}
                    session.get.side_effect = [response(404), response(200, {"items": [source], "total": 1}),
                                              response(404), response(200, {"providers": [exported]})]
                    result = sub.sync_source_access_tokens(server)
                    self.assertEqual(1, result["count"])
                    self.assertEqual(1 - i, result["added"])
                    row = accounts.list_accounts()[0]
                    if first_ref is None:
                        first_ref = row["management_id"]
                        accounts.update_account(row["access_token"], {"success": 5}, quiet=True)
                    self.assertEqual(first_ref, row["management_id"])
                    self.assertEqual("原站名字", row["name"])
                    self.assertEqual("7", row["sub2api"]["id"])
                    self.assertEqual(server["id"], row["sub2api"]["server_id"])
                    self.assertNotIn("must-never-save", json.dumps(storage.save_accounts.call_args.args))
                self.assertEqual(5, accounts.list_accounts()[0]["success"])
                local_refresh.assert_not_called()
                codes = [call.kwargs["json"].get("totp_code") or call.kwargs["json"].get("code")
                         for call in session.post.call_args_list if "login" not in call.args[0] or call.args[0].endswith("/2fa")]
                self.assertEqual(["111111", "222222", "333333"], codes)
                self.assertEqual(1, sum(call.args[0].endswith('/auth/login') for call in session.post.call_args_list))

    def test_export_without_id_never_guesses_by_position(self):
        exported = {"name": "重复名称", "platform": "openai", "credentials": {"access_token": token(int(time.time()), int(time.time()) + 3600)}}
        sources = [{"id": "1", "name": "重复名称"}, {"id": "2", "name": "重复名称"}]
        with patch.object(sub.sub2api_session_manager, "export_accounts", return_value={"accounts": [exported]}) as export:
            rows, errors = sub._fetch_accounts_for_import({"id": "fixture"}, ["1", "2"], sources)
        self.assertEqual(["1", "2"], [a["sub2api"]["id"] for a in rows])
        self.assertEqual([], errors)
        self.assertEqual([["1", "2"], ["1"], ["2"]], [call.args[1] for call in export.call_args_list])
