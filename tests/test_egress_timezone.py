from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "isolated-timezone-test-key")

from services.egress_timezone import EgressTimezone, EgressTimezoneCache, parse_provider_timezone, valid_timezone
from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI
from utils.pow import _legacy_parse_time, build_legacy_requirements_token, build_proof_token


class TimezoneTests(unittest.TestCase):
    def cache(self, path=None):
        if path is None:
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            path = Path(directory.name) / "cache.json"
        cache = EgressTimezoneCache(path)
        self.addCleanup(cache._probe_pool.shutdown)
        self.addCleanup(cache._refresh_pool.shutdown)
        return cache

    def test_offsets_match_browser_sign_and_daylight_saving(self):
        winter = datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp()
        summer = datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp()
        for zone, expected in [("America/New_York", (300, 240)), ("Asia/Shanghai", (-480, -480)),
                               ("Asia/Kathmandu", (-345, -345)), ("Australia/Adelaide", (-630, -570))]:
            with self.subTest(zone=zone):
                self.assertEqual(expected, tuple(EgressTimezone(zone).fields(t)["timezone_offset_min"] for t in (winter, summer)))

    def test_pow_date_matches_selected_timezone_for_both_tokens(self):
        instant = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
        with patch("utils.pow.datetime") as clock:
            clock.now.side_effect = lambda zone: instant.astimezone(zone)
            expected = "Wed Jul 15 2026 08:00:00 GMT-0400 (EDT)"
            self.assertEqual(expected, _legacy_parse_time("America/New_York"))
            requirements = build_legacy_requirements_token("test-agent", timezone_name="America/New_York")
            proof = build_proof_token("test-seed", "ffffffff", "test-agent", timezone_name="America/New_York")
        self.assertEqual(expected, json.loads(base64.b64decode(requirements[7:]))[1])
        self.assertEqual(expected, json.loads(base64.b64decode(proof[7:-2]))[1])

    def test_rejects_invalid_timezones_and_ambiguous_country_lists(self):
        for zone in (None, [], "../../etc/passwd", "Invalid/Timezone", "x" * 129):
            self.assertIsNone(valid_timezone(zone))
        self.assertEqual("UTC", valid_timezone("UTC"))
        self.assertIsNone(parse_provider_timezone("freeipapi", {"ipAddress": "8.8.8.8", "timeZones": ["America/Adak", "America/New_York"]}, "8.8.8.8"))
        self.assertEqual("Asia/Tokyo", parse_provider_timezone("freeipapi", {"ipAddress": "8.8.8.8", "timeZones": ["Asia/Tokyo"]}, "8.8.8.8"))
        self.assertIsNone(parse_provider_timezone("ipapi", {"ip": "1.1.1.1", "timezone": "Asia/Tokyo"}, "8.8.8.8"))
        self.assertIsNone(parse_provider_timezone("ipwho", {"ip": "8.8.8.8", "success": False, "timezone": {"id": "UTC"}}, "8.8.8.8"))

    def test_parallel_providers_use_same_ip_and_stable_priority(self):
        cache = self.cache()
        barrier = threading.Barrier(3)
        calls = []

        def lookup(provider, ip, proxy, impersonate, deadline):
            calls.append((provider, ip))
            barrier.wait(timeout=2)
            return "America/Los_Angeles" if provider == "ipwho" else "America/New_York"

        with patch.object(cache, "_request", return_value=b"ip=8.8.8.8\n"), patch.object(cache, "_lookup", side_effect=lookup):
            result = cache.resolve()
        self.assertEqual(EgressTimezone("America/Los_Angeles", "ipwho"), result)
        self.assertEqual({("ipwho", "8.8.8.8"), ("ipapi", "8.8.8.8"), ("freeipapi", "8.8.8.8")}, set(calls))

    def test_primary_failure_uses_backup(self):
        cache = self.cache()
        def lookup(provider, *_):
            if provider == "ipwho":
                raise ValueError("fixture failure")
            return "Asia/Tokyo" if provider == "ipapi" else None
        with patch.object(cache, "_request", return_value=b"ip=8.8.8.8\n"), patch.object(cache, "_lookup", side_effect=lookup):
            self.assertEqual(EgressTimezone("Asia/Tokyo", "ipapi"), cache.resolve())

    def test_cache_survives_restart_without_network_or_credentials(self):
        cache = self.cache()
        proxy = "http://fixture-user:fixture-password@proxy.invalid:8080"
        with patch.object(cache, "_request", return_value=b"ip=8.8.8.8\n") as network, \
             patch.object(cache, "_lookup", return_value="Asia/Tokyo"):
            self.assertEqual("Asia/Tokyo", cache.resolve(proxy).name)
            self.assertEqual("Asia/Tokyo", cache.resolve(proxy).name)
        self.assertEqual(1, network.call_count)
        raw = cache.path.read_text()
        self.assertNotIn("fixture-password", raw)
        self.assertNotIn("proxy.invalid", raw)
        self.assertNotIn("8.8.8.8", raw)
        self.assertEqual(0o600, cache.path.stat().st_mode & 0o777)
        restored = self.cache(cache.path)
        with patch.object(restored, "_request", side_effect=AssertionError("unexpected network")):
            self.assertEqual("Asia/Tokyo", restored.resolve(proxy).name)

    def test_different_proxy_routes_do_not_share_cache(self):
        cache = self.cache()
        with patch.object(cache, "_request", return_value=b"ip=8.8.8.8\n"), \
             patch.object(cache, "_lookup", side_effect=lambda _, ip, proxy, *rest: "Asia/Tokyo" if proxy.endswith("a") else "America/New_York"):
            self.assertEqual("Asia/Tokyo", cache.resolve("http://proxy-a").name)
            self.assertEqual("America/New_York", cache.resolve("http://proxy-b").name)
        self.assertEqual(2, len(cache._entries))

    def test_stale_cache_returns_immediately_while_refresh_runs(self):
        cache = self.cache()
        with patch.object(cache, "_request", return_value=b"ip=8.8.8.8\n"), patch.object(cache, "_lookup", return_value="Asia/Tokyo"):
            cache.resolve()
        next(iter(cache._entries.values()))["checked_at"] -= 601
        cache._retry_at.clear()
        entered, release = threading.Event(), threading.Event()
        def lookup(*_):
            entered.set()
            release.wait(timeout=2)
            return "America/New_York"
        with patch.object(cache, "_request", return_value=b"ip=8.8.8.8\n"), patch.object(cache, "_lookup", side_effect=lookup):
            started = time.monotonic()
            self.assertEqual("Asia/Tokyo", cache.resolve().name)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertTrue(entered.wait(timeout=1))
            future = next(iter(cache._flights.values()))
            release.set()
            future.result(timeout=2)
        self.assertEqual("America/New_York", cache.resolve().name)

    def test_failures_have_backoff_and_safe_fallback(self):
        cache = self.cache()
        with patch.object(cache, "_request", side_effect=ValueError("fixture failure")) as network:
            self.assertEqual(EgressTimezone(), cache.resolve())
            self.assertEqual(EgressTimezone(), cache.resolve())
        self.assertEqual(1, network.call_count)

    def test_rejects_corrupt_disk_cache(self):
        cache = self.cache()
        cache.path.write_text('{"version":1,"entries":[]}')
        with patch.object(cache, "_request", side_effect=ValueError("fixture failure")):
            self.assertEqual(EgressTimezone(), cache.resolve())

    def test_geolocation_client_has_no_chatgpt_headers_or_cookies(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [b"fixture"]
        session = Mock()
        session.get.return_value = response
        with patch("services.egress_timezone.requests.Session") as constructor:
            constructor.return_value.__enter__.return_value = session
            self.assertEqual(b"fixture", EgressTimezoneCache._request("https://ipwho.is/8.8.8.8", "http://proxy.invalid", "chrome110", time.monotonic() + 1))
        self.assertEqual({"Accept": "application/json,text/plain"}, session.get.call_args.kwargs["headers"])
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertTrue(constructor.call_args.kwargs["verify"])
        self.assertEqual("http://proxy.invalid", constructor.call_args.kwargs["proxy"])
        response.close.assert_called_once()

    def test_request_deadline_and_response_size_are_bounded(self):
        with patch("services.egress_timezone.requests.Session") as constructor:
            with self.assertRaises(TimeoutError):
                EgressTimezoneCache._request("https://ipwho.is/8.8.8.8", "", "chrome110", time.monotonic() - 1)
            constructor.assert_not_called()
            response = Mock(status_code=200)
            response.iter_content.return_value = [b"x" * 32769]
            constructor.return_value.__enter__.return_value.get.return_value = response
            with self.assertRaisesRegex(ValueError, "too large"):
                EgressTimezoneCache._request("https://ipwho.is/8.8.8.8", "", "chrome110", time.monotonic() + 1)
            response.close.assert_called_once()

    def test_backend_reuses_timezone_in_prepare_generation_and_sentinel(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend._egress_timezone = EgressTimezone("America/New_York", "ipwho")
        backend.base_url = "https://chatgpt.com"
        backend.access_token = "fixture-token"
        backend.user_agent = "fixture-agent"
        backend.pow_script_sources = []
        backend.pow_data_build = ""
        backend.session = Mock()
        backend._headers = lambda _, extra=None: dict(extra or {})
        expected = backend._timezone_fields()
        backend.session.post.return_value = Mock(status_code=200, json=lambda: {"conduit_token": "fixture-conduit"})
        backend._prepare_image_conversation("fixture prompt", ChatRequirements("fixture"), "gpt-image-2.5")
        prepare = backend.session.post.call_args.kwargs["json"]
        generated = backend._image_generation_payload("fixture prompt", "gpt-image-2.5")
        for payload in (prepare, generated):
            self.assertEqual(expected, {k: payload[k] for k in expected})
        self.assertEqual("America/New_York", backend._chat_target()[1])
        backend.session.post.side_effect = [Mock(status_code=200, json=lambda: {"prepare_token": "fixture", "proofofwork": {"required": True, "seed": "fixture", "difficulty": "f"}}), Mock(status_code=200, json=lambda: {"token": "fixture"})]
        with patch("services.openai_backend_api.build_legacy_requirements_token", return_value="fixture-p") as requirements, \
             patch("services.openai_backend_api.build_proof_token", return_value="fixture-proof") as proof:
            backend._get_chat_requirements()
        self.assertEqual("America/New_York", requirements.call_args.kwargs["timezone_name"])
        self.assertEqual("America/New_York", proof.call_args.kwargs["timezone_name"])


if __name__ == "__main__":
    unittest.main()
