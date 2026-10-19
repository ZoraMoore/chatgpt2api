"""构造版本一致的浏览器标识，并按账号稳定复用设备 ID。"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid

from curl_cffi.requests.impersonate import BrowserType, DEFAULT_CHROME


def _uuid(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def browser_fingerprint(account: dict, access_token: str, namespace: str = "chatgpt2api") -> dict[str, str]:
    if not isinstance(access_token, str) or len(access_token) > 65536:
        raise ValueError("invalid browser credential")
    if not isinstance(namespace, str) or len(namespace) > 512:
        namespace = "chatgpt2api"
    raw = account.get("fp")
    raw = dict(raw) if isinstance(raw, dict) else {}
    for key in ("user-agent", "impersonate", "oai-device-id", "oai-session-id"):
        if isinstance(account.get(key), str) and account[key]:
            raw[key] = account[key]
    profile = raw.get("impersonate", DEFAULT_CHROME)
    if profile == "chrome":
        profile = DEFAULT_CHROME
    supported = {item.value for item in BrowserType}
    if not isinstance(profile, str) or profile not in supported or not re.fullmatch(r"chrome\d+[a-z]?", profile):
        profile = DEFAULT_CHROME
    major = re.search(r"\d+", profile).group(0)
    agent = raw.get("user-agent")
    if (not isinstance(agent, str) or len(agent) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in agent)
            or not re.search(r"Chrome/" + major + r"\.", agent) or "Edg/" in agent or "Android" in agent):
        agent = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
    platform = "macOS" if "Macintosh" in agent else "Linux" if "Linux" in agent else "Windows"
    identity = str(account.get("user_id") or account.get("email") or "")[:512]
    account_id = ""
    try:
        claims = json.loads(base64.urlsafe_b64decode(access_token.split(".")[1] + "==="))
        auth = claims.get("https://api.openai.com/auth") or {}
        account_id = str(auth.get("chatgpt_account_id") or "")[:256]
        identity = str(auth.get("chatgpt_user_id") or auth.get("user_id") or identity)[:512]
    except (ValueError, IndexError, TypeError, AttributeError):
        pass
    seed = hashlib.sha256((account_id + "\0" + (identity or access_token)).encode()).hexdigest()
    # 无持久化设备信息的旧账号也保持稳定；不必重写含令牌的账号文件。
    device = _uuid(raw.get("oai-device-id")) or str(uuid.uuid5(uuid.NAMESPACE_URL, namespace + "/device/" + seed))
    session = _uuid(raw.get("oai-session-id")) or str(uuid.uuid5(uuid.NAMESPACE_URL, namespace + "/session/" + seed))
    return {"user-agent": agent, "impersonate": profile, "oai-device-id": device, "oai-session-id": session,
            "sec-ch-ua": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="99"',
            "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": json.dumps(platform),
            "navigator-platform": "MacIntel" if platform == "macOS" else "Linux x86_64" if platform == "Linux" else "Win32"}
