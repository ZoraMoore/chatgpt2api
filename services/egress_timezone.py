"""按出口缓存时区；地理查询使用独立客户端，绝不携带 ChatGPT 凭据。"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from curl_cffi import CurlOpt, requests

PROVIDERS = ("ipwho", "ipapi", "freeipapi")
DETECT_TIMEOUT = 4.0
REFRESH_INTERVAL = 600.0
FAILURE_BACKOFF = 60.0
MAX_STALE = 86400.0
MAX_ENTRIES = 256


def valid_timezone(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_+/-]{1,128}", value):
        return None
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return value


@dataclass(frozen=True)
class EgressTimezone:
    name: str = "UTC"
    source: str = "fallback"

    def fields(self, timestamp: float | None = None) -> dict[str, str | int]:
        instant = datetime.now(timezone.utc) if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc)
        offset = instant.astimezone(ZoneInfo(self.name)).utcoffset()
        # 网页采用 Date.getTimezoneOffset() 符号，与 UTC 偏移方向相反。
        return {"timezone": self.name, "timezone_offset_min": -int(offset.total_seconds() / 60)}


def parse_provider_timezone(provider: str, data: object, expected_ip: str) -> str | None:
    if not isinstance(data, dict) or data.get("success") is False or data.get("error"):
        return None
    try:
        if ipaddress.ip_address(data.get("ip") or data.get("ipAddress")) != ipaddress.ip_address(expected_ip):
            return None
    except (TypeError, ValueError):
        return None
    if provider == "ipwho":
        value = data.get("timezone")
        return valid_timezone(value.get("id") if isinstance(value, dict) else None)
    if provider == "ipapi":
        return valid_timezone(data.get("timezone"))
    if provider == "freeipapi":
        values = data.get("timeZones")
        # 此字段可能是国家全部时区，并非当前 IP 的时区；不能盲取第一项。
        if isinstance(values, list) and len(values) == 1:
            return valid_timezone(values[0])
    return None


class EgressTimezoneCache:
    def __init__(self, cache_path: Path | None = None) -> None:
        self.path = cache_path or Path(__file__).resolve().parents[1] / "data" / "egress_timezones.json"
        self._lock = threading.RLock()
        self._entries: dict[str, dict] = {}
        self._flights: dict[str, Future] = {}
        self._retry_at: dict[str, float] = {}
        self._loaded = False
        # 刷新和探测分开排队，避免协调任务占满执行探测的线程而相互等待。
        self._refresh_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="timezone-refresh")
        self._probe_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="timezone-probe")

    def resolve(self, proxy_url: str = "", impersonate: str = "chrome110") -> EgressTimezone:
        if not isinstance(proxy_url, str) or len(proxy_url) > 8192:
            return EgressTimezone()
        key = hashlib.sha256((proxy_url or "direct").encode()).hexdigest()
        now = time.time()
        with self._lock:
            self._load(now)
            record = self._entries.get(key)
            cached = (EgressTimezone(record["name"], record["source"])
                      if record and now - record["checked_at"] <= MAX_STALE else None)
            if cached and now - record["checked_at"] < REFRESH_INTERVAL:
                return cached
            future = self._flights.get(key)
            if future is None and now >= self._retry_at.get(key, 0) and len(self._flights) < 16:
                if len(self._retry_at) >= MAX_ENTRIES:
                    self._retry_at.pop(min(self._retry_at, key=self._retry_at.get))
                self._retry_at[key] = now + FAILURE_BACKOFF
                future = self._refresh_pool.submit(
                    self._refresh, key, proxy_url, impersonate, time.monotonic() + DETECT_TIMEOUT,
                )
                self._flights[key] = future
                future.add_done_callback(lambda done, route=key: self._finish(route, done))
            # 已有可用缓存时立即使用，过期校准在后台完成；一次调用固定一个时区。
            if cached:
                return cached
        if future is not None:
            try:
                return future.result(timeout=DETECT_TIMEOUT + 0.1) or EgressTimezone()
            except Exception:
                pass
        return EgressTimezone()

    def _finish(self, key: str, future: Future) -> None:
        with self._lock:
            if self._flights.get(key) is future:
                self._flights.pop(key, None)

    def _load(self, now: float) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self.path.stat().st_size > 131072:
                return
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            records = payload.get("entries") if isinstance(payload, dict) and payload.get("version") == 1 else None
            if not isinstance(records, dict) or len(records) > MAX_ENTRIES:
                return
            for key, item in records.items():
                if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(item, dict):
                    continue
                stamp = item.get("checked_at")
                if (not valid_timezone(item.get("name")) or item.get("source") not in PROVIDERS
                        or isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                        or not math.isfinite(stamp) or not now - MAX_STALE <= stamp <= now + 60):
                    continue
                self._entries[key] = {"name": item["name"], "source": item["source"], "checked_at": stamp}
        except (OSError, ValueError, RecursionError):
            pass

    def _save(self) -> None:
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".egress-timezones-", delete=False) as output:
                temporary = Path(output.name)
                json.dump({"version": 1, "entries": self._entries}, output, separators=(",", ":"))
            os.replace(temporary, self.path)
        except OSError:
            # 缓存写入失败不影响本次请求，也不记录可能含代理凭据的异常原文。
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _request(url: str, proxy_url: str, impersonate: str, deadline: float) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timezone detection deadline exceeded")
        with requests.Session(impersonate=impersonate, proxy=proxy_url or None, verify=True,
                              curl_options={CurlOpt.TIMEOUT_MS: max(1, int(remaining * 1000))}) as session:
            response = session.get(url, headers={"Accept": "application/json,text/plain"},
                                   timeout=(min(1.5, remaining), remaining), allow_redirects=False, stream=True)
            try:
                if response.status_code != 200:
                    raise ValueError("timezone endpoint failed")
                body = bytearray()
                for chunk in response.iter_content():
                    if len(body) + len(chunk) > 32768:
                        raise ValueError("timezone response is too large")
                    body.extend(chunk)
                return bytes(body)
            finally:
                response.close()

    def _lookup(self, provider: str, ip: str, proxy: str, impersonate: str, deadline: float) -> str | None:
        urls = {"ipwho": f"https://ipwho.is/{ip}", "ipapi": f"https://ipapi.co/{ip}/json/",
                "freeipapi": f"https://free.freeipapi.com/api/v1/json/{ip}"}
        return parse_provider_timezone(provider, json.loads(self._request(urls[provider], proxy, impersonate, deadline)), ip)

    def _refresh(self, key: str, proxy: str, impersonate: str, deadline: float) -> EgressTimezone | None:
        futures: dict[str, Future] = {}
        try:
            # 同一个代理对不同域名可能采用不同出口，先读取 ChatGPT 看到的 IP，
            # 再将该 IP 显式传给地理接口，避免探测到地理接口自身路径的出口。
            trace = self._request("https://chatgpt.com/cdn-cgi/trace", proxy, impersonate, deadline).decode("utf-8")
            values = dict(line.split("=", 1) for line in trace.splitlines() if "=" in line)
            address = ipaddress.ip_address(values.get("ip", ""))
            if not address.is_global:
                return None
            for provider in PROVIDERS:
                futures[provider] = self._probe_pool.submit(self._lookup, provider, str(address), proxy, impersonate, deadline)
            # 三家并发发起，但固定优先级，避免地理库分歧导致时区随网络快慢抖动。
            for provider, future in futures.items():
                try:
                    name = future.result(timeout=max(0.0, deadline - time.monotonic()))
                except Exception:
                    continue
                if not valid_timezone(name):
                    continue
                result = EgressTimezone(name, provider)
                with self._lock:
                    self._entries[key] = {"name": name, "source": provider, "checked_at": time.time()}
                    if len(self._entries) > MAX_ENTRIES:
                        self._entries.pop(min(self._entries, key=lambda item: self._entries[item]["checked_at"]))
                    self._save()
                return result
        except Exception:
            pass
        finally:
            for future in futures.values():
                future.cancel()
        return None


egress_timezones = EgressTimezoneCache()
