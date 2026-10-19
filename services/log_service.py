from __future__ import annotations

import hashlib
import json
import itertools
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from services.config import DATA_DIR
from services.protocol.error_response import anthropic_error_response, openai_error_response
from utils.helper import anthropic_sse_stream, sse_json_stream

LOG_TYPE_CALL = "call"
LOG_TYPE_ACCOUNT = "account"
INTERNAL_RESPONSE_KEYS = {"_account_email", "_conversation_id", "_image_execution"}
DEFAULT_LOG_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 4
DEFAULT_IMAGE_WORKER_LIMIT = 40
REVERSE_READ_CHUNK_BYTES = 64 * 1024


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


class LogService:
    def __init__(
            self,
            path: Path,
            *,
            max_bytes: int = DEFAULT_LOG_MAX_BYTES,
            backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
    ):
        self.path = path
        self.max_bytes = max(1, int(max_bytes))
        self.backup_count = max(1, int(backup_count))
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _legacy_id(raw_line: str) -> str:
        # 旧日志没有 UUID。只基于内容生成稳定 ID，保证轮转后仍能删除。
        payload = raw_line.encode("utf-8", errors="ignore")
        return hashlib.sha1(payload).hexdigest()[:24]

    def _parse_line(self, raw_line: str) -> dict[str, Any] | None:
        try:
            item = json.loads(raw_line)
        except Exception:
            return None
        if not isinstance(item, dict):
            return None
        parsed = dict(item)
        parsed["id"] = str(parsed.get("id") or self._legacy_id(raw_line))
        # 历史日志的无时区时间来自 UTC 服务器；只转换展示，不改文件及稳定 ID。
        try:
            timestamp = datetime.fromisoformat(str(parsed.get("time") or "").replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            parsed["time"] = timestamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
            parsed["timezone"] = "Asia/Shanghai"
        except (ValueError, TypeError, OverflowError):
            pass
        return parsed

    @staticmethod
    def _serialize_item(item: dict[str, Any]) -> str:
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _matches_filters(item: dict[str, Any], *, type: str = "", start_date: str = "", end_date: str = "") -> bool:
        t = str(item.get("time") or "")
        day = t[:10]
        if type and item.get("type") != type:
            return False
        if start_date and day < start_date:
            return False
        if end_date and day > end_date:
            return False
        return True

    def _backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _log_paths_newest_first(self) -> list[Path]:
        paths = [self.path] if self.path.exists() else []
        paths.extend(
            path
            for index in range(1, self.backup_count + 1)
            if (path := self._backup_path(index)).exists()
        )
        return paths

    @staticmethod
    def _iter_lines_reverse(path: Path):
        """按行从文件尾部迭代，内存占用受读取块大小限制。"""
        with path.open("rb") as file:
            position = file.seek(0, os.SEEK_END)
            buffer = b""
            while position > 0:
                read_size = min(REVERSE_READ_CHUNK_BYTES, position)
                position -= read_size
                file.seek(position)
                buffer = file.read(read_size) + buffer
                lines = buffer.split(b"\n")
                buffer = lines[0]
                for raw_line in reversed(lines[1:]):
                    if raw_line:
                        yield raw_line.decode("utf-8", errors="replace")
            if buffer:
                yield buffer.decode("utf-8", errors="replace")

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        try:
            current_size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if current_size + incoming_bytes <= self.max_bytes:
            return

        oldest = self._backup_path(self.backup_count)
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backup_count - 1, 0, -1):
            source = self._backup_path(index)
            if source.exists():
                source.replace(self._backup_path(index + 1))
        self.path.replace(self._backup_path(1))

    def add(self, type: str, summary: str = "", detail: dict[str, Any] | None = None, **data: Any) -> None:
        item = {
            "id": uuid4().hex,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "type": type,
            "summary": summary,
            "detail": detail or data,
        }
        encoded = (self._serialize_item(item) + "\n").encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(encoded))
            with self.path.open("ab") as file:
                file.write(encoded)

    def list(self, type: str = "", start_date: str = "", end_date: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        items: list[dict[str, Any]] = []
        with self._lock:
            for path in self._log_paths_newest_first():
                for raw_line in self._iter_lines_reverse(path):
                    item = self._parse_line(raw_line)
                    if item is None:
                        continue
                    if not self._matches_filters(item, type=type, start_date=start_date, end_date=end_date):
                        continue
                    items.append(item)
                    if len(items) >= limit:
                        return items
        return items

    def delete(self, ids: list[str]) -> dict[str, int]:
        target_ids = {str(item or "").strip() for item in ids if str(item or "").strip()}
        if not target_ids:
            return {"removed": 0}
        removed = 0
        with self._lock:
            for path in self._log_paths_newest_first():
                temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
                removed_from_file = 0
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as source, temp_path.open(
                            "w", encoding="utf-8", newline="",
                    ) as target:
                        for raw_line in source:
                            item = self._parse_line(raw_line.rstrip("\r\n"))
                            if item is not None and str(item.get("id") or "") in target_ids:
                                removed_from_file += 1
                                continue
                            target.write(raw_line)
                        target.flush()
                        os.fsync(target.fileno())
                    if removed_from_file:
                        os.chmod(temp_path, stat.S_IMODE(path.stat().st_mode))
                        temp_path.replace(path)
                        removed += removed_from_file
                    else:
                        temp_path.unlink()
                except Exception:
                    temp_path.unlink(missing_ok=True)
                    raise
        return {"removed": removed}


log_service = LogService(
    DATA_DIR / "logs.jsonl",
    max_bytes=_positive_env_int("CHATGPT2API_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES),
    backup_count=_positive_env_int("CHATGPT2API_LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT),
)

_image_worker_limiter = None
_image_worker_limit = _positive_env_int("CHATGPT2API_IMAGE_WORKER_LIMIT", DEFAULT_IMAGE_WORKER_LIMIT)


def _get_image_worker_limiter():
    # 隔离 HTTP 传输线程；实际图片并发统一由 image_execution 的队列限制。
    global _image_worker_limiter
    if _image_worker_limiter is None:
        import anyio

        _image_worker_limiter = anyio.CapacityLimiter(_image_worker_limit)
    return _image_worker_limiter


def _collect_urls(value: object) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "url" and isinstance(item, str):
                urls.append(item)
            elif key == "urls" and isinstance(item, list):
                urls.extend(str(url) for url in item if isinstance(url, str))
            else:
                urls.extend(_collect_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_collect_urls(item))
    return urls


def _collect_account_emails(value: object) -> list[str]:
    emails: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"_account_email", "account_email"} and isinstance(item, str) and item.strip():
                emails.append(item.strip())
            else:
                emails.extend(_collect_account_emails(item))
    elif isinstance(value, list):
        for item in value:
            emails.extend(_collect_account_emails(item))
    return emails


def _collect_conversation_ids(value: object) -> list[str]:
    ids: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "_conversation_id" and isinstance(item, str) and item.strip():
                ids.append(item.strip())
            else:
                ids.extend(_collect_conversation_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.extend(_collect_conversation_ids(item))
    return ids


def _strip_internal_response_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_internal_response_fields(item)
            for key, item in value.items()
            if key not in INTERNAL_RESPONSE_KEYS
        }
    if isinstance(value, list):
        return [_strip_internal_response_fields(item) for item in value]
    return value


def _request_excerpt(text: object, limit: int = 1000) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _image_error_response(exc: Exception) -> JSONResponse:
    from services.protocol.conversation import public_image_error_message

    message = public_image_error_message(str(exc))
    if "no available image quota" in message.lower():
        return openai_error_response(
            {
                "error": {
                    "message": "no available image quota",
                    "type": "insufficient_quota",
                    "param": None,
                    "code": "insufficient_quota",
                }
            },
            429,
        )
    if hasattr(exc, "to_openai_error") and hasattr(exc, "status_code"):
        return JSONResponse(status_code=int(exc.status_code), content=exc.to_openai_error())
    return openai_error_response(message, 502)


def _protocol_error_response(exc: Exception, status_code: int, sse: str) -> JSONResponse:
    message = str(exc)
    if sse == "anthropic":
        return anthropic_error_response(message, status_code)
    return openai_error_response(message, status_code)


def _next_item(items):
    try:
        return True, next(items)
    except StopIteration:
        return False, None


@dataclass
class LoggedCall:
    identity: dict[str, object]
    endpoint: str
    model: str
    summary: str
    started: float = field(default_factory=time.time)
    request_text: str = ""
    request_shape: dict[str, int] | None = None
    queue_wait_ms: int | None = field(default=None, init=False)
    worker_duration_ms: int | None = field(default=None, init=False)

    async def run(self, handler, *args, sse: str = "openai"):
        from services.protocol.conversation import ImageGenerationError

        try:
            if self.endpoint.startswith("/v1/images"):
                import anyio

                queued_at = time.monotonic()

                def run_image_handler():
                    worker_started = time.monotonic()
                    self.queue_wait_ms = max(0, int((worker_started - queued_at) * 1000))
                    try:
                        return handler(*args)
                    finally:
                        self.worker_duration_ms = max(0, int((time.monotonic() - worker_started) * 1000))

                result = await anyio.to_thread.run_sync(
                    run_image_handler,
                    limiter=_get_image_worker_limiter(),
                )
            else:
                result = await run_in_threadpool(handler, *args)
        except ImageGenerationError as exc:
            self.log("调用失败", {"_image_execution": exc.image_execution}, status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""),
                     conversation_id=getattr(exc, "conversation_id", ""))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)

        if isinstance(result, dict):
            self.log("调用完成", result)
            return _strip_internal_response_fields(result)

        sender = anthropic_sse_stream if sse == "anthropic" else sse_json_stream
        try:
            if self.endpoint.startswith("/v1/images"):
                import anyio

                has_first, first = await anyio.to_thread.run_sync(
                    _next_item, result, limiter=_get_image_worker_limiter(),
                )
            else:
                has_first, first = await run_in_threadpool(_next_item, result)
        except ImageGenerationError as exc:
            self.log("调用失败", {"_image_execution": exc.image_execution}, status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""),
                     conversation_id=getattr(exc, "conversation_id", ""))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)
        if not has_first:
            self.log("流式调用结束")
            return StreamingResponse(sender(()), media_type="text/event-stream")
        chunks = sender(self.stream(itertools.chain([first], result)))
        if self.endpoint.startswith("/v1/images"):
            return StreamingResponse(self._image_stream(chunks), media_type="text/event-stream")
        return StreamingResponse(chunks, media_type="text/event-stream")

    async def _image_stream(self, chunks):
        import anyio

        try:
            while True:
                has_item, item = await anyio.to_thread.run_sync(
                    _next_item, chunks, limiter=_get_image_worker_limiter(),
                )
                if not has_item:
                    break
                yield item
        finally:
            close = getattr(chunks, "close", None)
            if close:
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(close, limiter=_get_image_worker_limiter())

    def stream(self, items):
        urls: list[str] = []
        account_emails: list[str] = []
        conversation_ids: list[str] = []
        executions: list[dict[str, Any]] = []
        failed = False
        try:
            for item in items:
                if isinstance(item, dict):
                    executions.extend(item.get("_image_execution") or [])
                urls.extend(_collect_urls(item))
                account_emails.extend(_collect_account_emails(item))
                conversation_ids.extend(_collect_conversation_ids(item))
                yield _strip_internal_response_fields(item)
        except Exception as exc:
            failed = True
            self.log(
                "流式调用失败",
                {"_image_execution": [*executions, *getattr(exc, "image_execution", [])]},
                status="failed",
                error=str(exc),
                urls=urls,
                account_email=(account_emails[0] if account_emails else getattr(exc, "account_email", "")),
                conversation_id=(conversation_ids[0] if conversation_ids else getattr(exc, "conversation_id", "")),
            )
            if self.endpoint.startswith("/v1/images") and not hasattr(exc, "to_openai_error"):
                from services.protocol.conversation import ImageGenerationError, public_image_error_message

                raise ImageGenerationError(public_image_error_message(str(exc))) from exc
            raise
        finally:
            if not failed:
                self.log("流式调用结束", {"_image_execution": executions}, urls=urls, account_email=account_emails[0] if account_emails else "",
                         conversation_id=conversation_ids[0] if conversation_ids else "")

    def log(self, suffix: str, result: object = None, status: str = "success", error: str = "",
            urls: list[str] | None = None, account_email: str = "", conversation_id: str = "") -> None:
        detail = {
            "key_id": self.identity.get("id"),
            "key_name": self.identity.get("name"),
            "role": self.identity.get("role"),
            "endpoint": self.endpoint,
            "model": self.model,
            "started_at": datetime.fromtimestamp(self.started).strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_ms": int((time.time() - self.started) * 1000),
            "status": status,
        }
        if self.queue_wait_ms is not None:
            detail["queue_wait_ms"] = self.queue_wait_ms
            detail["worker_duration_ms"] = self.worker_duration_ms
        request_excerpt = _request_excerpt(self.request_text)
        if request_excerpt:
            detail["request_text"] = request_excerpt
        if self.request_shape:
            detail["request_shape"] = self.request_shape
        if error:
            detail["error"] = error
        email = str(account_email or "").strip()
        if not email:
            emails = _collect_account_emails(result)
            email = emails[0] if emails else ""
        if email:
            detail["account_email"] = email
        conv_id = str(conversation_id or "").strip()
        if not conv_id:
            conv_ids = _collect_conversation_ids(result)
            conv_id = conv_ids[0] if conv_ids else ""
        if conv_id:
            detail["conversation_id"] = conv_id
        if isinstance(result, dict) and result.get("_image_execution"):
            detail["image_execution"] = result["_image_execution"]
        collected_urls = [*(urls or []), *_collect_urls(result)]
        if collected_urls and not self.endpoint.startswith("/v1/search"):
            detail["urls"] = list(dict.fromkeys(collected_urls))
        log_service.add(LOG_TYPE_CALL, f"{self.summary}{suffix}", detail)
