"""预检后的优先级必须重新比较，但已提交的生成不得因排名变化重放。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from services.account_service import config
from services.image_submission import guard_image_submission
from services.protocol import conversation
from test_sub2api_full_import import make_service
from image_quota_fixtures import seed_known_local_balance


def account(token, *, priority=None, quota=5):
    row = {"access_token": token, "type": "self_serve_business_prolite", "quota": quota}
    if priority is not None:
        row.update(image_priority=priority, image_priority_mode="manual")
    return row


class ImagePriorityRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("services.account_service.browser_sentinel_enabled", return_value=False))
        self.enterContext(patch.object(type(config), "image_account_concurrency", new_callable=PropertyMock, return_value=3))
        self.enterContext(patch.object(type(config), "auto_remove_rate_limited_accounts", new_callable=PropertyMock, return_value=False))

    def service(self, rows):
        service, _ = make_service(rows)
        self.addCleanup(service._image_ledger.close)
        for row in rows:
            seed_known_local_balance(service, row['access_token'], row['quota'])
        return service

    def assert_only_selected_has_reservation(self, service, selected, generation):
        rows = {row["access_token"]: row for row in service.list_accounts()}
        for token, row in rows.items():
            self.assertEqual(int(token == selected), row["image_inflight"], token)
            self.assertEqual(int(token == selected), row["image_quota_reserved"], token)
            self.assertEqual(0, row["image_quota_uncertain"], token)
            self.assertEqual(5 - int(token == selected), row["image_quota_available"], token)
        service.release_image_slot(selected, generation_id=generation)
        for row in service.list_accounts():
            self.assertEqual(0, row["image_inflight"])
            self.assertEqual(0, row["image_quota_reserved"])
            self.assertEqual(5, row["image_quota_available"])

    def test_remote_plan_downgrade_selects_another_available_prolite(self):
        service = self.service([account("fixture-first"), account("fixture-second")])

        def refresh(token, *_args, **_kwargs):
            if token == "fixture-first":
                return service.update_account(token, {"type": "free", "quota": 5}, quiet=True, remote=True)
            return service.get_account(token)

        with patch.object(service, "fetch_remote_info", side_effect=refresh) as fetch:
            selected = service.get_available_access_token(generation_id="fixture-generation")
        self.assertEqual("fixture-second", selected)
        self.assertEqual(["fixture-first", "fixture-second"], [call.args[0] for call in fetch.call_args_list])
        self.assertEqual(2, service.get_account("fixture-first")["image_priority"])
        self.assert_only_selected_has_reservation(service, selected, "fixture-generation")

    def test_manual_priority_downgrade_during_preflight_reselects_without_leaking_slots(self):
        service = self.service([account("fixture-first"), account("fixture-second")])

        def refresh(token, *_args, **_kwargs):
            if token == "fixture-first":
                service.update_account(token, {"image_priority": 9}, quiet=True)
            return service.get_account(token)

        with patch.object(service, "fetch_remote_info", side_effect=refresh):
            selected = service.get_available_access_token(generation_id="fixture-generation")
        self.assertEqual("fixture-second", selected)
        self.assertEqual("manual", service.get_account("fixture-first")["image_priority_mode"])
        self.assert_only_selected_has_reservation(service, selected, "fixture-generation")

    def test_another_account_promoted_during_preflight_is_preferred(self):
        service = self.service([
            account("fixture-first", priority=1), account("fixture-second", priority=5),
        ])

        def refresh(token, *_args, **_kwargs):
            if token == "fixture-first":
                service.update_account("fixture-second", {"image_priority": 0}, quiet=True)
            return service.get_account(token)

        with patch.object(service, "fetch_remote_info", side_effect=refresh):
            selected = service.get_available_access_token(generation_id="fixture-generation")
        self.assertEqual("fixture-second", selected)
        self.assert_only_selected_has_reservation(service, selected, "fixture-generation")

    def test_last_reserved_unit_can_continue_when_higher_priority_account_is_busy(self):
        service = self.service([account("fixture-first", quota=1), account("fixture-busy")])
        with service._lock:
            service._image_inflight["fixture-busy"] = 3

        def refresh(token, *_args, **_kwargs):
            return service.update_account(token, {"type": "free", "quota": 1}, quiet=True, remote=True)

        with patch.object(service, "fetch_remote_info", side_effect=refresh) as fetch:
            selected = service.get_available_access_token(generation_id="fixture-generation")
        self.assertEqual("fixture-first", selected)
        fetch.assert_called_once()
        row = service.get_account(selected)
        self.assertEqual(1, row["image_quota_reserved"])
        self.assertEqual(0, row["image_quota_available"])
        service.assert_image_submission_allowed(selected, "fixture-generation")
        service.mark_image_result(selected, True, generation_id="fixture-generation")
        self.assertEqual(0, service.get_account(selected)["image_quota_available"])

    def test_post_submit_downgrade_does_not_replay_generation_on_another_account(self):
        service = self.service([account("fixture-first"), account("fixture-second")])
        backend = SimpleNamespace(access_token="fixture-first", close=Mock())

        def stream(active_backend, *_args):
            guard_image_submission(active_backend)
            service.update_account("fixture-first", {"type": "free"}, quiet=True, remote=True)
            raise RuntimeError("token invalidated after generation submission")

        request = conversation.ConversationRequest(
            model="gpt-image-2", prompt="fixture", image_transport="conversation",
        )
        with patch.object(service, "fetch_remote_info", side_effect=lambda token, *_a, **_k: service.get_account(token)), \
             patch.object(service, "refresh_access_token") as refresh, \
             patch.object(conversation, "account_service", service), \
             patch.object(conversation, "OpenAIBackendAPI", return_value=backend) as create_backend, \
             patch.object(conversation, "delivery_context", return_value={}), \
             patch.object(conversation, "stream_image_outputs", side_effect=stream) as generate:
            with self.assertRaises(conversation.ImageGenerationError):
                conversation._generate_single_image_impl(request, 1, 1)
        generate.assert_called_once()
        create_backend.assert_called_once()
        refresh.assert_not_called()
        backend.close.assert_called_once()
        rows = {row["access_token"]: row for row in service.list_accounts()}
        self.assertEqual(0, rows["fixture-first"]["image_inflight"])
        self.assertEqual(1, rows["fixture-first"]["image_quota_uncertain"])
        self.assertEqual(0, rows["fixture-second"]["image_inflight"])
        self.assertEqual(0, rows["fixture-second"]["image_quota_reserved"])


if __name__ == "__main__":
    unittest.main()
