"""Adobe IMS 凭证解析与续期。

Firefly 没有 OAuth refresh_token，续期只有两条路：
1. device_token（FireflyAndroid App 长期凭证）→ POST /ims/token/v4 换新 access_token；
2. Adobe SSO Cookie → POST /ims/check/v6/token 换新 access_token。

两者都没有时，token 过期后只能由管理员重新授权。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from services.firefly.constants import (
    ANDROID_CLIENT_ID,
    ANDROID_CLIENT_SECRET,
    ANDROID_REDIRECT_URI,
    DEFAULT_CLIENT_ID,
    DEFAULT_IMPERSONATE,
    DEFAULT_REDIRECT_URI,
    DEFAULT_REQUEST_TIMEOUT_SECS,
    DEFAULT_SCOPES,
    IMS_JUMPTOKEN_URL,
    IMS_PROFILE_URL,
    IMS_TOKEN_CHECK_URL,
    IMS_TOKEN_V4_URL,
    WEB_ORIGIN,
    WEB_REFERER,
)


class FireflyAuthError(Exception):
    """凭证无效或续期失败。"""


class FireflyReauthRequired(FireflyAuthError):
    """已无法免浏览器续期，需要管理员重新授权。"""


def _session(account: dict | None = None):
    from curl_cffi import requests

    from services.proxy_service import proxy_settings

    kwargs = proxy_settings.build_session_kwargs(
        account=account,
        impersonate=DEFAULT_IMPERSONATE,
        verify=True,
    )
    return requests.Session(**kwargs)


def _truncate(text: object, limit: int = 300) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "..."


def parse_access_token_claims(access_token: str) -> dict[str, Any]:
    """解析 IMS access_token(JWT) 的 payload；不校验签名（由 Adobe 侧完成）。"""
    parts = str(access_token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def token_expires_at(access_token: str) -> float:
    """返回过期时间戳（秒）。

    IMS 的 created_at / expires_in 都是毫秒字符串；解析失败时回退为「当前 + 12 小时」，
    避免把 token 误判为永不过期。
    """
    claims = parse_access_token_claims(access_token)
    try:
        created_ms = int(str(claims.get("created_at") or "0"))
        expires_ms = int(str(claims.get("expires_in") or "0"))
    except (TypeError, ValueError):
        return time.time() + 12 * 3600
    if created_ms <= 0 or expires_ms <= 0:
        return time.time() + 12 * 3600
    return (created_ms + expires_ms) / 1000.0


def token_expired(access_token: str, leeway_secs: float = 0.0) -> bool:
    return time.time() + max(0.0, leeway_secs) >= token_expires_at(access_token)


def has_generate_scope(access_token: str) -> tuple[bool, str]:
    """判断 token 是否具备生图 scope。

    device_token 直接换出的 token 常常只有 creative_sdk，会在 generate-async 返回 403。
    """
    scope = str(parse_access_token_claims(access_token).get("scope") or "")
    lower = scope.lower()
    return ("firefly_api" in lower or "creative_production" in lower), scope


def fetch_profile(access_token: str, client_id: str = "", account: dict | None = None) -> dict[str, Any]:
    """用 Bearer 调 /ims/profile 校验 token 并读取用户信息。"""
    if not str(access_token or "").strip():
        raise FireflyAuthError("firefly: access_token is required")
    session = _session(account)
    try:
        response = session.get(
            IMS_PROFILE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "x-api-key": client_id or DEFAULT_CLIENT_ID,
                "Accept": "application/json",
            },
            timeout=DEFAULT_REQUEST_TIMEOUT_SECS,
        )
        if response.status_code != 200:
            raise FireflyAuthError(
                f"firefly: profile check failed: status={response.status_code} body={_truncate(response.text, 200)}"
            )
        data = response.json() if response.text else {}
        if not isinstance(data, dict):
            raise FireflyAuthError("firefly: unexpected profile response")
        return data
    finally:
        session.close()


def _parse_expires_in_seconds(value: object) -> int:
    """IMS 的 expires_in 可能是秒或毫秒、数字或字符串，统一成秒。"""
    try:
        number = float(str(value or "0"))
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    return int(number / 1000) if number > 1e7 else int(number)


def _device_exchange(
    session: Any,
    device_token: str,
    device_id: str = "",
    client_id: str = "",
    client_secret: str = "",
) -> dict[str, Any]:
    """device_token → Android access_token（桥接第 1 步）。在给定 session 上执行。"""
    form = {
        "grant_type": "device",
        "client_id": client_id or ANDROID_CLIENT_ID,
        "client_secret": client_secret or ANDROID_CLIENT_SECRET,
        "device_token": device_token,
        "redirect_uri": ANDROID_REDIRECT_URI,
    }
    if str(device_id or "").strip():
        form["device_id"] = str(device_id).strip()

    response = session.post(
        IMS_TOKEN_V4_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data=form,
        timeout=DEFAULT_REQUEST_TIMEOUT_SECS,
    )
    if response.status_code != 200:
        raise FireflyReauthRequired(
            f"firefly: device_token exchange failed: status={response.status_code} "
            f"body={_truncate(response.text)}"
        )
    data = response.json() if response.text else {}
    if not isinstance(data, dict) or not str(data.get("access_token") or "").strip():
        raise FireflyReauthRequired("firefly: device_token exchange returned empty access_token")

    access_token = str(data["access_token"]).strip()
    claims = parse_access_token_claims(access_token)
    return {
        "access_token": access_token,
        "device_token": str(data.get("device_token") or device_token).strip(),
        "token_type": str(data.get("token_type") or "bearer"),
        "expires_in": _parse_expires_in_seconds(data.get("expires_in")),
        "email": str(data.get("email") or "").strip(),
        "display_name": str(data.get("displayName") or "").strip(),
        "user_id": str(data.get("userId") or data.get("authId") or claims.get("user_id") or "").strip(),
        "client_id": str(data.get("client_id") or claims.get("client_id") or client_id or ANDROID_CLIENT_ID).strip(),
        "scope": str(claims.get("scope") or ""),
        "expires_at": token_expires_at(access_token),
    }


def _ims_check(
    session: Any,
    session_cookies: str,
    client_id: str = "",
    scopes: tuple[str, ...] | list[str] | None = None,
) -> str:
    """凭 SSO Cookie 调 /ims/check/v6/token 换 Web access_token。在给定 session 上执行。"""
    response = session.post(
        IMS_TOKEN_CHECK_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Cookie": session_cookies,
            # WebView /ims/check 要求合法 Origin/Referer，否则 invalid_credentials。
            "Origin": WEB_ORIGIN,
            "Referer": WEB_REFERER,
            "Accept": "application/json",
        },
        data={
            "guest_allowed": "true",
            "client_id": client_id or DEFAULT_CLIENT_ID,
            "scope": ",".join(scopes or DEFAULT_SCOPES),
        },
        timeout=DEFAULT_REQUEST_TIMEOUT_SECS,
    )
    if response.status_code != 200:
        raise FireflyReauthRequired(
            f"firefly: token check failed: status={response.status_code} body={_truncate(response.text, 200)}"
        )
    data = response.json() if response.text else {}
    if not isinstance(data, dict):
        raise FireflyReauthRequired("firefly: unexpected token check response")
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        detail = str(data.get("error") or "") or _truncate(json.dumps(data, ensure_ascii=False), 200)
        raise FireflyReauthRequired(f"firefly: token check returned empty access_token: {detail}")
    return access_token


def refresh_with_device_token(
    device_token: str,
    device_id: str = "",
    client_id: str = "",
    client_secret: str = "",
    account: dict | None = None,
) -> dict[str, Any]:
    """用 App 长期 device_token 换取 Android access_token（仅换票，不桥接）。

    抓包确认（FireflyAndroid）：
        POST /ims/token/v4
        grant_type=device&client_id=FireflyAndroid&client_secret=...&device_token=...

    注意：换出的是 Android access（scope 只有 creative_sdk），**不能直接生图**。
    生图凭证请用 bridge_device_to_web_access。
    """
    device_token = str(device_token or "").strip()
    if not device_token:
        raise FireflyReauthRequired("firefly: device_token required")
    session = _session(account)
    try:
        return _device_exchange(session, device_token, device_id, client_id, client_secret)
    finally:
        session.close()


def refresh_with_cookies(
    session_cookies: str,
    client_id: str = "",
    scopes: tuple[str, ...] | list[str] | None = None,
    account: dict | None = None,
) -> str:
    """凭 Adobe SSO Cookie 调 /ims/check/v6/token 续期，返回新的 access_token。"""
    cookies = str(session_cookies or "").strip()
    if not cookies:
        raise FireflyReauthRequired("firefly: no session cookies for refresh")
    session = _session(account)
    try:
        return _ims_check(session, cookies, client_id, scopes)
    finally:
        session.close()


def _request_jump_url(session: Any, android_access_token: str) -> str:
    """用 Android access 换取跳转到 Web client 的 jump URL（桥接第 2 步）。"""
    params = {
        "target_client_id": DEFAULT_CLIENT_ID,
        "client_id": ANDROID_CLIENT_ID,
        "target_remember_me": "true",
        "target_redirect_uri": DEFAULT_REDIRECT_URI + "?locale=en",
    }
    response = session.post(
        IMS_JUMPTOKEN_URL,
        params=params,
        headers={
            "Authorization": f"Bearer {android_access_token}",
            "Accept": "application/json",
        },
        allow_redirects=False,
        timeout=DEFAULT_REQUEST_TIMEOUT_SECS,
    )
    if response.status_code != 200:
        raise FireflyReauthRequired(
            f"firefly: jumptoken failed: status={response.status_code} body={_truncate(response.text)}"
        )
    data = response.json() if response.text else {}
    jump = str((data or {}).get("jump") or "").strip() if isinstance(data, dict) else ""
    if not jump:
        raise FireflyReauthRequired("firefly: jumptoken returned empty jump url")
    return jump


def _follow_jump_for_cookies(session: Any, jump_url: str) -> str:
    """GET jump URL（302）让 Adobe 写入 SSO Cookie，拼成 Cookie 头返回（桥接第 3 步）。"""
    response = session.get(
        jump_url,
        headers={
            "Accept": "*/*",
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36"
            ),
        },
        allow_redirects=False,
        timeout=DEFAULT_REQUEST_TIMEOUT_SECS,
    )
    if response.status_code not in (200, 301, 302, 303, 307, 308):
        raise FireflyReauthRequired(f"firefly: jump GET unexpected status={response.status_code}")

    # curl_cffi 的 session 会把 Set-Cookie 累积进 session.cookies；直接全量拼成 Cookie 头，
    # 比按域名匹配更稳（抓包里关键是 ims_sid / aux_sid）。
    pairs: list[str] = []
    seen: set[str] = set()
    try:
        for name, value in session.cookies.items():
            if name and name not in seen:
                seen.add(name)
                pairs.append(f"{name}={value}")
    except Exception:
        for name, value in (session.cookies.get_dict() or {}).items():
            if name and name not in seen:
                seen.add(name)
                pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def bridge_device_to_web_access(
    device_token: str,
    device_id: str = "",
    account: dict | None = None,
) -> dict[str, Any]:
    """把 App device_token 桥成可生图的 Web access_token（含 firefly_api scope）。

    抓包确认（FireflyAndroid，2026-07-10）：
      1. device_token → Android access（/ims/token/v4 grant_type=device）
      2. Bearer Android AT → POST /ims/jumptoken/v1?target_client_id=clio-playground-web
      3. GET jump URL（302）→ 写入 ims_sid / aux_sid 等 SSO Cookie
      4. Cookie → POST /ims/check/v6/token（client_id=clio-playground-web）→ 含 firefly_api 的 Web access

    纯 Android access（creative_sdk）无法调 firefly-3p 生图端点，因此必须走这条桥。
    """
    device_token = str(device_token or "").strip()
    if not device_token:
        raise FireflyReauthRequired("firefly: device_token required for bridge")

    session = _session(account)
    try:
        android = _device_exchange(session, device_token, device_id)
        jump_url = _request_jump_url(session, android["access_token"])
        cookies = _follow_jump_for_cookies(session, jump_url)
        if not cookies:
            raise FireflyReauthRequired("firefly: bridge got empty SSO cookies after jump")
        web_access_token = _ims_check(session, cookies, DEFAULT_CLIENT_ID, DEFAULT_SCOPES)
    finally:
        session.close()

    claims = parse_access_token_claims(web_access_token)
    return {
        "access_token": web_access_token,
        "android_access_token": android["access_token"],
        "device_token": android.get("device_token") or device_token,
        "session_cookies": cookies,
        "client_id": str(claims.get("client_id") or DEFAULT_CLIENT_ID),
        "scope": str(claims.get("scope") or ""),
        "user_id": str(claims.get("user_id") or android.get("user_id") or "").strip(),
        "email": android.get("email") or "",
        "display_name": android.get("display_name") or "",
        "expires_at": token_expires_at(web_access_token),
    }
