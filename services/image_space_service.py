"""图像空间清理：预览快照、分批移入最近删除，并以完整复扫确认结果。"""
from __future__ import annotations

import copy
import hashlib
import re
import time
import uuid
from datetime import datetime, timezone
from collections import Counter
from threading import Lock, Thread

from curl_cffi import requests

from services.account_service import account_service
from services.account_operations import AccountOperations
from services.proxy_service import proxy_settings

ORIGIN = "https://chatgpt.com"
LIST_PATH = "/backend-api/files/library"
DELETE_PATH = LIST_PATH + "/files/delete-batch"
SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")


class ImageSpaceError(ValueError):
    pass


def account_ref(account: dict) -> str:
    return account.get("management_id") or hashlib.sha256(account["access_token"].encode()).hexdigest()


def image_created_at(row):
    raw = (row.get("created_at") or row.get("record_creation_time") or row.get("file_upload_time")
           or row.get("create_time") or row.get("created_time"))
    try:
        value = float(raw)
        if value > 1e12:
            value /= 1000
        return value if 0 < value < 253402300800 else None
    except (TypeError, ValueError, OverflowError):
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError):
            return None


def account_label(account: dict) -> str:
    return str(account.get("email") or account.get("name") or "未命名账号")[:200]


class ImageSpaceClient:
    def __init__(self, account: dict):
        token = account["access_token"]
        claim = account_service._decode_jwt_payload(token).get("https://api.openai.com/auth", {})
        claim = claim if isinstance(claim, dict) else {}
        account_id = account.get("account_id") or claim.get("chatgpt_account_id")
        if not isinstance(account_id, str) or not SAFE_ID.fullmatch(account_id):
            raise ImageSpaceError("账号缺少有效的 ChatGPT account_id，请先刷新或重新导入账号")
        if not isinstance(token, str) or len(token) > 32768 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ImageSpaceError("账号凭据格式无效")
        device = str(uuid.uuid4())
        self.headers = proxy_settings.build_headers({
            "authorization": f"Bearer {token}", "chatgpt-account-id": account_id,
            "oai-did": device, "oai-device-id": device, "oai-language": "zh-CN",
            "origin": ORIGIN, "referer": ORIGIN + "/space?category=images",
            "accept": "application/json", "content-type": "application/json",
        }, account=account)
        self.session = requests.Session(**proxy_settings.build_session_kwargs(
            account=account, upstream=True, impersonate="chrome", verify=True,
        ))
        self.deadline = time.monotonic() + 1800

    def close(self):
        self.session.close()

    def post(self, path: str, body: dict) -> dict:
        for attempt in range(5):
            if time.monotonic() >= self.deadline:
                raise ImageSpaceError("处理超过 30 分钟，结果尚未完整确认，请重新预览")
            try:
                response = self.session.post(ORIGIN + path, json=body, headers=self.headers,
                                             timeout=45, allow_redirects=False)
            except requests.exceptions.RequestException:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise ImageSpaceError("上游网络请求失败，请检查代理后重试") from None
            status = response.status_code
            if status in (401, 403):
                raise ImageSpaceError(f"上游 HTTP {status}，请检查账号凭据、权限或代理")
            if status in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            # 整批 409 也必须进入复扫，不能把冲突当成已删除。
            if status == 409 and path == DELETE_PATH:
                return {}
            if not 200 <= status < 300:
                raise ImageSpaceError(f"上游 HTTP {status}，未能完成图像空间操作")
            try:
                data = response.json()
            except (ValueError, TypeError):
                raise ImageSpaceError("上游返回了无效 JSON，无法确认结果") from None
            if not isinstance(data, dict):
                raise ImageSpaceError("上游响应格式异常，无法确认结果")
            return data
        raise ImageSpaceError("上游请求失败")

    def scan(self, progress=lambda count: None) -> dict[str, dict]:
        cursor = None
        seen_cursors = set()
        items = {}
        for _ in range(100):
            data = self.post(LIST_PATH, {
                "cursor": cursor, "limit": 200, "categories": ["images"], "source": None,
                "include_hidden_files": False, "include_saved_entities": False,
                "include_sites": False, "trashed_only": False,
                "external_account_keys": {}, "providers": [],
            })
            if not isinstance(data.get("items"), list):
                raise ImageSpaceError("图片列表格式异常，已停止操作")
            for row in data["items"]:
                if not isinstance(row, dict):
                    raise ImageSpaceError("图片条目格式异常，已停止操作")
                if row.get("trashed_at"):
                    continue
                item = {
                    "library_file_id": row.get("id") or row.get("library_file_id"),
                    "file_id": row.get("file_id"),
                    "parent_directory_id": row.get("directory_id") or row.get("parent_directory_id") or "",
                    "file_name": row.get("file_name") or "",
                    "created_at": image_created_at(row),
                }
                for key in ("library_file_id", "file_id", "parent_directory_id"):
                    value = item[key]
                    if key == "parent_directory_id" and value == "":
                        continue
                    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
                        raise ImageSpaceError("图片标识格式异常，已停止操作")
                if not isinstance(item["file_name"], str) or len(item["file_name"]) > 1024:
                    raise ImageSpaceError("图片文件名格式异常，已停止操作")
                items[item["library_file_id"]] = item
            if len(items) > 20000:
                raise ImageSpaceError("单账号图片超过 20000 张，请先在网页分批清理")
            progress(len(items))
            cursor = data.get("cursor")
            if cursor is None or cursor == "":
                return items
            if not isinstance(cursor, str) or len(cursor) > 8192 or cursor in seen_cursors:
                raise ImageSpaceError("图片分页游标异常，列表未完整读取")
            seen_cursors.add(cursor)
            # 空页仍可能有下一页，必须只以游标结束作为完整扫描依据。
        raise ImageSpaceError("图片列表超过 100 页，列表未完整读取")


def clear_snapshot(client, snapshot: dict, update, sleep=time.sleep):
    remaining = dict(snapshot)
    attempts = Counter()
    accepted = set()
    for sweep in range(8):
        pending = [item for key, item in remaining.items() if attempts[key] < 4]
        update(phase="deleting", sweep=sweep + 1)
        for offset in range(0, len(pending), 25):
            batch = pending[offset:offset + 25]
            response = client.post(DELETE_PATH, {"files": [{key: item[key] for key in
                ("library_file_id", "file_id", "parent_directory_id", "file_name") if key in item} for item in batch]})
            rows = response.get("files")
            batch_ids = {item["library_file_id"] for item in batch}
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get("library_file_id") in batch_ids:
                        code = row.get("status_code")
                        if row.get("success") is True or (isinstance(code, int) and 200 <= code < 300):
                            accepted.add(row["library_file_id"])
            elif response.get("success") is True:
                accepted.update(batch_ids)
            attempts.update(batch_ids)
            update(attempted=len(attempts), accepted=len(accepted))
            sleep(0.3)
        update(phase="verifying")
        sleep(20)
        listed = client.scan()
        # 仅删除用户确认过的快照，不扩大到任务期间新增的图片。
        remaining = {key: item for key, item in snapshot.items() if key in listed}
        update(remaining=len(remaining), verified=len(snapshot) - len(remaining))
        if not remaining:
            return
    raise ImageSpaceError("复核后仍有图片留在空间，可能删除失败或索引尚未更新；请稍后重新预览")


class ImageSpaceService:
    def __init__(self, accounts=account_service, client_factory=ImageSpaceClient):
        self.accounts = accounts
        self.client_factory = client_factory
        self.lock = Lock()
        self.jobs = {}
        self.snapshots = {}
        self.active = False

    def run_scheduled(self, now=None):
        """每天处理已显式开启的账号；未知图片年龄、在途或禁用账号不进入删除快照。"""
        now = time.time() if now is None else now
        with self.lock:
            if self.active:
                return
            self.active = True
        operations = AccountOperations(self.accounts)
        try:
            for account in self.accounts.list_accounts():
                if (account.get("image_cleanup_enabled") is not True or account.get("source_type") == "adobe"
                        or account.get("status") == "禁用" or account.get("image_inflight", 0)
                        or float(account.get("image_cleanup_next_at") or 0) > now):
                    continue
                ref = account_ref(account)
                # 执行前持久化下次时间，异常或重启不在当天重复提交清理。
                if not operations.save_state(ref, {"image_cleanup_last_at": now, "image_cleanup_next_at": now + 86400,
                                                   "image_cleanup_last_status": "running", "image_cleanup_last_error": ""}):
                    continue
                client = None
                try:
                    client = self.client_factory(account)
                    snapshot = {key: item for key, item in client.scan().items()
                                if (created := image_created_at(item)) is not None and created < now - 7 * 86400}
                    # 扫描期间可能关闭开关、删号或开始生图，提交删除前再次核实当前状态。
                    current = next((a for a in self.accounts.list_accounts() if account_ref(a) == ref), None)
                    if (not current or current.get("image_cleanup_enabled") is not True
                            or current.get("status") == "禁用" or current.get("image_inflight", 0)):
                        operations.save_state(ref, {"image_cleanup_last_status": "skipped"})
                        continue
                    if snapshot:
                        clear_snapshot(client, snapshot, lambda **fields: None)
                    operations.save_state(ref, {"image_cleanup_last_status": "success"})
                except Exception as exc:
                    operations.save_state(ref, {"image_cleanup_last_status": "error", "image_cleanup_last_error": self._error(exc)})
                finally:
                    self._close_client(client)
        finally:
            with self.lock:
                self.active = False

    def choices(self):
        return [{"id": account_ref(a), "label": account_label(a), "type": a.get("type", "")}
                for a in self.accounts.list_accounts() if a.get("source_type") != "adobe"]

    def _prune(self):
        for key, job in list(self.jobs.items()):
            if job["status"] not in ("scanning", "running") and time.time() - job["updated_at"] > 1800:
                self.jobs.pop(key)
                self.snapshots.pop(key, None)

    def get(self, job_id):
        with self.lock:
            self._prune()
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return copy.deepcopy(job)

    def _update(self, job_id, index=None, **fields):
        with self.lock:
            job = self.jobs[job_id]
            (job if index is None else job["accounts"][index]).update(fields)
            job["updated_at"] = time.time()

    def preview(self, refs, *, all_accounts=False):
        if (not isinstance(refs, list) or not isinstance(all_accounts, bool)
                or (bool(refs) and all_accounts) or (not all_accounts and len(refs) > 1000)
                or any(not isinstance(ref, str) or not re.fullmatch(r"[a-f0-9]{64}", ref) for ref in refs)):
            raise ImageSpaceError("账号选择范围无效，请重新选择")
        available = {account_ref(a): a for a in self.accounts.list_accounts()
                     if a.get("source_type") != "adobe"}
        # 全选只展开受理时存在的账号，后续新增账号不能进入这次确认过的快照。
        refs = list(available) if all_accounts else list(dict.fromkeys(refs))
        if not refs or any(ref not in available for ref in refs):
            raise ImageSpaceError("账号不存在或凭据已更新，请重新加载号池")
        selected = [available[ref] for ref in refs]
        with self.lock:
            self._prune()
            if self.active:
                raise ImageSpaceError("已有图像空间任务运行中，请等待完成")
            if len(self.jobs) >= 10:
                raise ImageSpaceError("预览任务过多，请稍后再试（任务保留 30 分钟）")
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {"id": job_id, "status": "scanning", "updated_at": time.time(),
                "all_accounts": all_accounts,
                "accounts": [{"label": account_label(a), "phase": "queued", "total": 0,
                              "attempted": 0, "accepted": 0, "verified": 0, "remaining": None,
                              "error": ""} for a in selected]}
            self.active = True
        Thread(target=self._preview, args=(job_id, selected), daemon=True).start()
        return job_id

    def _preview(self, job_id, selected):
        snapshots = []
        failed = False
        try:
            for index, account in enumerate(selected):
                client = None
                try:
                    self._update(job_id, index, phase="scanning")
                    client = self.client_factory(account)
                    items = client.scan(lambda count: self._update(job_id, index, total=count))
                    snapshots.append((account["access_token"], items))
                    self._update(job_id, index, phase="ready", total=len(items), remaining=len(items))
                except Exception as exc:
                    failed = True
                    self._update(job_id, index, phase="failed", error=self._error(exc))
                finally:
                    self._close_client(client)
            with self.lock:
                if not failed:
                    self.snapshots[job_id] = snapshots
            self._update(job_id, status="failed" if failed else "ready")
        finally:
            with self.lock:
                self.active = False

    @staticmethod
    def _close_client(client):
        if client is not None:
            try:
                client.close()
            except Exception:
                # 关闭连接失败不能盖过删除结果或让任务永久停留在运行中。
                pass

    @staticmethod
    def _error(exc):
        # 不返回原始网络异常或响应体，避免账号 token、代理口令流入页面和日志。
        return str(exc) if isinstance(exc, ImageSpaceError) else "操作异常，结果未完整确认，请重试"

    def execute(self, job_id):
        with self.lock:
            self._prune()
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if self.active or job["status"] != "ready":
                raise ImageSpaceError("任务正在运行或预览不可执行，请重新预览")
            selected = self.snapshots.pop(job_id)
            job["status"] = "running"
            job["updated_at"] = time.time()
            self.active = True
        Thread(target=self._execute, args=(job_id, selected), daemon=True).start()

    def _execute(self, job_id, selected):
        failed = False
        try:
            for index, (token, snapshot) in enumerate(selected):
                client = None
                try:
                    account = self.accounts.get_account(token)
                    if not account or account.get("source_type") == "adobe":
                        raise ImageSpaceError("账号已被移除，请重新加载号池")
                    if snapshot:
                        client = self.client_factory(account)
                        clear_snapshot(client, snapshot, lambda **fields: self._update(job_id, index, **fields))
                    self._update(job_id, index, phase="completed", remaining=0)
                except Exception as exc:
                    failed = True
                    self._update(job_id, index, phase="failed", error=self._error(exc))
                finally:
                    self._close_client(client)
            self._update(job_id, status="partial" if failed else "completed")
        finally:
            with self.lock:
                self.active = False


image_space_service = ImageSpaceService()


def start_image_space_scheduler(stop_event):
    def run():
        while not stop_event.is_set():
            try:
                image_space_service.run_scheduled()
            except Exception:
                # 不记录上游异常原文，避免凭据进入后台日志。
                pass
            stop_event.wait(60)
    thread = Thread(target=run, name="account-image-cleanup", daemon=True)
    thread.start()
    return thread
