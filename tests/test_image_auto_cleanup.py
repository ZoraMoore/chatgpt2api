import os
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "isolated-test-key")
from test_sub2api_full_import import make_service, token
from services.image_space_service import ImageSpaceService, image_created_at


class AutoCleanupTests(unittest.TestCase):
    def test_real_library_creation_fields_are_understood_without_using_modified_time(self):
        self.assertEqual(1791450000, image_created_at({'record_creation_time':'2026-10-08T09:00:00Z'}))
        self.assertEqual(1791450000, image_created_at({'file_upload_time':'2026-10-08T17:00:00+08:00'}))
        self.assertIsNone(image_created_at({'updated_at':'2026-10-08T09:00:00Z'}))

    def setup_service(self):
        now = 1792130000
        accounts,_=make_service([{"access_token": token(now,now+86400), "email":"fixture@example.test", "quota":10,
                                 "image_cleanup_enabled":True, "source_type":"codex"}])
        client=Mock()
        client.scan.return_value={"old":{"file_id":"old","created_at":now-8*86400},
                                  "recent":{"file_id":"recent","created_at":now-6*86400},
                                  "unknown":{"file_id":"unknown"}}
        return ImageSpaceService(accounts,Mock(return_value=client)),accounts,client,now

    def test_only_older_than_seven_days_is_cleared_and_state_survives_restart(self):
        service,accounts,client,now=self.setup_service()
        with patch('services.image_space_service.clear_snapshot') as clear:
            service.run_scheduled(now)
            self.assertEqual({'old'},set(clear.call_args.args[1]))
            ImageSpaceService(accounts,Mock(return_value=client)).run_scheduled(now+60)
            clear.assert_called_once()
        row=accounts.list_accounts()[0]
        self.assertEqual(now+86400,row['image_cleanup_next_at'])
        self.assertEqual('success',row['image_cleanup_last_status'])

    def test_disabled_busy_and_unknown_dates_are_not_deleted(self):
        for case in ('disabled','busy','unknown'):
            with self.subTest(case=case):
                service,accounts,client,now=self.setup_service();key=accounts.list_tokens()[0]
                if case=='disabled':accounts._accounts[key]['image_cleanup_enabled']=False
                if case=='busy':accounts._image_inflight[key]=1
                if case=='unknown':client.scan.return_value={'unknown':{'file_id':'unknown'}}
                with patch('services.image_space_service.clear_snapshot') as clear:
                    service.run_scheduled(now)
                clear.assert_not_called()

    def test_failure_waits_until_next_day_without_leaking_credentials(self):
        service,accounts,client,now=self.setup_service();client.scan.side_effect=RuntimeError('secret-refresh')
        service.run_scheduled(now)
        service.run_scheduled(now+60)
        client.scan.assert_called_once()
        row=accounts.list_accounts()[0]
        self.assertEqual('error',row['image_cleanup_last_status'])
        self.assertNotIn('secret-refresh',str(row.get('image_cleanup_last_error')))

    def test_disabling_account_while_scanning_stops_before_delete(self):
        service,accounts,client,now=self.setup_service()
        snapshot=client.scan.return_value
        def scan():
            accounts._accounts[accounts.list_tokens()[0]]['image_cleanup_enabled']=False
            return snapshot
        client.scan.side_effect=scan
        with patch('services.image_space_service.clear_snapshot') as clear:
            service.run_scheduled(now)
        clear.assert_not_called()
