"""Sub2API integration for browsing and importing ChatGPT OAuth accounts from a sub2api admin."""

from __future__ import annotations

import json
import os
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock

from curl_cffi.requests import Session

from services.account_service import account_service
from services.config import DATA_DIR
from services.sub2api_totp import TOTPSecretStore
from services.sub2api_session_service import (
    Sub2APISessionError,
    get_compatible_admin_response,
    sub2api_session_manager,
)


SUB2API_CONFIG_FILE = DATA_DIR / "sub2api_config.json"
REMOTE_OPERATION_LOCK = RLock()

def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalize_import_job(raw: object, *, fail_unfinished: bool) -> dict | None:
    if not isinstance(raw, dict):
        return None
    status = _clean(raw.get("status")) or "failed"
    if fail_unfinished and status in {"pending", "running"}:
        status = "failed"
    return {
        "job_id": _clean(raw.get("job_id")) or uuid.uuid4().hex,
        "status": status,
        "created_at": _clean(raw.get("created_at")) or _now_iso(),
        "updated_at": _clean(raw.get("updated_at")) or _clean(raw.get("created_at")) or _now_iso(),
        "total": int(raw.get("total") or 0),
        "completed": int(raw.get("completed") or 0),
        "added": int(raw.get("added") or 0),
        "skipped": int(raw.get("skipped") or 0),
        "refreshed": int(raw.get("refreshed") or 0),
        "failed": int(raw.get("failed") or 0),
        "errors": raw.get("errors") if isinstance(raw.get("errors"), list) else [],
    }


def _normalize_server(raw: dict) -> dict:
    return {
        "id": _clean(raw.get("id")) or _new_id(),
        "name": _clean(raw.get("name")),
        "base_url": _clean(raw.get("base_url")),
        "email": _clean(raw.get("email")),
        "password": _clean(raw.get("password")),
        "api_key": _clean(raw.get("api_key")),
        "group_id": _clean(raw.get("group_id")),
        "totp_secret_encrypted": _clean(raw.get("totp_secret_encrypted")),
        "auto_sync_enabled": raw.get("auto_sync_enabled") is True,
        "sync_interval_minutes": max(1, min(1440, int(raw.get("sync_interval_minutes") or 5))),
        "last_sync_at": _clean(raw.get("last_sync_at")),
        "last_sync_error": _clean(raw.get("last_sync_error")),
        "next_sync_at": float(raw.get("next_sync_at") or 0),
        "sync_count": int(raw.get("sync_count") or 0),
        "revision": int(raw.get("revision") or 0),
        "import_job": _normalize_import_job(raw.get("import_job"), fail_unfinished=True),
    }


class Sub2APIConfig:
    def __init__(self, store_file: Path):
        self._store_file = store_file
        self._lock = Lock()
        self.totp = TOTPSecretStore(store_file.with_name("sub2api_totp.key"))
        self._servers: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self._store_file.exists():
            return []
        try:
            raw = json.loads(self._store_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [_normalize_server(item) for item in raw if isinstance(item, dict)]
        except Exception:
            pass
        return []

    def _save(self) -> None:
        self._store_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._store_file.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(self._servers, ensure_ascii=False, indent=2) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, self._store_file)

    def list_servers(self) -> list[dict]:
        with self._lock:
            return [dict(server) for server in self._servers]

    def get_server(self, server_id: str) -> dict | None:
        with self._lock:
            for server in self._servers:
                if server["id"] == server_id:
                    return dict(server)
        return None

    def add_server(
        self,
        *,
        name: str,
        base_url: str,
        email: str,
        password: str,
        api_key: str,
        group_id: str = "",
        totp_secret: str = "",
        auto_sync_enabled: bool = False,
        sync_interval_minutes: int = 5,
    ) -> dict:
        server = _normalize_server({
            "id": _new_id(),
            "name": name,
            "base_url": base_url,
            "email": email,
            "password": password,
            "api_key": api_key,
            "group_id": group_id,
            "auto_sync_enabled": auto_sync_enabled,
            "sync_interval_minutes": sync_interval_minutes,
        })
        if totp_secret:
            server["totp_secret_encrypted"] = self.totp.seal(totp_secret, server)
        self._validate_auto_sync(server)
        with self._lock:
            self._servers.append(server)
            self._save()
        sub2api_session_manager.invalidate(server["id"])
        return dict(server)

    def update_server(self, server_id: str, updates: dict) -> dict | None:
        with self._lock:
            for index, server in enumerate(self._servers):
                if server["id"] != server_id:
                    continue
                updates = dict(updates)
                secret = updates.pop("totp_secret", None)
                clear_secret = updates.pop("clear_totp_secret", False)
                merged = {**server, **{k: v for k, v in updates.items() if v is not None}, "id": server_id,
                          "revision": int(server.get("revision") or 0) + 1, "last_sync_error": "", "next_sync_at": 0}
                if clear_secret:
                    merged["totp_secret_encrypted"] = ""
                elif secret:
                    merged["totp_secret_encrypted"] = self.totp.seal(secret, merged)
                elif merged.get("totp_secret_encrypted"):
                    self.totp.open(merged["totp_secret_encrypted"], merged)
                self._validate_auto_sync(merged)
                self._servers[index] = _normalize_server(merged)
                self._save()
                result = dict(self._servers[index])
                break
            else:
                return None
        sub2api_session_manager.invalidate(server_id)
        return result

    @staticmethod
    def _validate_auto_sync(server: dict) -> None:
        if server.get("auto_sync_enabled") and (not server.get("totp_secret_encrypted") or not server.get("email")
                                                or not server.get("password") or server.get("api_key")):
            raise ValueError("自动同步需要管理员邮箱、密码和 2FA 密钥")

    def set_sync_state(self, server_id: str, **fields) -> None:
        with self._lock:
            for server in self._servers:
                if server["id"] == server_id:
                    server.update({key: value for key, value in fields.items()
                                   if key in {"last_sync_at", "last_sync_error", "next_sync_at", "sync_count"}})
                    self._save()
                    return

    def delete_server(self, server_id: str) -> bool:
        with self._lock:
            before = len(self._servers)
            self._servers = [server for server in self._servers if server["id"] != server_id]
            removed = len(self._servers) < before
            if removed:
                self._save()
        if removed:
            sub2api_session_manager.invalidate(server_id)
        return removed

    def set_import_job(self, server_id: str, import_job: dict | None) -> dict | None:
        with self._lock:
            for index, server in enumerate(self._servers):
                if server["id"] != server_id:
                    continue
                next_server = dict(server)
                next_server["import_job"] = _normalize_import_job(import_job, fail_unfinished=False)
                self._servers[index] = next_server
                self._save()
                return dict(next_server)
        return None

    def get_import_job(self, server_id: str) -> dict | None:
        with self._lock:
            for server in self._servers:
                if server["id"] == server_id:
                    job = server.get("import_job")
                    return dict(job) if isinstance(job, dict) else None
        return None


def _api_key_headers(server: dict) -> dict[str, str]:
    api_key = _clean(server.get("api_key"))
    if not api_key:
        raise Sub2APISessionError("LOGIN_CONFIG_INVALID", "Sub2API Admin API Key is missing", status_code=400)
    return {"x-api-key": api_key, "Accept": "application/json"}


def _extract_access_token(credentials: object) -> str:
    if not isinstance(credentials, dict):
        return ""
    for key in ("access_token", "accessToken", "token"):
        value = _clean(credentials.get(key))
        if value:
            return value
    return ""


def _unwrap_envelope(payload: object) -> object:
    """Peel sub2api's `{code, message, data}` envelope, returning the inner `data` field
    when present. Also handles unwrapped responses from older/alt versions."""
    if isinstance(payload, dict) and "data" in payload and "code" in payload:
        return payload.get("data")
    return payload


def _extract_paged_items(payload: object) -> tuple[list, int]:
    """Return (items, total) from a paginated sub2api response.

    Handles both the wrapped shape `{code,data:{items,total,...}}` and a few looser
    variants (`{data:[...]}`, `[...]`, `{items:[...],total:N}`)."""
    inner = _unwrap_envelope(payload)
    if isinstance(inner, list):
        return inner, len(inner)
    if isinstance(inner, dict):
        for key in ("items", "data", "list"):
            value = inner.get(key)
            if isinstance(value, list):
                return value, int(inner.get("total") or len(value))
    return [], 0


def _remote_get(server: dict, path: str, *, params: dict | None = None, api_key_session: Session | None = None):
    if _clean(server.get("api_key")):
        if api_key_session is None:
            raise RuntimeError("API Key request session is missing")
        base_url = _clean(server.get("base_url")).rstrip("/")
        response = get_compatible_admin_response(
            api_key_session,
            base_url,
            path,
            headers=_api_key_headers(server),
            params=params,
        )
        if not response.ok:
            raise Sub2APISessionError(
                "UPSTREAM_ERROR",
                f"Sub2API request failed with HTTP {response.status_code}",
                status_code=502,
            )
        return response
    return sub2api_session_manager.authorized_get(server, path, params=params)


def sync_remote_accounts(server: dict) -> dict:
    with REMOTE_OPERATION_LOCK:
        if _clean(server.get("api_key")):
            return {"status": "authenticated", "accounts": list_remote_accounts(server)}
        auth = sub2api_session_manager.begin_login(server)
        if auth.get("status") == "login_2fa_required":
            if not server.get("totp_secret_encrypted"):
                return {**auth, "accounts": []}
            for attempt in range(2):
                try:
                    sub2api_session_manager.complete_login_2fa(server["id"], sub2api_config.totp.next_code(server))
                    break
                except Sub2APISessionError as exc:
                    if attempt or exc.code != "LOGIN_2FA_INVALID" or exc.status_code not in {400, 403}:
                        raise
        return {"status": "authenticated", "accounts": list_remote_accounts(server)}


def complete_remote_login_2fa(server: dict, totp_code: str) -> dict:
    with REMOTE_OPERATION_LOCK:
        sub2api_session_manager.complete_login_2fa(_clean(server.get("id")), totp_code)
        return {"status": "authenticated", "accounts": list_remote_accounts(server)}


def verify_automatic_step_up(server: dict) -> None:
    for attempt in range(2):
        try:
            sub2api_session_manager.verify_step_up(server, sub2api_config.totp.next_code(server))
            return
        except Sub2APISessionError as exc:
            if attempt or exc.code != "STEP_UP_INVALID" or exc.status_code not in {400, 403}:
                raise


def list_remote_accounts(server: dict) -> list[dict]:
    """Return a flat list of OpenAI OAuth accounts from a sub2api server."""
    base_url = _clean(server.get("base_url"))
    if not base_url:
        return []

    group_id = _clean(server.get("group_id"))

    session = Session(verify=True) if _clean(server.get("api_key")) else None
    items: list[dict] = []
    try:
        page = 1
        while True:
            params: dict[str, object] = {
                "platform": "openai",
                "type": "oauth",
                "page": page,
                "page_size": 200,
            }
            if group_id:
                params["group"] = group_id
            response = _remote_get(
                server,
                "/api/v1/admin/accounts",
                params=params,
                api_key_session=session,
            )
            payload = response.json()

            data, total = _extract_paged_items(payload)
            if not data:
                break

            for account in data:
                if not isinstance(account, dict):
                    continue
                credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
                account_id = account.get("id")
                if account_id is None:
                    continue
                items.append({
                    "id": str(account_id),
                    "name": _clean(account.get("name")),
                    "email": _clean(credentials.get("email")) or _clean(account.get("name")),
                    "plan_type": _clean(credentials.get("plan_type")),
                    "status": _clean(account.get("status")),
                    "expires_at": _clean(credentials.get("expires_at")),
                    "has_refresh_token": bool(_clean(credentials.get("refresh_token"))),
                    "chatgpt_user_id": _clean(credentials.get("chatgpt_user_id")),
                    "chatgpt_account_id": _clean(credentials.get("chatgpt_account_id")),
                })

            if page * 200 >= total or len(data) < 200:
                break
            page += 1
            if page > 50:
                raise Sub2APISessionError("SOURCE_TOO_LARGE", "请缩小源站分组范围后再同步", status_code=400)
    finally:
        if session is not None:
            session.close()

    return items


def list_remote_groups(server: dict) -> list[dict]:
    """Return OpenAI account groups from a sub2api server."""
    base_url = _clean(server.get("base_url"))
    if not base_url:
        return []

    session = Session(verify=True) if _clean(server.get("api_key")) else None
    items: list[dict] = []
    try:
        page = 1
        while True:
            response = _remote_get(
                server,
                "/api/v1/admin/groups",
                params={
                    "page": page,
                    "page_size": 200,
                },
                api_key_session=session,
            )
            payload = response.json()

            data, total = _extract_paged_items(payload)
            if not data:
                break

            for group in data:
                if not isinstance(group, dict):
                    continue
                group_id = group.get("id")
                if group_id is None:
                    continue
                items.append({
                    "id": str(group_id),
                    "name": _clean(group.get("name")),
                    "description": _clean(group.get("description")),
                    "platform": _clean(group.get("platform")),
                    "status": _clean(group.get("status")),
                    "account_count": int(group.get("account_count") or 0),
                    "active_account_count": int(group.get("active_account_count") or 0),
                })

            if page * 200 >= total or len(data) < 200:
                break
            page += 1
    finally:
        if session is not None:
            session.close()

    return items


def _fetch_accounts_for_import(server: dict, account_ids: list[str], source_accounts: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """通过受保护导出取得当前 AT，续期凭据只在响应内存中短暂出现，不进入本地号池。"""
    ids = [_clean(item) for item in account_ids if _clean(item)]
    if not ids:
        return [], []

    if _clean(server.get("api_key")):
        raise Sub2APISessionError(
            "API_KEY_EXPORT_FORBIDDEN",
            "Admin API Key cannot export protected accounts; use password login",
            status_code=403,
        )
    data = sub2api_session_manager.export_accounts(server, ids)
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, list):
        raise RuntimeError("invalid export payload")

    exported_accounts = [account for account in accounts if isinstance(account, dict)]
    top_level_ids = [_clean(account.get("id")) for account in exported_accounts]
    has_top_level_ids = any(top_level_ids)
    if has_top_level_ids and not all(top_level_ids):
        raise Sub2APISessionError("UPSTREAM_ERROR", "Sub2API export account IDs are inconsistent")
    if not has_top_level_ids and len(exported_accounts) > len(ids):
        raise Sub2APISessionError("UPSTREAM_ERROR", "Sub2API export returned more accounts than requested")
    if not has_top_level_ids:
        source_accounts = source_accounts if source_accounts is not None else list_remote_accounts(server)

    imported: list[dict] = []
    errors: list[dict] = []
    ambiguous_ids: set[str] = set()
    selected_ids = set(ids)
    matched_accounts = 0
    for index, account in enumerate(exported_accounts):
        credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
        account_id = _clean(account.get("id"))
        if has_top_level_ids and account_id not in selected_ids:
            continue
        matched_accounts += 1
        from services.account_service import AccountService
        account_label = account_id or _clean(account.get("name")) or ids[index]
        try:
            payload = AccountService._prepare_account_payload(account)
        except ValueError:
            errors.append({"name": account_label, "error": "账号凭据格式无效"})
            continue
        if payload:
            if not account_id:
                identity = AccountService._import_identity(payload)
                candidates = []
                for source in source_accounts or []:
                    if _clean(source.get("id")) not in selected_ids:
                        continue
                    source_identity = (_clean(source.get("chatgpt_user_id")).lower(), _clean(source.get("chatgpt_account_id")))
                    same_identity = identity and all(source_identity) and identity == source_identity
                    same_name = _clean(source.get("name")) == _clean(account.get("name"))
                    if same_identity or (same_name and not all(source_identity)):
                        candidates.append(source)
                exact_names = [source for source in candidates if _clean(source.get("name")) == _clean(account.get("name"))]
                if len(exact_names) == 1:
                    candidates = exact_names
                if len(candidates) != 1:
                    if candidates:
                        ambiguous_ids.update(_clean(source.get("id")) for source in candidates)
                    else:
                        errors.append({"name": account_label, "error": "无法匹配源站账号 ID"})
                    continue
                account_id = _clean(candidates[0].get("id"))
            payload["sub2api"] = {**payload.get("sub2api", {}), "server_id": server["id"], "id": account_id}
            payload["last_source_sync_at"] = _now_iso()
            imported.append(payload)
        else:
            errors.append({"name": account_label, "error": "missing access_token"})

    # 源站导出会省略 ID；同名且身份相同的记录必须按单个 ID 再取一次，不能猜测数组顺序。
    for account_id in sorted(ambiguous_ids):
        single = sub2api_session_manager.export_accounts(server, [account_id])
        rows = single.get("accounts") if isinstance(single, dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            errors.append({"name": account_id, "error": "源站单账号导出结果不唯一"})
            continue
        raw = rows[0]
        if raw.get("id") is not None and _clean(raw.get("id")) != account_id:
            errors.append({"name": account_id, "error": "源站导出账号 ID 不一致"})
            continue
        try:
            payload = AccountService._prepare_account_payload(raw)
        except ValueError:
            payload = None
        if not payload:
            errors.append({"name": account_id, "error": "账号凭据格式无效"})
            continue
        expected = next((item for item in source_accounts or [] if _clean(item.get("id")) == account_id), {})
        identity = (_clean(expected.get("chatgpt_user_id")).lower(), _clean(expected.get("chatgpt_account_id")))
        if all(identity) and AccountService._import_identity(payload) != identity:
            errors.append({"name": account_id, "error": "源站导出账号身份不一致"})
            continue
        payload["sub2api"] = {**payload.get("sub2api", {}), "server_id": server["id"], "id": account_id}
        payload["last_source_sync_at"] = _now_iso()
        imported.append(payload)

    imported_ids = {item["sub2api"]["id"] for item in imported}
    if len(imported_ids) < len(selected_ids):
        errors.append({"name": ",".join(sorted(selected_ids - imported_ids)), "error": f"exported {len(imported_ids)}/{len(selected_ids)} accounts"})

    return imported, errors


def _fetch_access_tokens_for_accounts(server: dict, account_ids: list[str]) -> tuple[list[str], list[dict]]:
    """兼容旧调用方；正式导入使用完整账号对象。"""
    accounts, errors = _fetch_accounts_for_import(server, account_ids)
    return [item["access_token"] for item in accounts], errors


class Sub2APIImportService:
    def __init__(self, sub2api_config: Sub2APIConfig):
        self._config = sub2api_config

    def start_import(self, server: dict, account_ids: list[str], step_up_code: str) -> dict:
        with REMOTE_OPERATION_LOCK:
            return self._start_import(server, account_ids, step_up_code)

    def _start_import(self, server: dict, account_ids: list[str], step_up_code: str) -> dict:
        ids = [_clean(item) for item in account_ids if _clean(item)]
        if not ids or len(ids) > 1000 or any(len(item) > 128 for item in ids):
            raise ValueError("account ids is required")

        server_id = _clean(server.get("id"))
        if not _clean(server.get("api_key")):
            if server.get("totp_secret_encrypted"):
                # 密钥已配置时，用户无需在每次导入时重复输入验证码。
                if not sub2api_session_manager.is_authenticated(server_id):
                    sync_remote_accounts(server)
                verify_automatic_step_up(server)
            elif not _clean(step_up_code):
                raise Sub2APISessionError("STEP_UP_REQUIRED", "Step-up TOTP code is required", status_code=409)
            else:
                sub2api_session_manager.verify_step_up(server, step_up_code)
        accounts, errors = _fetch_accounts_for_import(server, ids)

        job = {
            "job_id": uuid.uuid4().hex,
            "status": "pending",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "total": len(ids),
            "completed": 0,
            "added": 0,
            "skipped": 0,
            "refreshed": 0,
            "failed": len(errors),
            "errors": list(errors),
        }
        saved = self._config.set_import_job(server_id, job)
        if saved is None:
            raise ValueError("server not found")

        thread = threading.Thread(
            target=self._run_import,
            args=(server_id, ids, accounts, errors),
            name=f"sub2api-import-{server_id}",
            daemon=True,
        )
        thread.start()
        return dict(saved.get("import_job") or job)

    def _update_job(self, server_id: str, **updates) -> None:
        current = self._config.get_import_job(server_id)
        if current is None:
            return
        next_job = {**current, **updates, "updated_at": _now_iso()}
        self._config.set_import_job(server_id, next_job)

    def _append_error(self, server_id: str, account_id: str, message: str) -> None:
        current = self._config.get_import_job(server_id)
        if current is None:
            return
        errors = list(current.get("errors") or [])
        errors.append({"name": account_id, "error": message})
        self._update_job(server_id, errors=errors, failed=len(errors))

    def _run_import(
        self,
        server_id: str,
        account_ids: list[str],
        accounts: list[dict],
        errors: list[dict],
    ) -> None:
        self._update_job(server_id, status="running")

        current = self._config.get_import_job(server_id) or {}
        self._update_job(
            server_id,
            completed=len(account_ids),
            failed=len(current.get("errors") or []),
        )

        if not accounts:
            current = self._config.get_import_job(server_id) or {}
            self._update_job(
                server_id,
                status="failed",
                completed=int(current.get("total") or 0),
                failed=len(current.get("errors") or []),
            )
            return

        add_result = account_service.add_account_items(accounts)
        tokens = [item["access_token"] for item in accounts]
        refresh_result = account_service.refresh_accounts(tokens)
        current = self._config.get_import_job(server_id) or {}
        self._update_job(
            server_id,
            status="completed",
            completed=len(account_ids),
            added=int(add_result.get("added") or 0),
            skipped=int(add_result.get("skipped") or 0),
            refreshed=int(refresh_result.get("refreshed") or 0),
            failed=len(current.get("errors") or []),
        )


sub2api_config = Sub2APIConfig(SUB2API_CONFIG_FILE)
sub2api_import_service = Sub2APIImportService(sub2api_config)


def sync_source_access_tokens(server: dict) -> dict:
    """一次完整自动同步；只让源站处理 OAuth 续期，本地只更新最新 AT。"""
    with REMOTE_OPERATION_LOCK:
        if sub2api_session_manager.is_authenticated(server["id"]):
            try:
                result = {"status": "authenticated", "accounts": list_remote_accounts(server)}
            except Sub2APISessionError as exc:
                if exc.code != "SESSION_EXPIRED":
                    raise
                result = sync_remote_accounts(server)
        else:
            result = sync_remote_accounts(server)
        if result.get("status") != "authenticated":
            raise Sub2APISessionError("AUTO_LOGIN_REQUIRED", "请配置管理员 2FA 密钥", status_code=409)
        sources = result.get("accounts", [])
        if len(sources) > 1000:
            raise Sub2APISessionError("SOURCE_TOO_LARGE", "自动同步每个连接最多 1000 个账号，请按分组拆分", status_code=400)
        if not sources:
            return {"count": 0, "added": 0, "failed": 0}
        verify_automatic_step_up(server)
        accounts, errors = _fetch_accounts_for_import(server, [item["id"] for item in sources], sources)
        current = sub2api_config.get_server(server["id"])
        if current is None or current.get("revision") != server.get("revision"):
            raise Sub2APISessionError("CONFIG_CHANGED", "连接配置已变化，请重新同步", status_code=409)
        added = account_service.add_account_items(accounts)
        return {"count": len(accounts), "added": added["added"], "failed": len(errors)}


def start_sub2api_sync_scheduler(stop_event) -> threading.Thread:
    def run():
        while not stop_event.is_set():
            for server in sub2api_config.list_servers():
                if stop_event.is_set():
                    return
                if not server.get("auto_sync_enabled") or float(server.get("next_sync_at") or 0) > time.time():
                    continue
                interval = int(server.get("sync_interval_minutes") or 5) * 60
                try:
                    result = sync_source_access_tokens(server)
                    sub2api_config.set_sync_state(server["id"], last_sync_at=_now_iso(),
                        last_sync_error="部分账号无法匹配源站 ID" if result["failed"] else "",
                        sync_count=result["count"], next_sync_at=time.time() + interval)
                except Exception as exc:
                    code = exc.code if isinstance(exc, Sub2APISessionError) else "SYNC_FAILED"
                    # 错误只保存固定代码，不写出响应正文、密码或验证码。
                    sub2api_config.set_sync_state(server["id"], last_sync_error=code,
                        next_sync_at=time.time() + max(interval, 900))
            stop_event.wait(10)
    thread = threading.Thread(target=run, name="sub2api-access-token-sync", daemon=True)
    thread.start()
    return thread
