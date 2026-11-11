"""管理员 TOTP 密钥只在服务器端解密，不进入接口响应或日志。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import time
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet, InvalidToken


def normalize_totp_secret(value: object) -> str:
    if not isinstance(value, str) or not 16 <= len(value) <= 2048:
        raise ValueError("2FA 密钥格式无效")
    if value.lower().startswith("otpauth://"):
        uri = urlsplit(value)
        params = parse_qs(uri.query)
        if (uri.netloc != "totp" or params.get("algorithm", ["SHA1"])[0].upper() != "SHA1"
                or params.get("digits", ["6"])[0] != "6" or params.get("period", ["30"])[0] != "30"):
            raise ValueError("仅支持六位、30 秒周期的 TOTP 密钥")
        value = params.get("secret", [""])[0]
    secret = re.sub(r"[\s-]", "", value).upper().rstrip("=")
    if not re.fullmatch(r"[A-Z2-7]{16,128}", secret):
        raise ValueError("2FA 密钥格式无效")
    try:
        base64.b32decode(secret + "=" * (-len(secret) % 8))
    except Exception:
        raise ValueError("2FA 密钥格式无效") from None
    return secret


def totp_code(secret: str, now: float) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(now // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
    return f"{number % 1000000:06d}"


class TOTPSecretStore:
    def __init__(self, path: Path, *, clock=time.time, sleep=time.sleep):
        self.path = path
        self.clock = clock
        self.sleep = sleep
        self._lock = Lock()
        self._key_lock = Lock()
        self._last_steps: dict[str, int] = {}

    @staticmethod
    def _context(server: dict) -> list[str]:
        raw = str(server.get("base_url") or "").strip().rstrip("/")
        if len(raw) > 2048 or not raw.isprintable():
            raise ValueError("自动二次验证的源站地址格式无效")
        uri = urlsplit(raw)
        if uri.scheme != "https" or not uri.hostname or uri.username or uri.password or uri.query or uri.fragment:
            raise ValueError("自动二次验证必须使用有效的 HTTPS 源站地址")
        return [str(server.get("id") or ""), raw, str(server.get("email") or "").strip().lower()]

    def _cipher(self) -> Fernet:
        with self._key_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
            self.path.chmod(0o600)
            return Fernet(self.path.read_bytes())

    def seal(self, secret: str, server: dict) -> str:
        payload = {"context": self._context(server), "secret": normalize_totp_secret(secret)}
        return self._cipher().encrypt(json.dumps(payload).encode()).decode()

    def open(self, encrypted: str, server: dict) -> str:
        try:
            payload = json.loads(self._cipher().decrypt(encrypted.encode()))
            if payload.get("context") != self._context(server):
                raise ValueError()
            return normalize_totp_secret(payload["secret"])
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise ValueError("2FA 密钥不可用，请重新配置") from None

    def next_code(self, server: dict) -> str:
        secret = self.open(str(server.get("totp_secret_encrypted") or ""), server)
        identity = str(server.get("id") or "")
        with self._lock:
            now = self.clock()
            previous = self._last_steps.get(identity, -1)
            # 登录和敏感导出使用不同窗口，避免源站拒绝已使用的验证码。
            if int(now // 30) <= previous:
                self.sleep(max(0, (previous + 1) * 30 + 1 - now))
                now = self.clock()
            if now % 30 > 26:
                self.sleep(31 - now % 30)
                now = self.clock()
            self._last_steps[identity] = int(now // 30)
            return totp_code(secret, now)
