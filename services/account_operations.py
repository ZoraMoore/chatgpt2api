"""账号管理操作：使用稳定引用，不在任务状态或操作结果中传递凭据。"""
from __future__ import annotations

import copy
import re
import time
import uuid
from threading import Lock, Thread

from services.account_service import account_service


class AccountOperations:
    def __init__(self, accounts=account_service):
        self.accounts = accounts
        self.lock = Lock()
        self.jobs = {}

    @staticmethod
    def ref(account):
        return account["management_id"]

    def list(self):
        return [{"id": self.ref(a), "label": a.get("email") or a.get("name") or "未命名账号",
                 "expires_at": a.get("access_token_expires_at") or "",
                 "has_refresh_token": a.get("has_refresh_token", False),
                 "auto_refresh_token": a.get("auto_refresh_token") is not False,
                 "image_cleanup_enabled": a.get("image_cleanup_enabled") is True,
                 "cleanup_next_at": a.get("image_cleanup_next_at"),
                 "cleanup_last_status": a.get("image_cleanup_last_status"),
                 "cleanup_last_at": a.get("image_cleanup_last_at"),
                 "cleanup_last_error": a.get("image_cleanup_last_error") or "",
                 "image_inflight": a.get("image_inflight", 0),
                 "status": a.get("status"),
                 "quota_state": a.get("image_limit_state", ""),
                 "quota_reason": a.get("image_limit_reason", ""),
                 "quota_restore_at": a.get("image_limit_restore_at", ""),
                 "quota_occurred_at": a.get("image_limit_occurred_at", ""),
                 "quota_next_check_at": a.get("image_limit_next_check_at", ""),
                 "supported": a.get("source_type") != "adobe"}
                for a in self.accounts.list_accounts()]

    def resolve(self, refs):
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 20
                or any(not isinstance(ref, str) or not re.fullmatch(r"[a-f0-9]{64}", ref) for ref in refs)):
            raise ValueError("请选择 1 至 20 个有效账号")
        available = {self.ref(a): a for a in self.accounts.list_accounts()}
        if any(ref not in available or available[ref].get("source_type") == "adobe" for ref in refs):
            raise ValueError("账号不存在或不支持该操作，请重新加载列表")
        return [available[ref] for ref in dict.fromkeys(refs)]

    def save_state(self, ref, updates):
        # 管理设置不能触发原 update_account 中的限流账号自动移除逻辑。
        with self.accounts._lock:
            for token, account in self.accounts._accounts.items():
                if self.ref(account) == ref:
                    self.accounts._accounts[token] = {**account, **updates}
                    self.accounts._save_accounts()
                    return True
        return False

    def preferences(self, refs, *, image_cleanup_enabled=None, auto_refresh_token=None):
        selected = self.resolve(refs)
        updates = {}
        for key, value in (("image_cleanup_enabled", image_cleanup_enabled), ("auto_refresh_token", auto_refresh_token)):
            if value is not None:
                if not isinstance(value, bool):
                    raise ValueError("账号开关必须为布尔值")
                updates[key] = value
        if not updates:
            raise ValueError("请选择需要修改的账号设置")
        with self.accounts._lock:
            # 再次检查可防止加载列表后并发删除账号造成部分更新。
            wanted = {self.ref(a) for a in selected}
            present = {self.ref(a) for a in self.accounts._accounts.values()}
            if not wanted <= present:
                raise ValueError("账号已变更，请重新加载列表")
            for key, account in self.accounts._accounts.items():
                if self.ref(account) in wanted:
                    fields = dict(updates)
                    if fields.get("image_cleanup_enabled") is True and not account.get("image_cleanup_enabled"):
                        fields["image_cleanup_next_at"] = time.time() + 86400
                    self.accounts._accounts[key] = {**account, **fields}
            self.accounts._save_accounts()
        return self.list()

    def refresh(self, refs):
        selected = self.resolve(refs)
        results = []
        for initial in selected:
            ref = self.ref(initial)
            account = next((a for a in self.accounts.list_accounts() if self.ref(a) == ref), None)
            code = ""
            if account is None:
                code = "account_removed"
            elif not account.get("refresh_token"):
                code = "missing_refresh_token"
            else:
                previous = account.get("last_token_refresh_at")
                self.accounts.refresh_access_token(account["access_token"], force=True, event="manual_access_token_refresh")
                account = next((a for a in self.accounts.list_accounts() if self.ref(a) == ref), None)
                if account is None or account.get("last_token_refresh_error") or account.get("last_token_refresh_at") == previous:
                    code = "refresh_failed"
            results.append({"id": ref, "status": "error" if code else "success", "code": code,
                            "expires_at": account.get("access_token_expires_at") if account else ""})
        return results

    def start_refresh(self, refs):
        self.resolve(refs)
        with self.lock:
            self.jobs = {key: value for key, value in self.jobs.items() if time.time() - value["created_at"] < 3600}
            if any(job["status"] == "running" for job in self.jobs.values()):
                raise ValueError("已有 Access Token 刷新任务运行中，请稍后再试")
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {"id": job_id, "status": "running", "created_at": time.time(), "results": []}
        def run():
            try:
                results = self.refresh(refs)
                with self.lock:
                    self.jobs[job_id].update(status="completed", results=results)
            except Exception:
                with self.lock:
                    self.jobs[job_id].update(status="error", error="刷新任务失败，请检查账号状态后重试")
        Thread(target=run, name="manual-token-refresh", daemon=True).start()
        return job_id

    def get_job(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return copy.deepcopy(self.jobs[job_id])


account_operations = AccountOperations()
