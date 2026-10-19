"""通过本机隔离浏览器计算网页校验，认证请求仍由原账号 HTTP 会话发送。"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import time


class BrowserSentinelError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__("网页会话校验未完成，请稍后重试或更新登录会话。")


def browser_sentinel_enabled() -> bool:
    return bool(os.environ.get("CHATGPT2API_SENTINEL_SOCKET"))


def normalize_web_session_token(value: object) -> str:
    if value is None or value == "":
        return ""
    if (not isinstance(value, str) or not 32 <= len(value) <= 16384
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value)):
        raise ValueError("invalid web session token")
    return value


def account_web_session_token(account: dict) -> str:
    return normalize_web_session_token(account.get("web_session_token") or account.get("sessionToken"))


def has_web_session(account: dict) -> bool:
    try:
        return bool(account_web_session_token(account))
    except ValueError:
        return False


def apply_web_session_cookies(session, token: str) -> None:
    token = normalize_web_session_token(token)
    if not token:
        return
    name = "__Secure-next-auth.session-token"
    # 与浏览器 Cookie 的单条容量一致；不把长会话拼成浏览器无法保存的一条 Cookie。
    chunks = [token[index:index + 3933] for index in range(0, len(token), 3933)]
    for index, chunk in enumerate(chunks):
        cookie_name = name if len(chunks) == 1 else f"{name}.{index}"
        session.cookies.set(cookie_name, chunk, domain="chatgpt.com", path="/")


def _validated_header_token(value: object, *, required: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > 131072 or not value.isascii()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or (required and (not value or value.isdecimal()))):
        raise BrowserSentinelError("invalid_sdk_result")
    return value


class BrowserSentinelContext:
    def __init__(self, *, html: str, user_agent: str, device_id: str, timezone: str):
        self.path = os.environ.get("CHATGPT2API_SENTINEL_SOCKET", "")
        if (not self.path.startswith("/") or len(self.path.encode()) > 104
                or any(ord(char) < 32 for char in self.path)):
            raise BrowserSentinelError("invalid_socket_path")
        if not isinstance(html, str) or len(html.encode("utf-8")) > 2 * 1024 * 1024:
            raise BrowserSentinelError("invalid_input")
        self.input = {"html": html, "user_agent": user_agent, "device_id": device_id, "timezone": timezone}
        self.ticket = ""
        self.p = ""

    def _request(self, path: str, payload: dict, timeout: float = 35) -> dict:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > 4 * 1024 * 1024:
            raise BrowserSentinelError("invalid_input")
        connection = http.client.HTTPConnection("localhost", timeout=timeout)
        try:
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(timeout)
            connection.sock.connect(self.path)
            connection.request("POST", path, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read(512 * 1024 + 1)
            if len(data) > 512 * 1024:
                raise BrowserSentinelError("invalid_sdk_result")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise BrowserSentinelError("invalid_sdk_result")
            if response.status != 200:
                raise BrowserSentinelError("worker_busy" if response.status == 503 else "worker_failed")
            return value
        except BrowserSentinelError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise BrowserSentinelError("worker_unavailable") from None
        finally:
            connection.close()

    def __enter__(self):
        deadline = time.monotonic() + 10
        while True:
            try:
                result = self._request("/v1/start", self.input)
                break
            except BrowserSentinelError as error:
                # 只在工作进程明确尚未创建上下文时排队，不重试任何生成 POST。
                if error.code != "worker_busy" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        ticket = result.get("ticket")
        if not isinstance(ticket, str) or not re.fullmatch(r"[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12}", ticket):
            raise BrowserSentinelError("invalid_sdk_result")
        self.ticket = ticket
        try:
            self.p = _validated_header_token(result.get("p"), required=True)
            if not self.p.startswith("gAAAAAC") or len(self.p) > 16384:
                raise BrowserSentinelError("invalid_sdk_result")
        except BrowserSentinelError:
            self.__exit__(None, None, None)
            raise
        self.input = {}
        return self

    def solve(self, challenge: dict) -> tuple[str, str]:
        result = self._request("/v1/solve", {"ticket": self.ticket, "challenge": challenge})
        proof_required = bool((challenge.get("proofofwork") or {}).get("required"))
        turnstile_required = bool((challenge.get("turnstile") or {}).get("required"))
        proof = _validated_header_token(result.get("proof"), required=proof_required)
        turnstile = _validated_header_token(result.get("turnstile"), required=turnstile_required)
        if proof_required and not (proof.startswith("gAAAAAB") and proof.endswith("~S")):
            raise BrowserSentinelError("invalid_sdk_result")
        return proof, turnstile

    def __exit__(self, *_):
        if self.ticket:
            try:
                self._request("/v1/release", {"ticket": self.ticket}, timeout=2)
            except BrowserSentinelError:
                pass
            self.ticket = ""
