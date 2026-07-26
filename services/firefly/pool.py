"""Adobe 账号在 chatgpt2api 号池中的导入与统计。

Adobe 账号与 OpenAI 账号存在同一个 account_service 里，只靠 source_type="adobe" 区分，
因此号池管理页无需改动即可展示它们的状态、额度与生图数量。
"""

from __future__ import annotations

from typing import Any

from services.account_service import account_service
from services.firefly import SOURCE_TYPE
from services.firefly import auth as firefly_auth

# 导入时的默认额度：每成功出图一张扣 1，归零转「限流」，可在号池页随时调整。
DEFAULT_QUOTA = 100


def _iso_expires_at(access_token: str) -> str:
    return account_service._timestamp_to_iso(int(firefly_auth.token_expires_at(access_token)))


def build_account_payload(item: dict[str, Any]) -> dict[str, Any]:
    """校验一条 Adobe 凭证并组装成号池账号。

    凭证三选一（按优先级）：现成 access_token / device_token 换票 / SSO Cookie 换票。
    """
    access_token = str(item.get("access_token") or "").strip()
    device_token = str(item.get("device_token") or "").strip()
    device_id = str(item.get("device_id") or "").strip()
    session_cookies = str(item.get("session_cookies") or "").strip()
    client_id = str(item.get("client_id") or "").strip()
    proxy = str(item.get("proxy") or "").strip()
    arp_session_id = str(item.get("arp_session_id") or "").strip()
    probe_account = {"proxy": proxy}

    # x-arp-session-id 是生图请求头 x-arp-session-id 的值，必填。
    if not arp_session_id:
        raise ValueError("x-arp-session-id 是必填项")

    if device_token:
        # device_token 直接换出的是 Android access（scope 只有 creative_sdk），无法生图；
        # 必须桥接成带 firefly_api 的 Web access。桥接同时产出可续期的 SSO Cookie。
        bridged = firefly_auth.bridge_device_to_web_access(
            device_token, device_id=device_id, account=probe_account
        )
        access_token = bridged["access_token"]
        device_token = bridged.get("device_token") or device_token
        session_cookies = bridged.get("session_cookies") or session_cookies
        client_id = str(bridged.get("client_id") or client_id or "")
    elif not access_token and session_cookies:
        access_token = firefly_auth.refresh_with_cookies(
            session_cookies, client_id=client_id, account=probe_account
        )

    if not access_token:
        raise ValueError("需要 access_token、device_token 或 session_cookies 其中之一")

    profile = firefly_auth.fetch_profile(access_token, client_id=client_id, account=probe_account)
    has_scope, scope = firefly_auth.has_generate_scope(access_token)

    quota = item.get("quota")
    payload: dict[str, Any] = {
        "access_token": access_token,
        "source_type": SOURCE_TYPE,
        "type": str(profile.get("account_type") or "").strip() or "firefly",
        "email": str(profile.get("email") or item.get("email") or "").strip() or None,
        "user_id": str(profile.get("userId") or profile.get("authId") or "").strip() or None,
        "quota": DEFAULT_QUOTA if quota is None else max(0, int(quota)),
        "proxy": proxy,
        "scope": scope,
        "ims_expires_at": _iso_expires_at(access_token),
        "status": "正常" if has_scope else "异常",
    }
    if device_token:
        payload["device_token"] = device_token
    if device_id:
        payload["device_id"] = device_id
    if session_cookies:
        payload["session_cookies"] = session_cookies
    if client_id:
        payload["client_id"] = client_id
    if arp_session_id:
        payload["arp_session_id"] = arp_session_id
    if not has_scope:
        payload["last_refresh_error"] = "access_token 缺少 firefly_api / creative_production scope"
    return payload


def import_accounts(items: list[dict[str, Any]]) -> dict[str, Any]:
    """批量导入 Adobe 账号；单条失败不影响其余条目。"""
    payloads: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for index, item in enumerate(items):
        try:
            payloads.append(build_account_payload(item))
        except Exception as exc:
            errors.append({"index": str(index), "error": str(exc)})
    if not payloads:
        return {"added": 0, "skipped": 0, "errors": errors, "items": list_accounts()}
    result = account_service.add_account_items(payloads)
    return {
        "added": result.get("added", 0),
        "skipped": result.get("skipped", 0),
        "errors": errors,
        "items": list_accounts(),
    }


# 可以在导入后单独改的字段：凭证类三项 + 调度类三项。
EDITABLE_FIELDS = (
    "arp_session_id",
    "device_token",
    "session_cookies",
    "device_id",
    "client_id",
    "quota",
    "proxy",
    "status",
)


def update_credentials(access_token: str, updates: dict[str, Any]) -> dict[str, Any]:
    """更新一个已存在的 Adobe 账号（换 x-arp-session-id、补 device_token、改额度等）。

    传空字符串表示清空该字段；不传则保持不动。
    """
    token = str(access_token or "").strip()
    if not token:
        raise ValueError("access_token is required")
    account = account_service.get_account(token)
    if account is None:
        raise LookupError("account not found")
    if not _is_adobe(account):
        raise ValueError("不是 Adobe 账号，请在号池管理页直接编辑")

    changes: dict[str, Any] = {}
    for field in EDITABLE_FIELDS:
        if field not in updates:
            continue
        value = updates[field]
        if field == "quota":
            changes["quota"] = max(0, int(value or 0))
        else:
            text = str(value or "").strip()
            # x-arp-session-id 必填，不允许改成空值。
            if field == "arp_session_id" and not text:
                raise ValueError("x-arp-session-id 是必填项，不能清空")
            changes[field] = text
    if not changes:
        raise ValueError("没有需要更新的字段")

    updated = account_service.update_account(token, changes, quiet=True)
    if updated is None:
        raise LookupError("account not found")
    return _sanitize(updated)


def list_accounts() -> list[dict[str, Any]]:
    """号池里的 Adobe 账号（凭证字段已脱敏）。"""
    return [_sanitize(account) for account in account_service.list_accounts() if _is_adobe(account)]


def _is_adobe(account: dict[str, Any]) -> bool:
    return account_service._normalize_source_type(account.get("source_type")) == SOURCE_TYPE


def _sanitize(account: dict[str, Any]) -> dict[str, Any]:
    hidden = {"device_token", "session_cookies", "arp_session_id", "password", "refresh_token", "id_token"}
    sanitized = {key: value for key, value in account.items() if key not in hidden}
    arp_session_id = str(account.get("arp_session_id") or "").strip()
    sanitized["has_device_token"] = bool(str(account.get("device_token") or "").strip())
    sanitized["has_session_cookies"] = bool(str(account.get("session_cookies") or "").strip())
    sanitized["has_arp_session_id"] = bool(arp_session_id)
    # 只回前 16 位，够在界面上区分是哪一份，又不至于把整包风控指纹抛出去。
    sanitized["arp_session_id_preview"] = arp_session_id[:16] if arp_session_id else ""
    return sanitized


def stats() -> dict[str, Any]:
    """Adobe 分池统计：账号数、状态分布、剩余额度与累计生图数量。"""
    items = [account for account in account_service.list_accounts() if _is_adobe(account)]
    return {
        "total": len(items),
        "active": sum(1 for item in items if item.get("status") == "正常"),
        "limited": sum(1 for item in items if item.get("status") == "限流"),
        "abnormal": sum(1 for item in items if item.get("status") == "异常"),
        "disabled": sum(1 for item in items if item.get("status") == "禁用"),
        "total_quota": sum(max(0, int(item.get("quota") or 0)) for item in items if item.get("status") == "正常"),
        "total_success": sum(int(item.get("success") or 0) for item in items),
        "total_fail": sum(int(item.get("fail") or 0) for item in items),
        "inflight": sum(int(item.get("image_inflight") or 0) for item in items),
    }
