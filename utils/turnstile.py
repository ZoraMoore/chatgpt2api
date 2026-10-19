"""执行 Sentinel 寄存器程序，保持 JavaScript 类型、队列和异步语义。"""
from __future__ import annotations

import base64
import binascii
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TurnstileChallengeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Upstream session verification failed. Please try again later.")


def _xor_string(text: str, key: str) -> str:
    if not key:
        return text
    return "".join(chr(ord(char) ^ ord(key[index % len(key)])) for index, char in enumerate(text))


def _validate_program(program: object) -> None:
    if not isinstance(program, list) or not program or len(program) > 10000:
        raise TurnstileChallengeError("invalid_program")
    pending = [(program, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > 100000 or depth > 64:
            raise TurnstileChallengeError("program_limit")
        if isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
        elif isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, float) and not math.isfinite(value):
            raise TurnstileChallengeError("invalid_number")


def solve_turnstile_token(dx: str, p: str, *, context: dict[str, Any] | None = None,
                          strict: bool = False) -> str | None:
    try:
        if (not isinstance(dx, str) or not 1 <= len(dx) <= 1048576
                or not isinstance(p, str) or not 1 <= len(p) <= 16384 or not p.isascii()):
            raise TurnstileChallengeError("invalid_input")
        decoded = base64.b64decode(dx, validate=True).decode("latin1")
        program = json.loads(_xor_string(decoded, p))
        _validate_program(program)
        browser = dict(context or {})
        zone = browser.get("timezone", "UTC")
        if not isinstance(zone, str) or not 1 <= len(zone) <= 128:
            raise TurnstileChallengeError("invalid_timezone")
        ZoneInfo(zone)
        encoded = json.dumps({"program": json.dumps(program, separators=(",", ":")),
                              "key": p, "context": browser}, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
            raise TurnstileChallengeError("input_limit")
        # 仅子进程设置 TZ，避免修改多账号服务的全局时区或继承认证环境变量。
        completed = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).with_name("turnstile_worker.py"))],
            input=encoded, text=True, capture_output=True, timeout=1.5,
            env={"TZ": zone, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if completed.returncode != 0 or len(completed.stdout) > 200000:
            raise TurnstileChallengeError("worker_failed")
        result = json.loads(completed.stdout)
        token = result.get("token")
        if not isinstance(token, str) or not 1 <= len(token) <= 131072:
            reason = result.get("reason")
            allowed = {"rejected", "no_result", "execution_limit", "queue_limit", "invalid_instruction",
                       "unsupported_opcode", "call_limit", "execution_failed", "worker_failed"}
            raise TurnstileChallengeError(reason if reason in allowed else "invalid_result")
        base64.b64decode(token, validate=True)
        return token
    except TurnstileChallengeError:
        if strict:
            raise
    except subprocess.TimeoutExpired:
        if strict:
            raise TurnstileChallengeError("execution_limit") from None
    except (ValueError, TypeError, OSError, RecursionError, binascii.Error, ZoneInfoNotFoundError):
        if strict:
            raise TurnstileChallengeError("invalid_input") from None
    return None
