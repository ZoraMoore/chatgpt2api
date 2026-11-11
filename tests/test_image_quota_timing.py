from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from services import image_quota as quota
from utils.helper import UpstreamHTTPError, ensure_ok


NOW = 1794000000.0
REASON = "你已达到图像生成请求上限。"


def final_message(text=REASON, **fields):
    return {"id": "quota-message", "author": {"role": "assistant"}, "channel": "final",
            "end_turn": True, "status": "finished_successfully", "create_time": NOW,
            "content": {"content_type": "text", "parts": [text]}, **fields}


class ImageQuotaTimingTests(unittest.TestCase):
    def parser(self, name):
        parser = getattr(quota, name, None)
        self.assertTrue(callable(parser), f"missing parser: {name}")
        return parser

    def cooldown(self, message, **kwargs):
        return self.parser("resolve_image_cooldown")(message, now=NOW, **kwargs)

    def test_absolute_iso_seconds_and_milliseconds_share_one_deadline(self):
        reset = self.parser("resolve_image_reset")
        deadline = NOW + 1200
        for value in (deadline, str(deadline), deadline * 1000,
                      datetime.fromtimestamp(deadline, timezone.utc).isoformat()):
            with self.subTest(value=value):
                self.assertEqual(deadline, reset(value, now=NOW))

    def test_relative_reset_requires_explicit_relative_mode(self):
        reset = self.parser("resolve_image_reset")
        self.assertEqual(NOW + 1200, reset(1200, now=NOW, relative=True))
        self.assertEqual(NOW, reset(0, now=NOW, relative=True))
        self.assertIsNone(reset(0, now=NOW))

    def test_malformed_ambiguous_or_unbounded_reset_is_rejected(self):
        reset = self.parser("resolve_image_reset")
        for value in (None, True, -1, float("inf"), float("nan"), "x" * 129,
                      "tomorrow", "2026-11-07T10:00:00", NOW + 31 * 86400):
            with self.subTest(value=value):
                self.assertIsNone(reset(value, now=NOW))
        for value in (-1, float("inf"), 31 * 86400):
            with self.subTest(relative=value):
                self.assertIsNone(reset(value, now=NOW, relative=True))

    def test_structured_absolute_time_wins_over_retry_and_text(self):
        result = self.cooldown(final_message(REASON + "20分钟后重试。", metadata={
            "system_error": {"reset_at": NOW + 7200}}), retry_after="1800")
        self.assertEqual(NOW + 7200, result["restore_at"])
        self.assertEqual(NOW + 7200, result["next_check_at"])
        self.assertEqual("structured_absolute", result["time_source"])

    def test_reset_after_accepts_relative_seconds_or_absolute_iso(self):
        for value, source in ((1200, "structured_relative"),
                              (datetime.fromtimestamp(NOW + 1200, timezone.utc).isoformat(),
                               "structured_absolute")):
            with self.subTest(value=value):
                result = self.cooldown({"reset_after": value})
                self.assertEqual(NOW + 1200, result["restore_at"])
                self.assertEqual(source, result["time_source"])

    def test_retry_after_supports_fractional_seconds_and_http_date(self):
        for value, seconds in (("12.5", 12.5), (0, 0),
                               ("Fri, 06 Nov 2026 21:21:00 GMT", 60)):
            with self.subTest(value=value):
                result = self.cooldown({}, retry_after=value)
                self.assertEqual(NOW + seconds, result["restore_at"])
                self.assertEqual("retry_after", result["time_source"])

    def test_relative_text_is_anchored_to_its_original_message_time(self):
        for text, seconds in (("请在20分钟后重试。", 1200), ("请等待半小时。", 1800),
                              ("半小时后可再次生成。", 1800),
                              ("Please try again in 1 hour and 20 minutes.", 4800),
                              ("You can create more images in 12 minutes.", 720)):
            with self.subTest(text=text):
                result = self.cooldown({"reason": REASON + text}, occurred_at=NOW - 60)
                self.assertEqual(NOW - 60 + seconds, result["restore_at"])
                self.assertEqual("text_relative", result["time_source"])

    def test_unrelated_plan_period_is_not_added_to_recovery_wait(self):
        result = self.cooldown({"reason": REASON + "24小时最多100次，20分钟后重试。"})
        self.assertEqual(NOW + 1200, result["restore_at"])

    def test_malformed_duration_does_not_become_an_immediate_reset(self):
        for text in ("五五小时后重试。", "-20分钟后重试。", "半半小时后重试。"):
            with self.subTest(text=text):
                self.assertIsNone(self.cooldown({"reason": REASON + text})["restore_at"])

    def test_nested_serialized_error_keeps_its_structured_time(self):
        result = self.cooldown({"system_error": '{"message":"quota", "reset_after":1200}'})
        self.assertEqual(NOW + 1200, result["restore_at"])

    def test_invalid_observation_time_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parser("resolve_image_cooldown")({}, now=True)

    def test_unknown_time_has_only_a_separate_conservative_check_time(self):
        result = self.cooldown({"reason": REASON + "每24小时最多100次。"})
        self.assertIsNone(result["restore_at"])
        self.assertEqual(NOW + quota.CHECK_RETRY, result["next_check_at"])
        self.assertEqual("unknown", result["time_source"])

    def test_existing_detection_keeps_unknown_restore_and_reports_it_honestly(self):
        evidence = quota.detect_message_limit(final_message(), "conversation", now=NOW)
        self.assertIsNotNone(evidence)
        self.assertIsNone(evidence.restore_at)
        self.assertEqual(NOW + quota.CHECK_RETRY, evidence.next_check_at)
        error = quota.ImageQuotaExceeded(evidence.record())
        self.assertIsNone(error.restore_at)
        self.assertIn("未提供恢复时间", str(error))
        self.assertNotIn("预计恢复时间", str(error))

    def test_old_positional_evidence_constructor_remains_compatible(self):
        evidence = quota.ImageQuotaEvidence(REASON, "test", "conversation", "message",
                                           "event", NOW, NOW, NOW + 1200)
        self.assertEqual(NOW + 1200, evidence.restore_at)
        self.assertIn("预计恢复时间", str(quota.ImageQuotaExceeded(evidence.record())))

    def test_user_quotes_and_non_image_tools_are_still_not_quota_evidence(self):
        message = final_message()
        self.assertIsNone(quota.detect_message_limit(message, user_text=REASON, now=NOW))
        message.update(author={"role": "tool", "name": "python"}, system_error=REASON)
        self.assertIsNone(quota.detect_message_limit(message, now=NOW))

    def test_http_date_retry_after_is_preserved_as_delay(self):
        with patch("utils.helper.time.time", return_value=NOW):
            error = UpstreamHTTPError("/backend-api/f/conversation", 429, {},
                                      retry_after="Fri, 06 Nov 2026 21:21:00 GMT")
        self.assertEqual(60, error.retry_after)

    def test_http_retry_deadline_does_not_move_when_classification_is_delayed(self):
        with patch("utils.helper.time.time", return_value=NOW):
            error = UpstreamHTTPError("/backend-api/f/conversation", 429,
                                      {"error": {"code": "image_quota_exceeded"}}, retry_after=1200)
        classify = self.parser("image_quota_evidence_from_http")
        first = classify(error, now=NOW + 60)
        self.assertEqual(NOW + 1200, first.restore_at)
        self.assertIs(first, classify(error, now=NOW + 120))

    def test_ensure_ok_preserves_fractional_retry_after(self):
        response = SimpleNamespace(status_code=429, text="", json=lambda: {},
                                   headers={"Retry-After": "12.5"})
        with patch("utils.helper.time.time", return_value=NOW):
            with self.assertRaises(UpstreamHTTPError) as raised:
                ensure_ok(response, "/backend-api/f/conversation")
        self.assertEqual(12.5, raised.exception.retry_after)

    def test_http_quota_classification_requires_explicit_generation_evidence(self):
        classify = self.parser("image_quota_evidence_from_http")
        error = UpstreamHTTPError("/backend-api/f/conversation", 429,
                                  {"error": {"code": "image_quota_exceeded", "reset_after": 1200}})
        evidence = classify(error, now=NOW)
        self.assertIsNotNone(evidence)
        self.assertEqual(NOW + 1200, evidence.restore_at)
        for context, body in (("/backend-api/f/conversation", {"error": {"code": "rate_limit_exceeded"}}),
                              ("/backend-api/conversation/existing", {"error": {"message": REASON}}),
                              ("/backend-api/f/conversation", {"error": {
                                  "type": "moderation_error", "message": REASON}})):
            with self.subTest(context=context, body=body):
                self.assertIsNone(classify(UpstreamHTTPError(context, 429, body), now=NOW))

    def test_request_throttle_keeps_retry_deadline_and_a_fixed_reason(self):
        classify = self.parser("image_throttle_evidence_from_http")
        with patch("utils.helper.time.time", return_value=NOW):
            error = UpstreamHTTPError("/backend-api/f/conversation", 429,
                                      {"error": {"message": "private upstream detail"}}, retry_after=90)
        evidence = classify(error, now=NOW + 10)
        self.assertEqual("upstream_http_image_throttle", evidence.source)
        self.assertEqual("图片请求被上游暂时限流。", evidence.reason)
        self.assertEqual(NOW + 90, evidence.restore_at)
        self.assertEqual(NOW + 90, evidence.next_check_at)
        self.assertEqual("retry_after", evidence.time_source)
        self.assertIs(evidence, classify(error, now=NOW + 20))

    def test_prepare_request_throttle_accepts_structured_recovery_time(self):
        classify = self.parser("image_throttle_evidence_from_http")
        error = UpstreamHTTPError("/backend-api/f/conversation/prepare", 429,
                                  {"error": {"code": "rate_limit_exceeded", "reset_at": NOW + 120}})
        evidence = classify(error, now=NOW)
        self.assertEqual(NOW + 120, evidence.restore_at)
        self.assertEqual("structured_absolute", evidence.time_source)

    def test_request_throttle_unknown_time_waits_thirty_seconds_without_guessing_text(self):
        classify = self.parser("image_throttle_evidence_from_http")
        error = UpstreamHTTPError("/backend-api/f/conversation", 429,
                                  {"error": {"message": "Try again in 20 minutes."}})
        evidence = classify(error, now=NOW)
        self.assertIsNone(evidence.restore_at)
        self.assertEqual(NOW + 30, evidence.next_check_at)
        self.assertEqual("unknown", evidence.time_source)

    def test_request_throttle_does_not_duplicate_explicit_quota_evidence(self):
        classify = self.parser("image_throttle_evidence_from_http")
        error = UpstreamHTTPError("/backend-api/f/conversation", 429,
                                  {"error": {"code": "image_quota_exceeded"}})
        self.assertIsNone(classify(error, now=NOW))
        self.assertIsNotNone(quota.image_quota_evidence_from_http(error, now=NOW))

    def test_request_throttle_excludes_readback_download_policy_and_auth_errors(self):
        classify = self.parser("image_throttle_evidence_from_http")
        for context, status, body in (
                ("/backend-api/conversation/existing", 429, {}),
                ("/backend-api/tasks", 429, {}),
                ("image_download", 429, {}),
                ("/backend-api/f/conversation", 401, {}),
                ("/backend-api/f/conversation", 429, {"error": {"type": "moderation_error"}}),
                ("/backend-api/f/conversation", 429, {"error": {"code": "invalid_access_token"}}),
                ("/backend-api/f/conversation", 429, {"error": {"message": "Unauthorized"}})):
            with self.subTest(context=context, status=status, body=body):
                self.assertIsNone(classify(UpstreamHTTPError(context, status, body, retry_after=10), now=NOW))

    def test_request_throttle_never_changes_response_quota_fields(self):
        classify = self.parser("image_throttle_evidence_from_http")
        body = {"error": {"code": "rate_limit_exceeded"}, "quota": 73, "success": 27}
        error = UpstreamHTTPError("/backend-api/f/conversation", 429, body)
        evidence = classify(error, now=NOW)
        self.assertIsNotNone(evidence)
        self.assertEqual({"error": {"code": "rate_limit_exceeded"}, "quota": 73, "success": 27}, body)
        self.assertNotIn("quota", evidence.record())


if __name__ == "__main__":
    unittest.main()
