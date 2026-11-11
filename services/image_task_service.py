from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import wait
from datetime import datetime
from pathlib import Path
from typing import Any

from services.config import DATA_DIR, config
from services.account_service import account_service
from services.image_execution import image_work_pool
from services.image_recovery_policy import (AUTO_RECOVERY_ATTEMPTS, AUTO_RECOVERY_CONCURRENCY,
    AUTO_RETRY_REASONS, valid_recovery_plan, recovery_delay, recovery_plan)
from services.content_filter import request_text
from services.log_service import LOG_TYPE_CALL, log_service
from services.protocol import openai_v1_image_edit, openai_v1_image_generations
from utils.helper import resolve_image_transport
from utils.log import logger

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_ERROR = "error"
TERMINAL_STATUSES = {TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}
UNFINISHED_STATUSES = {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _timestamp(value: object) -> float:
    if not isinstance(value, str) or not value.strip():
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:26], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _clean(value: object, default: str = "") -> str:
    return str(value or default).strip()


_RECOVERY_FAILURE_SOURCES = (
    "resume_poll", "transport", "upstream_http", "upstream_http_conversation_throttle",
    "upstream_http_image_throttle", "upstream_http_image_quota", "image_tool_system_error", "assistant_final",
)


def _validated_recovery_failure(value: object) -> dict:
    # 仅保留定位异常所需的结构字段，异常正文、HTTP上下文和凭据均不得落盘。
    if not isinstance(value, dict) or value.get("source") not in _RECOVERY_FAILURE_SOURCES:
        return {}
    name = value.get("exception_type")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name):
        return {}
    result = {"source": value["source"], "exception_type": name}
    status = value.get("status_code")
    if type(status) is int and 100 <= status <= 599:
        result["status_code"] = status
    retry_at = value.get("retry_at")
    if type(retry_at) in (int, float) and 0 <= retry_at < 253402300800 and math.isfinite(retry_at):
        result["retry_at"] = retry_at
    return result


def _owner_id(identity: dict[str, object]) -> str:
    return _clean(identity.get("id")) or "anonymous"


def _task_key(owner_id: str, task_id: str) -> str:
    return f"{owner_id}:{task_id}"


def _validated_delivery_context(value: object) -> dict[str, Any]:
    # 恢复只能保留本轮身份和输入排除证据，整份拒绝多余字段，避免长期凭据落盘。
    fields = {"submitted_message_id", "input_asset_ids", "input_image_hashes"}
    if not isinstance(value, dict) or not fields.issubset(value) or set(value) - fields - {"completion_receipt", "observed_evidence"}:
        return {}
    submitted = value["submitted_message_id"]
    if not isinstance(submitted, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", submitted):
        return {}
    assets = value["input_asset_ids"]
    hashes = value["input_image_hashes"]
    if (not isinstance(assets, list) or len(assets) > 20
            or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item)
                   for item in assets)):
        return {}
    if (not isinstance(hashes, list) or len(hashes) > 10
            or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", item)
                   for item in hashes)):
        return {}
    result = {"submitted_message_id": submitted, "input_asset_ids": list(assets), "input_image_hashes": list(hashes)}
    if "completion_receipt" in value:
        from services.protocol.image_delivery import validated_completion_receipt

        receipt = validated_completion_receipt(value["completion_receipt"])
        if not receipt or receipt["submitted_message_id"] != submitted:
            return {}
        result["completion_receipt"] = receipt
    if "observed_evidence" in value:
        from services.protocol.image_delivery import validated_observed_evidence

        observed = validated_observed_evidence(value["observed_evidence"])
        if not observed:
            return {}
        result["observed_evidence"] = observed
    return result


def _collect_image_urls(data: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in data:
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": task.get("id"),
        "status": task.get("status"),
        "mode": task.get("mode"),
        "model": task.get("model"),
        "size": task.get("size"),
        "quality": task.get("quality"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }
    if task.get("image_transport") in ("voice", "conversation"):
        item["image_transport"] = task["image_transport"]
    if task.get("conversation_id"):
        item["conversation_id"] = task.get("conversation_id")
    if task.get("data") is not None:
        item["data"] = task.get("data")
    if task.get("usage") is not None:
        item["usage"] = task.get("usage")
    if task.get("error"):
        item["error"] = task.get("error")
    if task.get("progress"):
        item["progress"] = task.get("progress")
    if task.get("duration_ms") is not None:
        item["duration_ms"] = task.get("duration_ms")
    plan = valid_recovery_plan(task.get("auto_recovery"))
    if plan and plan["state"] in {"waiting", "polling"}:
        item["recovering"] = True
    if task.get("status") in (TASK_STATUS_RUNNING, TASK_STATUS_QUEUED):
        if task.get("status") == TASK_STATUS_RUNNING:
            # RUNNING 状态仅在 started_ts 被设置后（generating）才计时
            base_ts = task.get("started_ts")
        else:
            # QUEUED 状态从 created_ts 开始计时（排队等待中）
            base_ts = task.get("created_ts") or task.get("updated_ts")
        if base_ts:
            item["elapsed_secs"] = round(time.time() - base_ts, 1)
    return item


class ImageTaskService:
    def __init__(
        self,
        path: Path,
        *,
        generation_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_generations.handle,
        edit_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_edit.handle,
        retention_days_getter: Callable[[], int] | None = None,
    ):
        self.path = path
        self.generation_handler = generation_handler
        self.edit_handler = edit_handler
        self.retention_days_getter = retention_days_getter or (lambda: config.image_retention_days)
        self._lock = threading.RLock()
        self._origin_inflight: set[str] = set()
        self._resume_leases: dict[str, object] = {}
        self._auto_futures: dict[str, Any] = {}
        self._recovery_stop = threading.Event()
        self._recovery_wake = threading.Event()
        self._recovery_thread = None
        self._tasks: dict[str, dict[str, Any]] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._tasks = self._load_locked()
            changed = self._recover_unfinished_locked()
            changed = self._cleanup_locked() or changed
            if changed:
                self._save_locked()

    def make_recovery_checkpoint(self, identity: dict, *, task_id: str, mode: str, model: str,
                                 base_url: str = "", existing_task_id: str = "", origin: bool = True,
                                 defer_release: bool = False):
        """同步调用仅在进入恢复时登记；每张图固定原owner/账号/会话，正常响应保持原格式。"""
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise ValueError("invalid recovery task identity")
        owner = _owner_id(identity)

        def checkpoint(event: dict):
            if not isinstance(event, dict) or event.get("phase") not in {"pending", "success", "error", "released"}:
                raise ValueError("invalid image recovery checkpoint")
            index, total = event.get("index"), event.get("total")
            if type(index) is not int or type(total) is not int or not 1 <= index <= total <= 4:
                raise ValueError("invalid image recovery index")
            if existing_task_id and (index != 1 or total != 1):
                raise ValueError("asynchronous image task must have one result")
            cid, account_ref = event.get("conversation_id"), event.get("resume_account_ref")
            context = _validated_delivery_context(event.get("delivery_context"))
            if (not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cid)
                    or not isinstance(account_ref, str) or not re.fullmatch(r"[a-f0-9]{64}", account_ref)
                    or not context):
                raise ValueError("invalid original image recovery identity")
            for evidence_key in ("observed_evidence", "completion_receipt"):
                if evidence_key in context and context[evidence_key]["conversation_id"] != cid:
                    raise ValueError("image recovery evidence conversation mismatch")
            public_id = existing_task_id or f"recovery-{task_id}-{index}"
            key = _task_key(owner, public_id)
            with self._lock:
                task = self._tasks.get(key)
                if task is not None and any(task.get(k) and task[k] != v for k, v in (
                        ("conversation_id", cid), ("resume_account_ref", account_ref))):
                    raise ValueError("cannot replace original image recovery identity")
                if task is not None and task.get("delivery_context", {}).get("submitted_message_id") not in (None, context["submitted_message_id"]):
                    raise ValueError("cannot replace original image submission identity")
                if event["phase"] == "released":
                    if not defer_release:
                        self._origin_inflight.discard(key)
                        self._recovery_wake.set()
                    return public_id if task else ""
                if task is not None and task.get("status") == TASK_STATUS_SUCCESS:
                    return public_id
                if task is None:
                    if event["phase"] != "pending":
                        return ""
                    if existing_task_id:
                        raise ValueError("original image task is missing")
                    now = _now_iso()
                    task = {"id": public_id, "owner_id": owner, "mode": "edit" if mode == "edit" else "generate",
                            "model": model, "image_transport": "conversation", "created_at": now,
                            "created_ts": time.time(), "started_ts": time.time(), "prompt": "",
                            "response_format": "url", "base_url": base_url}
                    self._tasks[key] = task
                task.update(conversation_id=cid, resume_account_ref=account_ref, delivery_context=context,
                            updated_at=_now_iso(), updated_ts=time.time())
                phase = event["phase"]
                if phase == "pending":
                    if origin:
                        self._origin_inflight.add(key)
                    task.update(status=TASK_STATUS_RUNNING, progress="confirming_image", error="")
                elif phase == "error":
                    reason = event.get("reason", "")
                    reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,80}", reason) else "unavailable"
                    self._set_recovery_error_locked(task, reason, "原会话图片仍待确认：" + reason)
                else:
                    data = event.get("data")
                    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                        raise ValueError("recovery must deliver one image")
                    url = data[0].get("url")
                    if not isinstance(url, str) or not url:
                        # 同步b64响应不写入任务JSON；沿用已有图片存储，只保存最终URL。
                        from services.protocol.conversation import format_image_result

                        raw = data[0].get("b64_json")
                        if not isinstance(raw, str) or not 0 < len(raw) <= 70 * 1024 * 1024:
                            raise ValueError("invalid final image recovery data")
                        stored = format_image_result([{"b64_json": raw}], "", "url", base_url)
                        url = stored["data"][0]["url"]
                    task.update(status=TASK_STATUS_SUCCESS, data=[{"url": url}], progress="receiving_image", error="")
                    if task.get("auto_recovery"):
                        task["auto_recovery"].update(state="completed", last_reason="ready")
                self._save_locked()
            return public_id

        return checkpoint

    @staticmethod
    def _has_recovery_identity(task: dict) -> bool:
        context = _validated_delivery_context(task.get("delivery_context"))
        cid = task.get("conversation_id")
        if (task.get("image_transport") != "conversation" or not task.get("owner_id")
                or not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cid)
                or not isinstance(task.get("resume_account_ref"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", task["resume_account_ref"]) or not context):
            return False
        proof = context.get("observed_evidence") or context.get("completion_receipt")
        return bool(proof and proof.get("conversation_id") == cid)

    def _set_recovery_error_locked(self, task: dict, reason: str, message: str, *, now=None, retry_at=None):
        now = time.time() if now is None else now
        task.update(status=TASK_STATUS_ERROR, error=message, data=[], updated_ts=now,
                    updated_at=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"))
        plan = task.get("auto_recovery")
        if reason in AUTO_RETRY_REASONS and self._has_recovery_identity(task):
            if plan is None:
                plan = task["auto_recovery"] = recovery_plan(now, reason)
            if (valid_recovery_plan(plan) and plan["state"] not in {"stopped", "completed"}
                    and plan["attempts"] < AUTO_RECOVERY_ATTEMPTS and now < plan["deadline"]):
                # 重启及后续错误不得把已持久化的上游冷却提前；等待不占工作槽位。
                due_at = max(plan["next_poll_at"], now + recovery_delay(plan["attempts"]))
                if type(retry_at) in (int, float) and 0 <= retry_at < 253402300800 and math.isfinite(retry_at):
                    due_at = max(due_at, retry_at)
                plan.update(state="waiting", last_reason=reason, next_poll_at=min(plan["deadline"], due_at))
                task.update(status=TASK_STATUS_QUEUED, progress="confirming_image", error="")
                self._recovery_wake.set()
                return
        if valid_recovery_plan(plan):
            final_reason = ("recovery_deadline_exceeded" if now >= plan["deadline"] else
                            "recovery_attempts_exhausted" if plan["attempts"] >= AUTO_RECOVERY_ATTEMPTS else reason)
            plan.update(state="stopped", last_reason=final_reason)
            task["error"] = "原会话自动续查已停止：" + final_reason

    def prepare_auto_recovery(self, now=None):
        """恢复持久化计划；只对受限、可核验的旧恢复记录执行一次迁移。"""
        now = time.time() if now is None else now
        changed = False
        migrated = 0
        with self._lock:
            ordered = sorted(self._tasks.values(), key=lambda t: _timestamp(t.get("updated_at")), reverse=True)
            for task in ordered:
                if task.get("status") == TASK_STATUS_SUCCESS:
                    continue
                plan = task.get("auto_recovery")
                if valid_recovery_plan(plan):
                    if plan["state"] in {"waiting", "polling"}:
                        if not self._has_recovery_identity(task):
                            self._set_recovery_error_locked(task, "invalid_recovery_identity", "原恢复身份无效", now=now)
                        elif now >= plan["deadline"] or plan["attempts"] >= AUTO_RECOVERY_ATTEMPTS:
                            self._set_recovery_error_locked(task, plan["last_reason"], "原会话恢复期限已结束", now=now)
                        else:
                            plan.update(state="waiting", next_poll_at=max(now, plan["next_poll_at"]))
                            task.update(status=TASK_STATUS_QUEUED, progress="confirming_image", error="")
                        changed = True
                    continue
                if (plan is not None or migrated >= 200 or not str(task.get("id", "")).startswith("recovery-")
                        or task.get("status") != TASK_STATUS_ERROR or not self._has_recovery_identity(task)):
                    continue
                updated = task.get("updated_ts")
                if type(updated) not in (int, float) or not math.isfinite(updated):
                    updated = _timestamp(task.get("updated_at"))
                if not 0 <= now - updated <= 86400:
                    continue
                error = task.get("error", "")
                prefix = "原会话图片仍待确认："
                reason = error[len(prefix):] if isinstance(error, str) and error.startswith(prefix) else ""
                if reason not in AUTO_RETRY_REASONS:
                    continue
                self._set_recovery_error_locked(task, reason, error, now=now)
                migrated += 1
                changed = True
            if changed:
                self._save_locked()
        return migrated

    def dispatch_due_recoveries(self, now=None) -> int:
        now = time.time() if now is None else now
        selected = []
        with self._lock:
            if self._recovery_stop.is_set():
                return 0
            for key, task in sorted(self._tasks.items(), key=lambda item: item[1].get("auto_recovery", {}).get("next_poll_at", float("inf"))):
                plan = task.get("auto_recovery")
                if (not valid_recovery_plan(plan) or plan["state"] != "waiting"
                        or key in self._origin_inflight or key in self._resume_leases):
                    continue
                if not self._has_recovery_identity(task) or now >= plan["deadline"] or plan["attempts"] >= AUTO_RECOVERY_ATTEMPTS:
                    self._set_recovery_error_locked(task, plan["last_reason"] if self._has_recovery_identity(task) else "invalid_recovery_identity", "自动续查停止", now=now)
                    self._save_locked()
                    continue
                if plan["next_poll_at"] > now or len(self._auto_futures) >= AUTO_RECOVERY_CONCURRENCY:
                    continue
                lease = object()
                self._resume_leases[key] = lease
                self._auto_futures[key] = None
                plan.update(state="polling", attempts=plan["attempts"] + 1)
                task.update(status=TASK_STATUS_RUNNING, progress="confirming_image")
                selected.append((key, lease))
            if selected:
                self._save_locked()
        for key, lease in selected:
            try:
                future = image_work_pool.submit(self._run_auto_recovery, key, lease)
            except Exception:
                with self._lock:
                    self._set_recovery_error_locked(self._tasks[key], "readback_unavailable", "后台查询暂不可用")
                    self._release_recovery_lease(key, lease)
                    self._save_locked()
                continue
            with self._lock:
                self._auto_futures[key] = future
            future.add_done_callback(lambda done, k=key, ticket=lease: self._release_recovery_lease(k, ticket, done))
            if self._recovery_stop.is_set():
                future.cancel()
        return len(selected)

    def _release_recovery_lease(self, key, lease, future=None):
        with self._lock:
            if self._resume_leases.get(key) is lease:
                task = self._tasks.get(key)
                if future is not None and task and task.get("auto_recovery", {}).get("state") == "polling":
                    if future.cancelled():
                        self._set_recovery_error_locked(task, task["auto_recovery"]["last_reason"], "后台查询暂缓")
                        self._save_locked()
                    elif future.exception() is not None:
                        self._set_recovery_error_locked(task, "recovery_interrupted", "后台查询异常终止")
                        self._save_locked()
                self._resume_leases.pop(key, None)
                self._auto_futures.pop(key, None)
        self._recovery_wake.set()

    def _run_auto_recovery(self, key, lease):
        with self._lock:
            task = self._tasks.get(key)
            if self._recovery_stop.is_set() or self._resume_leases.get(key) is not lease or not task:
                return
            cid, mode, model = task["conversation_id"], task["mode"], task["model"]
            identity = {"id": task["owner_id"]}
        self._run_resume_poll(key, cid, 8.0, identity, mode, model, auto_recovery=True, lease=lease)

    def start_auto_recovery(self):
        with self._lock:
            if self._recovery_thread is not None:
                return
            self.prepare_auto_recovery()
            self._recovery_stop.clear()
            def run():
                while not self._recovery_stop.is_set():
                    try:
                        self.dispatch_due_recoveries()
                    except Exception as exc:
                        logger.error({"event": "image_recovery_scheduler_error", "error_type": type(exc).__name__})
                    self._recovery_wake.wait(1.0)
                    self._recovery_wake.clear()
            self._recovery_thread = threading.Thread(target=run, name="image-recovery-scheduler", daemon=True)
            self._recovery_thread.start()

    def stop_auto_recovery(self):
        self._recovery_stop.set()
        self._recovery_wake.set()
        with self._lock:
            thread = self._recovery_thread
        if thread is not None:
            thread.join(timeout=2.0)
        with self._lock:
            futures = [future for future in self._auto_futures.values() if future is not None]
        for future in futures:
            future.cancel()
        pending = [future for future in futures if not future.done()]
        if pending:
            wait(pending, timeout=45.0)
        with self._lock:
            self._recovery_thread = None

    def submit_generation(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        size: str | None,
        quality: str = "auto",
        base_url: str = "",
        image_transport: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "model": model,
            "n": 1,
            "size": size,
            "quality": quality,
            "response_format": "url",
            "base_url": base_url,
            "_image_transport": config.image_transport if image_transport is None else image_transport,
        }
        return self._submit(identity, client_task_id=client_task_id, mode="generate", payload=payload)

    def submit_edit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        size: str | None,
        quality: str = "auto",
        base_url: str = "",
        images: list[tuple[bytes, str, str]] | None = None,
        masks: list[tuple[bytes, str, str]] | None = None,
        image_transport: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "images": images or [],
            "mask": masks or [],
            "model": model,
            "n": 1,
            "size": size,
            "quality": quality,
            "response_format": "url",
            "base_url": base_url,
            "_image_transport": config.image_transport if image_transport is None else image_transport,
        }
        return self._submit(identity, client_task_id=client_task_id, mode="edit", payload=payload)

    def list_tasks(self, identity: dict[str, object], task_ids: list[str]) -> dict[str, Any]:
        owner = _owner_id(identity)
        requested_ids = [_clean(task_id) for task_id in task_ids if _clean(task_id)]
        with self._lock:
            if self._cleanup_locked():
                self._save_locked()
            items = []
            missing_ids = []
            for task_id in requested_ids:
                task = self._tasks.get(_task_key(owner, task_id))
                if task is None:
                    missing_ids.append(task_id)
                else:
                    items.append(_public_task(task))
            if not requested_ids:
                items = [
                    _public_task(task)
                    for task in self._tasks.values()
                    if task.get("owner_id") == owner
                ]
                items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
                missing_ids = []
            return {"items": items, "missing_ids": missing_ids}

    def _submit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        mode: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = _clean(client_task_id)
        if not task_id:
            raise ValueError("client_task_id is required")
        transport = payload.get("_image_transport", config.image_transport)
        if not isinstance(transport, str) or transport not in ("voice", "conversation"):
            raise ValueError("image_transport must be voice or conversation")
        transport = resolve_image_transport(payload.get("model"), transport)
        # 入队时固定型号所需的有效模式，之后设置更新不能改变排队或同一批图片。
        payload = {**payload, "_image_transport": transport}
        owner = _owner_id(identity)
        key = _task_key(owner, task_id)
        now = _now_iso()
        should_start = False
        with self._lock:
            cleaned = self._cleanup_locked()
            task = self._tasks.get(key)
            if task is not None:
                if cleaned:
                    self._save_locked()
                return _public_task(task)
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": TASK_STATUS_QUEUED,
                "mode": mode,
                "model": _clean(payload.get("model"), "gpt-image-2"),
                "size": _clean(payload.get("size")),
                "quality": _clean(payload.get("quality"), "auto"),
                "image_transport": transport,
                "created_at": now,
                "updated_at": now,
                "created_ts": time.time(),
                "prompt": _clean(payload.get("prompt")),
                "response_format": _clean(payload.get("response_format"), "url"),
                "base_url": _clean(payload.get("base_url")),
            }
            self._tasks[key] = task
            self._save_locked()
            should_start = True

        if should_start:
            image_work_pool.submit(
                self._run_task, key, mode, payload, dict(identity), _clean(payload.get("model"), "gpt-image-2"),
            )
        return _public_task(task)

    def _run_task(
        self,
        key: str,
        mode: str,
        payload: dict[str, Any],
        identity: dict[str, object],
        model: str,
    ) -> None:
        started = time.time()
        self._update_task(key, status=TASK_STATUS_RUNNING, error="")
        progress_lock = threading.Lock()
        generation_started = False

        # 创建进度回调，每个步骤完成后更新任务状态
        def progress_callback(step: str) -> None:
            nonlocal generation_started
            if step == "generating":
                with progress_lock:
                    if not generation_started:
                        generation_started = True
                        self._update_task(key, started_ts=time.time())
            self._update_task(key, progress=step)
        # 将进度回调添加到 payload 中（handler 会提取并传递给 ConversationRequest）
        payload_with_progress = {**payload, "progress_callback": progress_callback,
            "_delivery_checkpoint_callback": self.make_recovery_checkpoint(identity, task_id="async",
                existing_task_id=self._tasks[key]["id"], mode=mode, model=model, base_url=payload.get("base_url", ""),
                defer_release=True)}
        try:
            handler = self.edit_handler if mode == "edit" else self.generation_handler
            result = handler(payload_with_progress)
            if not isinstance(result, dict):
                raise RuntimeError("image task returned streaming result unexpectedly")
            data = result.get("data")
            account_email = _clean(result.get("_account_email") or result.get("account_email"))
            if not isinstance(data, list) or not data:
                upstream = _clean(result.get("message"))
                if upstream:
                    message = upstream
                else:
                    message = "号池中没有可用账号或所有账号均被限流，请检查号池状态（账号额度、是否被封禁、是否到达生图上限）"
                error = RuntimeError(message)
                if account_email:
                    setattr(error, "account_email", account_email)
                raise error
            usage = result.get("usage")
            duration_ms = int((time.time() - started) * 1000)
            self._update_task(key, status=TASK_STATUS_SUCCESS, data=data, usage=usage, error="", duration_ms=duration_ms)
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用完成",
                request_preview=request_text(payload.get("prompt")),
                urls=_collect_image_urls(data),
                account_email=account_email,
                conversation_id=_clean(result.get("_conversation_id")),
                image_execution=result.get("_image_execution"),
                image_transport=payload.get("_image_transport", ""),
            )
        except Exception as exc:
            error_message = str(exc) or "image task failed"
            account_email = _clean(getattr(exc, "account_email", ""))
            conversation_id = _clean(getattr(exc, "conversation_id", ""))
            account_ref = _clean(getattr(exc, "resume_account_ref", ""))
            delivery_context = _validated_delivery_context(getattr(exc, "delivery_context", None))
            duration_ms = int((time.time() - started) * 1000)
            with self._lock:
                task = self._tasks[key]
                if task.get("status") == TASK_STATUS_SUCCESS:
                    return
                reason = self._resume_failure_reason(exc)
                original_submitted = task.get("delivery_context", {}).get("submitted_message_id")
                if delivery_context and original_submitted and delivery_context["submitted_message_id"] != original_submitted:
                    reason, delivery_context = "submission_mismatch", {}
                if task.get("conversation_id") and conversation_id and task["conversation_id"] != conversation_id:
                    reason, conversation_id = "conversation_mismatch", task["conversation_id"]
                    delivery_context = {}
                if task.get("resume_account_ref") and account_ref and task["resume_account_ref"] != account_ref:
                    reason, account_ref = "original_account_mismatch", task["resume_account_ref"]
                    delivery_context = {}
                expected_cid = task.get("conversation_id") or conversation_id
                if delivery_context and any(delivery_context[k]["conversation_id"] != expected_cid
                        for k in ("observed_evidence", "completion_receipt") if k in delivery_context):
                    reason, delivery_context = "conversation_mismatch", {}
                if conversation_id:
                    task["conversation_id"] = conversation_id
                if delivery_context:
                    task["delivery_context"] = delivery_context
                if re.fullmatch(r"[0-9a-f]{64}", account_ref):
                    task["resume_account_ref"] = account_ref
                self._set_recovery_error_locked(task, reason, error_message)
                task.update(duration_ms=duration_ms, updated_at=_now_iso(), updated_ts=time.time())
                self._save_locked()
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用失败",
                request_preview=request_text(payload.get("prompt")),
                status="failed",
                error=error_message,
                account_email=account_email,
                conversation_id=conversation_id,
                image_execution=getattr(exc, "image_execution", None),
                image_transport=payload.get("_image_transport", ""),
            )
        finally:
            with self._lock:
                self._origin_inflight.discard(key)
            self._recovery_wake.set()

    def _log_call(
        self,
        identity: dict[str, object],
        mode: str,
        model: str,
        started: float,
        suffix: str,
        *,
        request_preview: str = "",
        status: str = "success",
        error: str = "",
        urls: list[str] | None = None,
        account_email: str = "",
        conversation_id: str = "",
        image_execution: list[dict[str, Any]] | None = None,
        image_transport: str = "",
    ) -> None:
        endpoint = "/v1/images/edits" if mode == "edit" else "/v1/images/generations"
        summary_prefix = "图生图" if mode == "edit" else "文生图"
        detail = {
            "key_id": identity.get("id"),
            "key_name": identity.get("name"),
            "role": identity.get("role"),
            "endpoint": endpoint,
            "model": model,
            "started_at": datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": _now_iso(),
            "duration_ms": int((time.time() - started) * 1000),
            "status": status,
        }
        if request_preview:
            detail["request_text"] = request_preview
        if error:
            detail["error"] = error
        if account_email:
            detail["account_email"] = account_email
        if conversation_id:
            detail["conversation_id"] = conversation_id
        if image_execution:
            detail["image_execution"] = image_execution
        if image_transport in ("voice", "conversation"):
            detail["image_transport"] = image_transport
        if urls:
            detail["urls"] = list(dict.fromkeys(urls))
        try:
            log_service.add(LOG_TYPE_CALL, f"{summary_prefix}{suffix}", detail)
        except Exception:
            pass

    def _update_task(self, key: str, **updates: Any) -> None:
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                return
            task.update(updates)
            task["updated_at"] = _now_iso()
            task["updated_ts"] = time.time()
            self._save_locked()

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        raw_items = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(raw_items, list):
            return {}
        tasks: dict[str, dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            task_id = _clean(item.get("id"))
            owner = _clean(item.get("owner_id"))
            if not task_id or not owner:
                continue
            status = _clean(item.get("status"))
            if status not in {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}:
                status = TASK_STATUS_ERROR
            task = {
                "id": task_id,
                "owner_id": owner,
                "status": status,
                "mode": "edit" if item.get("mode") == "edit" else "generate",
                "model": _clean(item.get("model"), "gpt-image-2"),
                "size": _clean(item.get("size")),
                "quality": _clean(item.get("quality"), "auto"),
                "created_at": _clean(item.get("created_at"), _now_iso()),
                "updated_at": _clean(item.get("updated_at"), _clean(item.get("created_at"), _now_iso())),
                "created_ts": item.get("created_ts"),
                "updated_ts": item.get("updated_ts"),
                "started_ts": item.get("started_ts"),
                "duration_ms": item.get("duration_ms"),
            }
            for name in ("conversation_id", "resume_account_ref", "prompt", "response_format", "base_url"):
                if isinstance(item.get(name), str):
                    task[name] = item[name]
            if item.get("image_transport") in ("voice", "conversation"):
                task["image_transport"] = item["image_transport"]
            delivery_context = _validated_delivery_context(item.get("delivery_context"))
            if delivery_context:
                task["delivery_context"] = delivery_context
            if "auto_recovery" in item:
                plan = valid_recovery_plan(item["auto_recovery"])
                if not plan:
                    plan = recovery_plan(time.time(), "invalid_recovery_plan")
                    plan["state"] = "stopped"
                task["auto_recovery"] = plan
            failure = _validated_recovery_failure(item.get("recovery_failure"))
            if failure:
                task["recovery_failure"] = failure
            data = item.get("data")
            if isinstance(data, list):
                task["data"] = data
            usage = item.get("usage")
            if isinstance(usage, dict):
                task["usage"] = usage
            error = _clean(item.get("error"))
            if error:
                task["error"] = error
            tasks[_task_key(owner, task_id)] = task
        return tasks

    def _save_locked(self) -> None:
        items = sorted(self._tasks.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps({"tasks": items}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)

    def _recover_unfinished_locked(self) -> bool:
        changed = False
        for task in self._tasks.values():
            if task.get("status") in UNFINISHED_STATUSES:
                plan = task.get("auto_recovery")
                if self._has_recovery_identity(task) and (plan is None or valid_recovery_plan(plan)):
                    if plan is None:
                        task["auto_recovery"] = recovery_plan(time.time(), "restart_pending")
                        plan = task["auto_recovery"]
                    if plan["state"] in {"waiting", "polling"}:
                        self._set_recovery_error_locked(task, plan["last_reason"], "服务已重启，继续读取原任务")
                        changed = True
                        continue
                task["status"] = TASK_STATUS_ERROR
                task["error"] = ("服务已重启，可继续查询原会话图片，勿重新生成"
                    if task.get("resume_account_ref") and task.get("delivery_context")
                    else "服务已重启，未完成的图片任务已中断")
                task["updated_at"] = _now_iso()
                changed = True
        return changed

    def _cleanup_locked(self) -> bool:
        try:
            retention_days = max(1, int(self.retention_days_getter()))
        except Exception:
            retention_days = 30
        cutoff = time.time() - retention_days * 86400
        removed_keys = [
            key
            for key, task in self._tasks.items()
            if task.get("status") in TERMINAL_STATUSES and _timestamp(task.get("updated_at")) < cutoff
        ]
        for key in removed_keys:
            self._tasks.pop(key, None)
        return bool(removed_keys)

    def resume_poll(
        self, identity: dict[str, object], task_id: str, extra_timeout_secs: float = 30.0,
    ) -> dict[str, Any]:
        """人工与自动查询共享领取锁，只读取原账号与原会话。"""
        if (not isinstance(extra_timeout_secs, (int, float)) or isinstance(extra_timeout_secs, bool)
                or not math.isfinite(extra_timeout_secs) or not 5 <= extra_timeout_secs <= 120):
            raise ValueError("extra_timeout_secs must be between 5 and 120")
        key = _task_key(_owner_id(identity), _clean(task_id))
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                raise ValueError("task not found")
            if key in self._resume_leases or key in self._origin_inflight:
                return _public_task(task)
            plan = valid_recovery_plan(task.get("auto_recovery"))
            if task.get("status") != TASK_STATUS_ERROR and not (plan and plan["state"] == "waiting"):
                raise ValueError("task is not in error state")
            conversation_id = _clean(task.get("conversation_id"))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", conversation_id):
                raise ValueError("task has no valid conversation_id")
            account_ref = _clean(task.get("resume_account_ref"))
            if not re.fullmatch(r"[0-9a-f]{64}", account_ref):
                raise ValueError("task has no original account reference; cannot resume safely")
            lease = object()
            self._resume_leases[key] = lease
            mode, model = task.get("mode", "generate"), task.get("model", "gpt-image-2")
            self._update_task(key, status=TASK_STATUS_QUEUED, error="")
            public = _public_task(task)
        try:
            future = image_work_pool.submit(self._run_resume_poll, key, conversation_id,
                extra_timeout_secs, dict(identity), mode, model, lease=lease)
            future.add_done_callback(lambda done: self._release_recovery_lease(key, lease))
        except Exception:
            self._release_recovery_lease(key, lease)
            raise
        return public

    @staticmethod
    def _resume_failure_reason(exc) -> str:
        from services.image_quota import ImageQuotaExceeded
        from utils.helper import UpstreamHTTPError
        if isinstance(exc, ImageQuotaExceeded):
            return ("readback_throttled" if exc.record.get("source") == "upstream_http_conversation_throttle"
                    else "image_quota_exceeded")
        if isinstance(exc, UpstreamHTTPError) and exc.status_code == 429 and re.fullmatch(
                r"/backend-api/conversations?/[A-Za-z0-9_-]{1,128}", exc.context):
            return "readback_throttled"
        if (getattr(exc, "code", None) == "image_request_rate_limited"
                and ImageTaskService._resume_failure_details(exc).get("source") == "upstream_http_conversation_throttle"):
            return "readback_throttled"
        for item in getattr(exc, "image_execution", None) or []:
            reason = item.get("selection_reason") if isinstance(item, dict) else None
            if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,80}", reason):
                return reason
        code = getattr(exc, "code", None)
        if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code):
            return code
        from curl_cffi.requests.exceptions import RequestException
        if isinstance(exc, (RequestException, UpstreamHTTPError)):
            return "readback_unavailable"
        if isinstance(exc, RuntimeError) and str(exc) == "original image account is busy; try resuming later":
            return "original_account_busy"
        if isinstance(exc, ValueError) and str(exc) == "original image account is no longer available":
            return "original_account_unavailable"
        return "recovery_interrupted"

    @staticmethod
    def _resume_failure_details(exc) -> dict:
        from curl_cffi.requests.exceptions import RequestException
        from services.image_quota import ImageQuotaExceeded, resolve_image_reset, resolve_retry_after
        from utils.helper import UpstreamHTTPError

        now = time.time()
        evidence = exc.record if isinstance(exc, ImageQuotaExceeded) else {}
        if not evidence:
            for item in getattr(exc, "image_execution", None) or []:
                candidate = item.get("image_quota_limit") if isinstance(item, dict) else None
                if isinstance(candidate, dict) and candidate.get("source") in _RECOVERY_FAILURE_SOURCES:
                    evidence = candidate
                    break
        source = evidence.get("source", "resume_poll")
        if isinstance(exc, UpstreamHTTPError):
            source = ("upstream_http_conversation_throttle" if exc.status_code == 429 and re.fullmatch(
                r"/backend-api/conversations?/[A-Za-z0-9_-]{1,128}", exc.context) else "upstream_http")
        elif isinstance(exc, RequestException):
            source = "transport"
        failure = {"source": source if source in _RECOVERY_FAILURE_SOURCES else "resume_poll",
                   "exception_type": type(exc).__name__}
        status = 429 if isinstance(exc, ImageQuotaExceeded) else getattr(exc, "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            failure["status_code"] = status
        retry_values = [resolve_image_reset(value, now=now) for value in (
            evidence.get("restore_at"), evidence.get("next_check_at"), getattr(exc, "retry_at", None),
            getattr(exc, "restore_at", None))]
        if getattr(exc, "retry_at", None) is None:
            retry_values.append(resolve_retry_after(getattr(exc, "retry_after", None), now=now))
        retry_values = [value for value in retry_values if value is not None]
        if retry_values:
            failure["retry_at"] = max(retry_values)
        return _validated_recovery_failure(failure)

    def _run_resume_poll(
        self, key: str, conversation_id: str, extra_timeout_secs: float,
        identity: dict[str, object], mode: str, model: str, *, auto_recovery: bool = False, lease=None,
    ) -> None:
        started = time.time()
        backend = None
        token = ""
        with self._lock:
            if lease is None:
                if key in self._resume_leases or key in self._origin_inflight:
                    return
                lease = object()
                self._resume_leases[key] = lease
            elif self._resume_leases.get(key) is not lease:
                return
            task = self._tasks.get(key)
            if not task or task.get("status") == TASK_STATUS_SUCCESS:
                if not auto_recovery:
                    self._release_recovery_lease(key, lease)
                return
            task = dict(task)
            if auto_recovery and (self._recovery_stop.is_set() or time.time() >= task["auto_recovery"]["deadline"]):
                self._set_recovery_error_locked(self._tasks[key], "completion_missing", "原会话确认期结束")
                self._save_locked()
                if not auto_recovery:
                    self._release_recovery_lease(key, lease)
                return
            self._update_task(key, status=TASK_STATUS_RUNNING)
        try:
            from services.openai_backend_api import OpenAIBackendAPI
            from services.protocol.conversation import ConversationRequest, collect_image_outputs, stream_image_outputs

            if auto_recovery:
                token = account_service.acquire_image_resume_token(task.get("resume_account_ref", ""),
                    wait_timeout_secs=2.0, conversation_id=conversation_id)
                # 当前账号池已维护最新token；短后台轮不能被全局刷新锁/长OAuth请求阻塞。
                active_token = token
            else:
                token = account_service.acquire_image_resume_token(task.get("resume_account_ref", ""), conversation_id=conversation_id)
                active_token = account_service.refresh_access_token(token, event="image_resume") or token
            backend = OpenAIBackendAPI(access_token=active_token)
            backend._image_auto_recovery = auto_recovery
            if auto_recovery:
                remaining = max(0.001, task["auto_recovery"]["deadline"] - time.time())
                backend._image_auto_deadline = time.monotonic() + remaining
                extra_timeout_secs = min(8.0, remaining)
            request = ConversationRequest(
                model=model, prompt=task.get("prompt", ""),
                response_format=task.get("response_format", "url"), base_url=task.get("base_url") or None,
                image_transport=task.get("image_transport") or config.image_transport,
                delivery_context=_validated_delivery_context(task.get("delivery_context")),
                delivery_checkpoint_callback=self.make_recovery_checkpoint(identity, task_id="resume",
                    existing_task_id=task["id"], mode=mode, model=model, base_url=task.get("base_url", ""), origin=False),
            )
            backend.image_transport = request.image_transport
            backend._image_resume_account_ref = task.get("resume_account_ref", "")
            result = collect_image_outputs(stream_image_outputs(
                backend, request, resume_conversation_id=conversation_id, poll_timeout_secs=extra_timeout_secs,
                resume_has_reference=mode == "edit", allow_terminal_recovery=task.get("image_transport") == "conversation",
            ))
            data = result.get("data")
            if not isinstance(data, list) or len(data) != 1:
                raise RuntimeError("原会话尚未交付唯一图片")
            with self._lock:
                if self._resume_leases.get(key) is not lease:
                    return
                current = self._tasks[key]
                current.update(status=TASK_STATUS_SUCCESS, data=data, error="",
                               updated_at=_now_iso(), updated_ts=time.time(), duration_ms=int((time.time() - started) * 1000))
                if valid_recovery_plan(current.get("auto_recovery")):
                    current["auto_recovery"].update(state="completed", last_reason="ready")
                self._save_locked()
            account_service.complete_image_generation_from_resume(active_token, conversation_id)
            account_service.complete_image_probe_from_resume(active_token, conversation_id)
            if auto_recovery:
                log_service.add("delivery", "图片后台恢复完成", {"event": "image_recovery_completed",
                    "task_id": task["id"], "conversation_id": conversation_id, "returned_n": 1})
            else:
                self._log_call(identity, mode, model, started, "调用完成（续轮询）", status="success",
                               urls=_collect_image_urls(data), conversation_id=conversation_id,
                               image_execution=result.get("_image_execution"))
        except Exception as exc:
            context = _validated_delivery_context(getattr(exc, "delivery_context", None))
            reason = self._resume_failure_reason(exc)
            failure = self._resume_failure_details(exc)
            with self._lock:
                if self._resume_leases.get(key) is not lease:
                    return
                current = self._tasks[key]
                if current.get("status") == TASK_STATUS_SUCCESS:
                    return
                if context:
                    original_submitted = current.get("delivery_context", {}).get("submitted_message_id")
                    if original_submitted and context["submitted_message_id"] != original_submitted:
                        reason = "submission_mismatch"
                    elif any(context[k]["conversation_id"] != conversation_id
                             for k in ("observed_evidence", "completion_receipt") if k in context):
                        reason = "conversation_mismatch"
                    else:
                        current["delivery_context"] = context
                current["recovery_failure"] = failure
                error_message = "原会话续查失败：" + reason
                self._set_recovery_error_locked(current, reason, error_message,
                    retry_at=failure.get("retry_at") if reason == "readback_throttled" else None)
                current.update(updated_at=_now_iso(), updated_ts=time.time(), duration_ms=int((time.time() - started) * 1000))
                self._save_locked()
                stopped = current.get("status") == TASK_STATUS_ERROR
            if auto_recovery:
                logger.info({"event": "image_recovery_round", "conversation_id": conversation_id,
                             "reason": reason, "stopped": stopped, "failure": failure})
                if stopped:
                    log_service.add("delivery", "图片后台恢复停止", {"event": "image_recovery_stopped",
                        "task_id": task["id"], "conversation_id": conversation_id, "reason": reason, "failure": failure})
            else:
                self._log_call(identity, mode, model, started, "调用失败（续轮询）", status="failed",
                               error=error_message, conversation_id=conversation_id,
                               image_execution=getattr(exc, "image_execution", None))
        finally:
            try:
                if backend is not None:
                    backend.close()
            finally:
                try:
                    if token:
                        account_service.release_image_slot(token, resume=True)
                finally:
                    if not auto_recovery:
                        self._release_recovery_lease(key, lease)


image_task_service = ImageTaskService(DATA_DIR / "image_tasks.json")
