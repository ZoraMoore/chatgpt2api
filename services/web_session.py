"""校验并保存网页登录会话，不改变现有账号的调用与选号方式。"""
from __future__ import annotations

import re


def normalize_web_session_token(value: object) -> str:
    if value is None or value == "":
        return ""
    # 登录 Cookie 使用五段 JWE；限制字符和长度，避免保存不可用或含控制字符的凭据。
    if (not isinstance(value, str) or not 32 <= len(value) <= 16384
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value)):
        raise ValueError("invalid web session token")
    return value
