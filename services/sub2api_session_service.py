"""In-memory authenticated HTTP sessions for Sub2API admin operations."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable

from curl_cffi.requests import Session


SUB2API_USER_AGENT = "chatgpt2api-sub2api/1.0"


class Sub2APISessionError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass
class Sub2APISessionState:
    session: Session
    base_url: str
    headers: dict[str, str]
    temp_token: str = ""
    access_token: str = ""
    expires_at: float = 0


def _clean(value: object) -> str:
    return str(value or "").strip()


def _unwrap(payload: object) -> object:
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        return payload.get("data")
    return payload


class Sub2APISessionManager:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._session_factory = session_factory or (lambda: Session(verify=True))
        self._clock = clock
        self._states: dict[str, Sub2APISessionState] = {}
        self._lock = Lock()

    def begin_login(self, server: dict) -> dict:
        server_id = _clean(server.get("id"))
        base_url = _clean(server.get("base_url")).rstrip("/")
        email = _clean(server.get("email"))
        password = _clean(server.get("password"))
        if not server_id or not base_url or not email or not password:
            raise Sub2APISessionError("LOGIN_CONFIG_INVALID", "Sub2API login configuration is incomplete", status_code=400)

        self.invalidate(server_id)
        state = Sub2APISessionState(
            session=self._session_factory(),
            base_url=base_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": SUB2API_USER_AGENT,
            },
        )
        with self._lock:
            self._states[server_id] = state

        response = state.session.post(
            f"{base_url}/api/v1/auth/login",
            json={"email": email, "password": password},
            headers=state.headers,
            timeout=30,
        )
        body = self._response_body(response, "LOGIN_FAILED")
        if not isinstance(body, dict):
            self.invalidate(server_id)
            raise Sub2APISessionError("LOGIN_FAILED", "Sub2API login response is invalid")

        if bool(body.get("requires_2fa")):
            state.temp_token = _clean(body.get("temp_token"))
            if not state.temp_token:
                self.invalidate(server_id)
                raise Sub2APISessionError("LOGIN_FAILED", "Sub2API login challenge is invalid")
            return {
                "status": "login_2fa_required",
                "user_email_masked": _clean(body.get("user_email_masked")),
            }

        return self._store_access_token(server_id, state, body)

    def complete_login_2fa(self, server_id: str, totp_code: str) -> dict:
        code = _clean(totp_code)
        if len(code) != 6 or not code.isdigit():
            raise Sub2APISessionError("LOGIN_2FA_INVALID", "TOTP code must be 6 digits", status_code=400)
        state = self._require_state(server_id, require_token=False)
        if not state.temp_token:
            raise Sub2APISessionError("SESSION_EXPIRED", "Login challenge expired; sync again", status_code=409)

        response = state.session.post(
            f"{state.base_url}/api/v1/auth/login/2fa",
            json={"temp_token": state.temp_token, "totp_code": code},
            headers=state.headers,
            timeout=30,
        )
        body = self._response_body(response, "LOGIN_2FA_INVALID")
        if not isinstance(body, dict):
            raise Sub2APISessionError("LOGIN_2FA_INVALID", "Sub2API 2FA response is invalid", status_code=403)
        state.temp_token = ""
        return self._store_access_token(server_id, state, body)

    def verify_step_up(self, server: dict, code: str) -> None:
        totp_code = _clean(code)
        if len(totp_code) != 6 or not totp_code.isdigit():
            raise Sub2APISessionError("STEP_UP_INVALID", "Step-up TOTP code must be 6 digits", status_code=400)
        state = self._require_state(_clean(server.get("id")))
        response = state.session.post(
            f"{state.base_url}/api/v1/user/totp/step-up",
            json={"code": totp_code},
            headers=self._authorized_headers(state),
            timeout=30,
        )
        body = self._response_body(response, "STEP_UP_INVALID")
        if not isinstance(body, dict) or not bool(body.get("verified")):
            raise Sub2APISessionError("STEP_UP_INVALID", "Sub2API Step-up verification failed", status_code=403)

    def authorized_get(self, server: dict, path: str, *, params: dict | None = None):
        state = self._require_state(_clean(server.get("id")))
        response = state.session.get(
            f"{state.base_url}{path}",
            headers=self._authorized_headers(state),
            params=params,
            timeout=30,
        )
        if not response.ok:
            self._raise_authorized_error(server, response)
        return response

    def export_accounts(self, server: dict, account_ids: list[str]) -> dict:
        ids = [_clean(item) for item in account_ids if _clean(item)]
        if not ids or len(ids) > 1000 or any(len(item) > 128 for item in ids):
            raise Sub2APISessionError("ACCOUNT_IDS_INVALID", "Account IDs are invalid", status_code=400)
        response = self.authorized_get(
            server,
            "/api/v1/admin/accounts/data",
            params={"ids": ",".join(ids), "include_proxies": "false"},
        )
        body = self._response_body(response, "UPSTREAM_ERROR")
        if not isinstance(body, dict) or not isinstance(body.get("accounts"), list):
            raise Sub2APISessionError("UPSTREAM_ERROR", "Sub2API export response is invalid")
        return body

    def invalidate(self, server_id: str) -> None:
        with self._lock:
            state = self._states.pop(_clean(server_id), None)
        if state is not None:
            state.session.close()

    def _require_state(self, server_id: str, *, require_token: bool = True) -> Sub2APISessionState:
        with self._lock:
            state = self._states.get(_clean(server_id))
        if state is None:
            raise Sub2APISessionError("SESSION_EXPIRED", "Sub2API session expired; sync again", status_code=409)
        if require_token and (not state.access_token or state.expires_at <= self._clock()):
            self.invalidate(server_id)
            raise Sub2APISessionError("SESSION_EXPIRED", "Sub2API session expired; sync again", status_code=409)
        return state

    def _store_access_token(self, server_id: str, state: Sub2APISessionState, body: dict) -> dict:
        access_token = _clean(body.get("access_token"))
        if not access_token:
            self.invalidate(server_id)
            raise Sub2APISessionError("LOGIN_FAILED", "Sub2API login did not return an access token")
        expires_in = max(60, int(body.get("expires_in") or 3600))
        state.access_token = access_token
        state.expires_at = self._clock() + expires_in
        state.temp_token = ""
        return {"status": "authenticated", "expires_in": expires_in}

    @staticmethod
    def _authorized_headers(state: Sub2APISessionState) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": state.headers["User-Agent"],
            "Authorization": f"Bearer {state.access_token}",
        }

    def _raise_authorized_error(self, server: dict, response) -> None:
        status_code = int(getattr(response, "status_code", 502) or 502)
        upstream_code = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                upstream_code = _clean(payload.get("code"))
                data = payload.get("data")
                if isinstance(data, dict):
                    upstream_code = upstream_code or _clean(data.get("code"))
        except Exception:
            pass

        server_id = _clean(server.get("id"))
        if status_code == 401 or upstream_code in {"SESSION_BINDING_MISMATCH", "TOKEN_EXPIRED", "TOKEN_REVOKED"}:
            self.invalidate(server_id)
            raise Sub2APISessionError("SESSION_EXPIRED", "Sub2API session expired; sync again", status_code=409)
        if upstream_code == "STEP_UP_ADMIN_API_KEY_FORBIDDEN":
            raise Sub2APISessionError(
                "API_KEY_EXPORT_FORBIDDEN",
                "Admin API Key cannot export protected accounts; use password login",
                status_code=403,
            )
        if status_code == 403:
            raise Sub2APISessionError("STEP_UP_INVALID", "Sub2API Step-up verification is required or invalid", status_code=403)
        raise Sub2APISessionError("UPSTREAM_ERROR", f"Sub2API request failed with HTTP {status_code}", status_code=502)

    @staticmethod
    def _response_body(response, error_code: str) -> object:
        if not response.ok:
            status_code = int(getattr(response, "status_code", 502) or 502)
            raise Sub2APISessionError(error_code, f"Sub2API request failed with HTTP {status_code}", status_code=status_code)
        try:
            return _unwrap(response.json())
        except Exception as exc:
            raise Sub2APISessionError(error_code, "Sub2API response is not valid JSON") from exc


sub2api_session_manager = Sub2APISessionManager()
