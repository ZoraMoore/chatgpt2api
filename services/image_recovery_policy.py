"""自动恢复只读既有会话；期限与重试原因采用受限持久化契约。"""
from __future__ import annotations

import math
import re

AUTO_RECOVERY_SECONDS = 1800.0
AUTO_RECOVERY_ATTEMPTS = 60
AUTO_RECOVERY_CONCURRENCY = 2
AUTO_RETRY_REASONS = frozenset({
    "completion_missing", "document_in_progress", "tool_incomplete", "calls_pending",
    "artifact_missing", "generation_pending", "invalid_branch", "observed_tools_missing",
    "observed_calls_missing", "snapshot_messages_missing", "snapshot_asset_changed",
    "snapshot_final_regressed", "snapshot_regressed_failure", "readback_unavailable",
    "input_assets_invalid", "missing_submission", "stream_submission_unconfirmed",
    "image_final_download_failed", "original_account_busy", "restart_pending",
})


def valid_recovery_plan(value) -> dict:
    fields = {"version", "state", "attempts", "started_at", "next_poll_at", "deadline", "last_reason"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["version"]) is not int
            or value["version"] != 1 or not isinstance(value["state"], str)
            or value["state"] not in {"waiting", "polling", "stopped", "completed"}
            or type(value["attempts"]) is not int or not 0 <= value["attempts"] <= AUTO_RECOVERY_ATTEMPTS):
        return {}
    if any(type(value[k]) not in (int, float) or not math.isfinite(value[k]) or value[k] < 0
           for k in ("started_at", "next_poll_at", "deadline")):
        return {}
    if not 0 <= value["deadline"] - value["started_at"] <= AUTO_RECOVERY_SECONDS:
        return {}
    if not isinstance(value["last_reason"], str) or not re.fullmatch(r"[a-z_]{1,80}", value["last_reason"]):
        return {}
    return dict(value)


def recovery_delay(attempts: int) -> float:
    return 10.0 if attempts == 0 else 30.0 if attempts == 1 else 60.0


def recovery_plan(now: float, reason: str) -> dict:
    return {"version": 1, "state": "waiting", "attempts": 0, "started_at": now,
            "next_poll_at": now + recovery_delay(0), "deadline": now + AUTO_RECOVERY_SECONDS,
            "last_reason": reason}
