import os
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "isolated-test-key")
from test_sub2api_full_import import make_service, token
from services.account_operations import AccountOperations


class AccountOperationsTests(unittest.TestCase):
    def manager(self):
        now = int(time.time())
        accounts, storage = make_service([{"access_token": token(now,now+3600), "refresh_token": "secret-refresh",
                                          "source_type": "codex", "email": "fixture@example.test", "quota": 10}])
        return AccountOperations(accounts), accounts, storage

    def test_preferences_survive_rotation_and_resolve_without_secrets(self):
        manager, accounts, storage = self.manager()
        before = manager.list()[0];ref = before["id"]
        self.assertTrue(before["auto_refresh_token"])
        self.assertFalse(before["image_cleanup_enabled"])
        manager.preferences([ref], image_cleanup_enabled=True, auto_refresh_token=False)
        old = accounts.list_tokens()[0]
        accounts._apply_refreshed_tokens(old, {"access_token": token(int(time.time())+1,int(time.time())+86400),
                                              "refresh_token": "rotated-secret"}, "test")
        after = manager.list()[0]
        self.assertEqual(ref, after["id"])
        self.assertTrue(after["image_cleanup_enabled"])
        self.assertFalse(after["auto_refresh_token"])
        self.assertTrue(after["expires_at"])
        self.assertNotIn("secret", str(after))

    def test_manual_refresh_forces_oauth_and_returns_only_safe_status(self):
        manager, accounts, _ = self.manager();ref = manager.list()[0]["id"]
        with patch.object(accounts, "_request_access_token_refresh", return_value={"access_token": token(int(time.time())+1,int(time.time())+86400),
                         "refresh_token": "rotated-secret"}) as refresh:
            result = manager.refresh([ref])
        refresh.assert_called_once()
        self.assertEqual("success", result[0]["status"])
        self.assertNotIn("secret", str(result))
        self.assertNotIn("access_token", result[0])

    def test_invalid_refs_fail_before_any_preference_changes(self):
        manager, accounts, storage = self.manager();ref=manager.list()[0]["id"]
        with self.assertRaises(ValueError):
            manager.preferences([ref, "a"*64], image_cleanup_enabled=True)
        storage.save_accounts.assert_not_called()
        self.assertFalse(manager.list()[0]["image_cleanup_enabled"])

    def test_missing_refresh_token_and_disabled_accounts_are_explicit(self):
        manager, accounts, _ = self.manager();row=accounts._accounts[accounts.list_tokens()[0]]
        row["refresh_token"] = ""
        result = manager.refresh([manager.list()[0]["id"]])
        self.assertEqual("missing_refresh_token",result[0]["code"])

    def test_all_operation_routes_require_admin(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from api.account_operations import create_router
        app=FastAPI();app.include_router(create_router())
        with TestClient(app) as client:
            self.assertEqual(401,client.get('/api/accounts/operations').status_code)
            for path,body in [('preferences',{'account_ids':['a'*64],'image_cleanup_enabled':True}),
                              ('refresh',{'account_ids':['a'*64]})]:
                self.assertEqual(401,client.post('/api/accounts/operations/'+path,json=body).status_code)
