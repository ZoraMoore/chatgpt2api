import base64
import copy
import json
import os
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "isolated-test-key")
from services.account_service import AccountService
from services import sub2api_service as sub


def token(iat, exp, email="fixture@example.test"):
    payload = {"iat": iat, "exp": exp, "email": email,
               "https://api.openai.com/auth": {"chatgpt_account_id": "account-id", "chatgpt_user_id": "user-id"}}
    return "fixture." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".signature"


def make_service(rows=()):
    storage = Mock()
    storage.load_accounts.return_value = copy.deepcopy(list(rows))
    with patch.object(AccountService, "_load_cumulative_total", return_value=len(rows)):
        service = AccountService(storage)
    service._save_cumulative_total = Mock()
    return service, storage


class FullSub2APIImportTests(unittest.TestCase):
    def exported(self):
        now = int(time.time())
        return {"id": 7, "name": "fixture@example.test", "platform": "openai", "type": "oauth",
                "group_ids": [2], "extra": {"region": "test"}, "credentials": {
                    "access_token": token(now, now + 3600), "refresh_token": "fixture-refresh",
                    "id_token": "fixture-id", "expires_at": str(now + 3600), "client_id": "fixture-client",
                    "plan_type": "plus", "account_id": "account-id", "custom_credential": "retained"}}

    def test_nested_export_preserves_credentials_and_metadata(self):
        source = self.exported()
        payload = AccountService._prepare_account_payload(source)
        self.assertIsNotNone(payload)
        for key, value in source["credentials"].items():
            self.assertEqual(value, payload[key])
        self.assertEqual(source["extra"], payload["sub2api"]["extra"])
        self.assertNotIn("credentials", payload["sub2api"])
        self.assertEqual("codex", payload["source_type"])
        self.assertEqual("plus", payload["type"])

    def test_remote_credentials_cannot_enable_local_cleanup(self):
        source = self.exported()
        source["credentials"].update(image_cleanup_enabled=True, management_id="attacker-controlled")
        payload = AccountService._prepare_account_payload(source)
        self.assertNotIn("image_cleanup_enabled", payload)
        self.assertNotIn("management_id", payload)
        self.assertTrue(payload["sub2api"]["credential_local_fields"]["image_cleanup_enabled"])

    def test_oauth_uses_imported_client_id_and_retains_rotated_fields(self):
        service, _ = make_service()
        response = Mock(status_code=200, text="not empty")
        response.json.return_value = {"access_token":"fixture-new", "refresh_token":"fixture-new-refresh", "id_token":"fixture-new-id", "expires_in":3600}
        session = Mock();session.post.return_value = response
        with patch('curl_cffi.requests.Session', return_value=session):
            result = service._request_access_token_refresh('fixture-old-refresh', {'client_id':'source-client'})
        self.assertEqual('source-client', session.post.call_args.kwargs['data']['client_id'])
        self.assertEqual('fixture-new-refresh', result['refresh_token'])
        self.assertEqual(3600, result['expires_in'])
        session.close.assert_called_once()

    def test_settings_import_passes_full_objects_to_account_service(self):
        source = self.exported()
        with patch.object(sub.sub2api_session_manager, "export_accounts", return_value={"accounts": [source]}):
            accounts, errors = sub._fetch_accounts_for_import({"id": "test"}, ["7"])
        self.assertEqual([], errors)
        self.assertEqual("fixture-refresh", accounts[0]["refresh_token"])
        store = Mock()
        store.get_import_job.return_value = {"total": 1, "errors": []}
        with patch.object(sub, "account_service") as service:
            service.add_account_items.return_value = {"added": 1}
            service.refresh_accounts.return_value = {"refreshed": 1}
            sub.Sub2APIImportService(store)._run_import("test", ["7"], accounts, [])
        service.add_account_items.assert_called_once_with(accounts)
        service.add_accounts.assert_not_called()

    def test_same_identity_reimport_rotates_existing_record_and_preserves_settings(self):
        now = int(time.time());old = token(now-100, now+100);new = token(now, now+3600)
        service, storage = make_service([{"access_token": old, "refresh_token": "old-refresh", "email": "fixture@example.test",
                                         "source_type": "codex", "proxy": "http://proxy.test:80", "success": 5,
                                         "image_cleanup_enabled": True, "auto_refresh_token": False}])
        result = service.add_account_items([{**self.exported(), "credentials": {
            "access_token": new, "refresh_token": "new-refresh", "email": "fixture@example.test"}}])
        self.assertEqual(0, result["added"])
        self.assertEqual(1, len(service.list_accounts()))
        self.assertEqual(new, service.resolve_access_token(old))
        item = service.get_account(new)
        self.assertTrue(item["image_cleanup_enabled"])
        self.assertFalse(item["auto_refresh_token"])
        self.assertEqual("http://proxy.test:80", item["proxy"])
        self.assertEqual(5, item["success"])
        self.assertEqual("new-refresh", item["refresh_token"])

    def test_older_export_does_not_replace_newer_credentials(self):
        now = int(time.time());old = token(now-100,now+10);new = token(now,now+3600)
        service, _ = make_service([{"access_token": new, "refresh_token": "new-refresh", "source_type": "codex"}])
        service.add_account_items([{"access_token": old, "refresh_token": "old-refresh", "source_type": "codex"}])
        self.assertEqual(1, len(service.list_accounts()))
        self.assertEqual("new-refresh", service.get_account(new)["refresh_token"])

    def test_imported_expiry_drives_refresh_for_opaque_token_and_opt_out(self):
        service, _ = make_service([{"access_token": "opaque", "refresh_token": "fixture-refresh",
                                  "expires_at": int(time.time()) + 60}])
        self.assertEqual(["opaque"], service.list_expiring_access_tokens())
        service._accounts["opaque"]["auto_refresh_token"] = False
        self.assertEqual([], service.list_expiring_access_tokens())
        with patch.object(service, "_request_access_token_refresh", return_value={"access_token": "new-opaque",
                         "refresh_token": "rotated-refresh", "expires_in": 86400}):
            self.assertEqual("new-opaque", service.refresh_access_token("opaque", force=True))
        item = service.get_account("new-opaque")
        self.assertEqual("rotated-refresh", item["refresh_token"])
        self.assertGreater(float(item["expires_at"]), time.time() + 86000)
