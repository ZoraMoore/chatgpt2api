#!/usr/bin/env python3
"""检测原生首轮生图的模型字段；离线解析不联网，live 使用隔离的线上运行代码。"""
from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MAX_CAPTURE_BYTES = 32 * 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 10_000
MAX_FIELDS = 256
PROBE_PRIVATE_DIR = Path("/probe-private")
PROBE_OUTPUT_DIR = Path("/probe-output")
MODEL_FIELDS = ("resolved_model_slug", "model_slug", "default_model_slug", "model")
# 只输出模型名称，未知值仍记为“字段存在”，但不原样写出。
MODEL_NAME = re.compile(r"(?:gpt|chatgpt|o[1-9]|auto|dall-e|sora)[a-z0-9_.:-]{0,80}", re.I)


class ProbeError(Exception):
    """错误文本只使用脚本定义的固定标识，禁止带出上游错误原文和凭据。"""


def read_limited(path: Path, limit: int = MAX_CAPTURE_BYTES) -> bytes:
    if not path.is_file() or path.stat().st_size > limit:
        raise ProbeError("文件不存在或超过大小限制")
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ProbeError("文件超过大小限制")
    return value


def write_report(path: Path, report: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


class StreamAudit:
    """检测实际消息元数据，复用生产补丁解析器；不扫描提示词、推理正文和工具代码。"""

    def __init__(self) -> None:
        from services.protocol.final_image import FinalImageTracker

        self.tracker = FinalImageTracker()
        self.event_count = 0
        self.done = False
        self.parse_errors = 0
        self.limited = False
        self.fields: list[dict] = []
        self._seen: set[tuple] = set()
        self.snapshot_available = False
        self.snapshot_complete = False
        self.snapshot_error = False

    def _fields(self, value: object, source: str, location: str) -> None:
        if not isinstance(value, dict):
            return
        for key in MODEL_FIELDS:
            if key not in value:
                continue
            raw = value[key]
            state, slug = "invalid_redacted", None
            if raw is None:
                state = "null"
            elif raw == "":
                state = "empty"
            elif isinstance(raw, str) and MODEL_NAME.fullmatch(raw):
                state, slug = "value", raw
            signature = (source, location, key, state, slug)
            if signature in self._seen:
                continue
            if len(self.fields) >= MAX_FIELDS:
                self.limited = True
                continue
            self._seen.add(signature)
            field = {"source": source, "location": location + "." + key, "field": key, "state": state}
            if slug is not None:
                field["value"] = slug
            if source == "sse":
                field["event_index"] = self.event_count
            self.fields.append(field)

    def _message(self, value: object, source: str) -> None:
        if isinstance(value, dict) and isinstance(value.get("author"), dict):
            if value["author"].get("role") == "assistant":
                self._fields(value.get("metadata"), source, "assistant.metadata")

    def feed(self, payload: str) -> None:
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_EVENT_BYTES:
            self.limited = True
            return
        if payload.strip() == "[DONE]":
            self.done = True
            self.tracker.stream_complete = True
            return
        if self.done:
            # 一份报告只检查一轮完整响应，不能把后续会话的字段混入这一轮。
            self.parse_errors += 1
            return
        self.event_count += 1
        if self.event_count > MAX_EVENTS:
            self.limited = True
            return
        try:
            event = json.loads(payload)
            if event == "v1":  # 网页 SSE 编码版本声明不是消息。
                return
            if not isinstance(event, dict):
                raise ValueError("invalid event")
            self._fields(event, "sse", "event")
            if event.get("type") == "server_ste_metadata":
                self._fields(event.get("metadata"), "sse", "server_ste_metadata")
            self.tracker.update(event)
            self._message(self.tracker.current, "sse")
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            self.parse_errors += 1

    def snapshot(self, document: object) -> None:
        if not isinstance(document, dict):
            self.snapshot_error = True
            return
        self.snapshot_available = True
        self._fields(document, "conversation_response", "conversation")
        messages: list = []
        mapping = document.get("mapping")
        flat = document.get("messages")
        if isinstance(mapping, dict):
            messages = [node.get("message") for node in mapping.values() if isinstance(node, dict)]
        elif isinstance(flat, list):
            messages = [node.get("message", node) if isinstance(node, dict) else None for node in flat]
        # 空对象或未知响应格式不能用来证明字段缺失。
        has_assistant = any(isinstance(item, dict) and isinstance(item.get("author"), dict)
                            and item["author"].get("role") == "assistant" for item in messages)
        self.snapshot_complete = has_assistant and len(messages) <= MAX_EVENTS
        if len(messages) > MAX_EVENTS:
            self.limited = True
        for message in messages[:MAX_EVENTS]:
            self._message(message, "conversation_response")

    def presence(self, source: str, complete: bool) -> dict:
        found = [item for item in self.fields if item["source"] == source and item["field"] == "resolved_model_slug"]
        return {
            "presence": "present" if found else ("absent" if complete else "inconclusive"),
            "values": sorted({item["value"] for item in found if "value" in item}),
            "value_states": sorted({item["state"] for item in found}),
        }

    def report(self, *, mode: str, generation: dict | None = None) -> dict:
        successful = mode == "capture" or bool(generation and generation.get("successful"))
        sse_complete = self.done and not self.parse_errors and not self.limited
        summary = self.tracker.execution_summary("observed")
        return {
            "schema_version": 1,
            "mode": mode,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generation": generation,
            "sse": {
                "event_count": self.event_count,
                "done_received": self.done,
                "parse_errors": self.parse_errors,
                "analysis_limited": self.limited,
                "resolved_model_slug": self.presence("sse", successful and sse_complete),
            },
            "conversation_response": {
                "available": self.snapshot_available,
                "recognized": self.snapshot_complete,
                "read_failed": self.snapshot_error,
                "resolved_model_slug": self.presence(
                    "conversation_response", successful and self.snapshot_complete and not self.snapshot_error and not self.limited,
                ),
            },
            "model_fields": list(self.fields),
            "execution": {
                "thought_message_count": summary["thought_message_count"],
                "code_tool_after_image": summary["code_tool_after_image"],
                "image_tool_observed": self.tracker.generated,
            },
            "interpretation": "仅判定字段是否存在；字段缺失、模型标签或 PoW 难度均不能单独证明降智。",
        }


def parse_sse(text: str, audit: StreamAudit) -> None:
    data: list[str] = []
    for line in text.splitlines():
        if line == "":
            if data:
                audit.feed("\n".join(data))
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        audit.feed("\n".join(data))
    if audit.event_count == 0:
        audit.parse_errors += 1


def analyze_har(path: Path, entry_index: int | None) -> dict:
    document = json.loads(read_limited(path))
    entries = document.get("log", {}).get("entries", []) if isinstance(document, dict) else []
    if not isinstance(entries, list) or len(entries) > MAX_EVENTS:
        raise ProbeError("无效 HAR")
    matches = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") or {}
        if isinstance(request, dict) and request.get("method") == "POST":
            if urlsplit(str(request.get("url") or "")).path in {"/backend-api/f/conversation", "/backend-api/conversation"}:
                matches.append(index)
    if entry_index is None:
        if len(matches) != 1:
            raise ProbeError("HAR 必须只有一条生成请求，或使用 --entry-index 明确选择，避免混合会话")
        entry_index = matches[0]
    if entry_index not in matches:
        raise ProbeError("所选 HAR 条目不是 conversation 生成 POST")
    entry = entries[entry_index]
    response = entry.get("response") or {}
    if not isinstance(response, dict):
        raise ProbeError("无效 HAR 响应")
    audit = StreamAudit()
    events = entry.get("_eventSourceMessages")
    content = response.get("content") or {}
    payloads = [event["data"] for event in events if isinstance(event, dict) and isinstance(event.get("data"), str)] if isinstance(events, list) else []
    stream_source = "unavailable"
    if isinstance(events, list) and events and len(payloads) == len(events):
        stream_source = "event_source_messages"
        if len(events) > MAX_EVENTS:
            audit.limited = True
        for payload in payloads[:MAX_EVENTS]:
            audit.feed(payload)
    elif isinstance(content, dict) and isinstance(content.get("text"), str) and content["text"]:
        stream_source = "response_content"
        text = content["text"]
        if content.get("encoding") == "base64":
            text = base64.b64decode(text, validate=True).decode("utf-8")
        parse_sse(text, audit)
    else:
        # 部分 HAR 只导出事件名称和时间，没有 data，不能据此断言模型字段不存在。
        for payload in payloads[:MAX_EVENTS]:
            audit.feed(payload)
        if len(payloads) > MAX_EVENTS:
            audit.limited = True
        if payloads:
            stream_source = "partial_event_source_messages"
        audit.parse_errors += 1
    successful = response.get("status") == 200
    report = audit.report(mode="capture" if successful else "failed_capture")
    report["capture"] = {"format": "har", "entry_index": entry_index, "http_status": response.get("status"),
                         "event_records": len(events) if isinstance(events, list) else 0,
                         "event_payload_records": len(payloads), "stream_source": stream_source}
    return report


def run_docker(arguments: list[str], *, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *arguments], capture_output=True, timeout=timeout, check=False)
    if check and result.returncode:
        # Docker 输出可能包含配置或上游错误，失败时只显示固定提示。
        raise ProbeError("Docker 操作失败；未输出原始日志")
    return result


def select_probe_accounts(accounts: list[dict], email: str | None, fingerprint: str | None) -> list[dict]:
    if fingerprint and not re.fullmatch(r"[0-9a-f]{12,64}", fingerprint):
        raise ProbeError("令牌指纹须为 SHA-256 的 12 到 64 位小写十六进制前缀")
    selected = [item for item in accounts if (item.get("source_type") or "web") == "web"
                and isinstance(item.get("access_token"), str) and item["access_token"]]
    if email:
        selected = [item for item in selected if str(item.get("email") or "").casefold() == email.casefold()]
    if fingerprint:
        selected = [item for item in selected if hashlib.sha256(item["access_token"].encode()).hexdigest().startswith(fingerprint)]
    if not selected:
        raise ProbeError("当前线上账号池中没有符合条件的网页账号")
    # 导入按令牌去重，同一邮箱可能同时保留旧会话和新会话；明确选号时不能随机选到旧令牌。
    if (email or fingerprint) and len({item["access_token"] for item in selected}) > 1:
        raise ProbeError("该选择对应多个令牌，请用 --token-fingerprint 明确选择会话")
    return selected


def run_live(args: argparse.Namespace) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", args.container):
        raise ProbeError("无效容器名称")
    prompt = read_limited(args.prompt_file, 128 * 1024).decode("utf-8")
    if not prompt.strip():
        raise ProbeError("提示词不能为空")
    image = read_limited(args.image_file, 20 * 1024 * 1024) if args.image_file else None
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    output_dir = args.output_dir.resolve()
    # 显式捕获到进程内存；不写出含有环境变量的 inspect 原文。
    inspect = json.loads(run_docker(["inspect", "--format", "{{json .}}", args.container]).stdout)
    if not inspect.get("State", {}).get("Running") or not re.fullmatch(r"sha256:[0-9a-f]{64}", inspect.get("Image", "")):
        raise ProbeError("目标容器未运行或镜像标识无效")
    if any((item.partition("=")[0] == "STORAGE_BACKEND" and item.partition("=")[2] != "json")
           for item in inspect.get("Config", {}).get("Env", [])):
        raise ProbeError("当前脚本仅支持 JSON 账号池，不能猜测数据库账号来源")
    networks = list(inspect.get("NetworkSettings", {}).get("Networks", {}))
    if len(networks) != 1 or networks[0] in {"host", "none"}:
        raise ProbeError("当前脚本需要目标容器使用单个 Docker bridge 网络")
    probe_name = "image-protocol-probe-" + secrets.token_hex(6)
    report_path = output_dir / "report.json"
    with tempfile.TemporaryDirectory(prefix="image-protocol-private-") as temporary:
        private = Path(temporary)
        data = private / "data"
        data.mkdir(mode=0o700)
        try:
            for name in ("services", "utils", "config.json"):
                run_docker(["cp", f"{args.container}:/app/{name}", str(private / name)])
            run_docker(["cp", f"{args.container}:/app/data/accounts.json", str(data / "accounts.json")])
            accounts = json.loads(read_limited(data / "accounts.json"))
            if not isinstance(accounts, list) or not all(isinstance(item, dict) for item in accounts):
                raise ProbeError("未知账号池格式")
            accounts = select_probe_accounts(accounts, args.account_email, getattr(args, "token_fingerprint", None))
            write_report(data / "accounts.json", accounts)
            configuration = json.loads(read_limited(private / "config.json"))
            if not isinstance(configuration, dict):
                raise ProbeError("未知配置格式")
            configuration["auth-key"] = secrets.token_hex(32)
            # 保留实际模型、提示词、代理及超时配置；测试产物只保存到隔离目录。
            configuration["image_storage"] = {"enabled": False, "mode": "local", "public_base_url": ""}
            write_report(private / "config.json", configuration)
            (private / "prompt.txt").write_text(prompt, encoding="utf-8")
            if image is not None:
                (private / "reference.bin").write_bytes(image)
            (private / "isolated-marker").write_text(probe_name, encoding="utf-8")
            command = [
                "run", "--name", probe_name, "--read-only", "--log-driver=none", "--network", networks[0],
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--cpus", "1", "--memory", "768m", "--pids-limit", "128",
                "--tmpfs", "/tmp:rw,nosuid,noexec,size=32m", "-e", "STORAGE_BACKEND=json",
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "IMAGE_PROTOCOL_PROBE_ISOLATED=1",
                "-e", "IMAGE_PROTOCOL_PROBE_MODEL=" + args.model,
            ]
            # 复用线上显式环境代理和上游地址；凭据仅放入临时环境文件。
            env_names = {"CHATGPT2API_BASE_URL", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                         "http_proxy", "https_proxy", "all_proxy", "no_proxy"}
            environment = [item for item in inspect.get("Config", {}).get("Env", []) if item.partition("=")[0] in env_names]
            if any("\n" in item or "\r" in item for item in environment):
                raise ProbeError("无效代理环境变量")
            (private / "runtime.env").write_text("\n".join(environment) + "\n", encoding="utf-8")
            command += ["--env-file", str(private / "runtime.env")]
            mounts = [(private / "services", "/app/services", True), (private / "utils", "/app/utils", True),
                      (private / "config.json", "/app/config.json", True), (data, "/app/data", False),
                      (private, "/probe-private", True), (output_dir, "/probe-output", False),
                      (Path(__file__).resolve(), "/probe-tool/test_image_protocol.py", True)]
            for source, target, readonly in mounts:
                if "," in str(source):
                    raise ProbeError("挂载路径不能含逗号")
                command += ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]
            command += ["--entrypoint", "/app/.venv/bin/python", inspect["Image"], "/probe-tool/test_image_protocol.py", "_worker"]
            try:
                result = run_docker(command, timeout=args.timeout, check=False)
                if not report_path.exists():
                    raise ProbeError("隔离测试未生成报告；未输出原始日志")
                report = json.loads(read_limited(report_path))
                report["worker_exit_code"] = result.returncode
            except subprocess.TimeoutExpired:
                # 先停止仍在运行的 worker，避免它覆盖宿主机写出的超时判定。
                run_docker(["rm", "--force", probe_name], check=False)
                report = json.loads(read_limited(report_path)) if report_path.exists() else StreamAudit().report(mode="live")
                report["generation"] = {"successful": False, "image_count": 0, "failure_type": "ProbeTimeout"}
                for source in ("sse", "conversation_response"):
                    if report[source]["resolved_model_slug"]["presence"] == "absent":
                        report[source]["resolved_model_slug"]["presence"] = "inconclusive"
            report["runtime"] = {
                "source_container": args.container, "source_image": inspect["Image"],
                "code_sha256": {name: hashlib.sha256(read_limited(private / name)).hexdigest()
                                for name in ("services/openai_backend_api.py", "services/protocol/conversation.py")},
                "protocol_handler": "/v1/images/edits" if image is not None else "/v1/images/generations",
                "http_wrapper_tested": False,
                "isolated_data": True, "max_generation_posts": 1,
            }
            write_report(report_path, report)
            return report
        finally:
            run_docker(["rm", "--force", probe_name], check=False)


def run_worker() -> int:
    if os.getenv("IMAGE_PROTOCOL_PROBE_ISOLATED") != "1" or not (PROBE_PRIVATE_DIR / "isolated-marker").is_file():
        raise ProbeError("实测 worker 只能在脚本创建的隔离容器运行")
    sys.path.insert(0, "/app")
    report_path = PROBE_OUTPUT_DIR / "report.json"
    # 库日志可能含账号或上游原文，探针仅保存明确白名单中的元数据。
    from utils.log import logger
    for level in ("info", "debug", "warning", "error"):
        setattr(logger, level, lambda *args, **kwargs: None)
    audit = StreamAudit()
    calls: list[dict] = []
    blocked_posts = 0
    account_fingerprints: set[str] = set()
    token_fingerprints: set[str] = set()
    generation = {"successful": False, "image_count": 0}
    started = time.monotonic()

    def save() -> None:
        report = audit.report(mode="live", generation=dict(generation))
        report.update(upstream_generation_calls=list(calls), blocked_additional_posts=blocked_posts,
                      account_fingerprints=sorted(account_fingerprints), token_fingerprints=sorted(token_fingerprints),
                      duration_seconds=round(time.monotonic() - started, 3))
        write_report(report_path, report)

    with open(os.devnull, "w") as discarded, redirect_stdout(discarded), redirect_stderr(discarded):
        try:
            from services.openai_backend_api import OpenAIBackendAPI
            from services.protocol import openai_v1_image_edit, openai_v1_image_generations
            from PIL import Image

            original_init = OpenAIBackendAPI.__init__
            original_stream = OpenAIBackendAPI.stream_conversation
            original_get = OpenAIBackendAPI._get_conversation

            def observed_init(backend, *args, **kwargs):
                original_init(backend, *args, **kwargs)
                email = str(backend.account.get("email") or "unknown")
                account_fingerprints.add(hashlib.sha256(email.encode()).hexdigest()[:12])
                access_token = getattr(backend, "access_token", "")
                if isinstance(access_token, str) and access_token:
                    token_fingerprints.add(hashlib.sha256(access_token.encode()).hexdigest()[:12])
                original_post = backend.session.post

                def observed_post(url, *post_args, **post_kwargs):
                    nonlocal blocked_posts
                    path = urlsplit(str(url)).path
                    if path not in {"/backend-api/f/conversation", "/backend-api/conversation"}:
                        return original_post(url, *post_args, **post_kwargs)
                    # 不为“测到字段”重试生成；轮询和只读会话查询不受此限制。
                    if calls:
                        blocked_posts += 1
                        save()
                        raise ProbeError("已阻止额外生成 POST")
                    payload = post_kwargs.get("json") or {}
                    call = {"path": path, "http_status": None}
                    if isinstance(payload, dict):
                        model = payload.get("model")
                        call["model"] = model if isinstance(model, str) and MODEL_NAME.fullmatch(model) else None
                        effort = payload.get("thinking_effort")
                        call["thinking_effort"] = effort if isinstance(effort, str) and effort in {"low", "medium", "high", "extended"} else None
                        call["new_conversation"] = "conversation_id" not in payload
                    calls.append(call)
                    save()
                    response = original_post(url, *post_args, **post_kwargs)
                    call["http_status"] = response.status_code
                    save()
                    return response

                backend.session.post = observed_post

            def observed_get(backend, conversation_id):
                try:
                    document = original_get(backend, conversation_id)
                    audit.snapshot(document)
                    return document
                except Exception:
                    audit.snapshot_error = True
                    raise
                finally:
                    save()

            def observed_stream(backend, *args, **kwargs):
                try:
                    for raw in original_stream(backend, *args, **kwargs):
                        audit.feed(raw)
                        save()
                        yield raw
                finally:
                    cid = audit.tracker.conversation_id
                    if cid and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cid) and not audit.snapshot_available:
                        try:
                            observed_get(backend, cid)
                        except Exception:
                            pass
                    save()

            OpenAIBackendAPI.__init__ = observed_init
            OpenAIBackendAPI.stream_conversation = observed_stream
            OpenAIBackendAPI._get_conversation = observed_get
            prompt = read_limited(PROBE_PRIVATE_DIR / "prompt.txt", 128 * 1024).decode("utf-8")
            body = {"prompt": prompt, "model": os.environ.get("IMAGE_PROTOCOL_PROBE_MODEL", "gpt-image-2"),
                    "n": 1, "size": "auto", "quality": "auto", "response_format": "b64_json", "stream": False}
            reference = PROBE_PRIVATE_DIR / "reference.bin"
            handle = openai_v1_image_generations.handle
            if reference.exists():
                raw_reference = read_limited(reference, 20 * 1024 * 1024)
                with Image.open(BytesIO(raw_reference)) as decoded:
                    mime = Image.MIME.get(decoded.format)
                    decoded.verify()
                if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                    raise ProbeError("不支持的参考图格式")
                body["images"] = [(raw_reference, "reference", mime)]
                handle = openai_v1_image_edit.handle
            save()
            result = handle(body)
            rows = result.get("data", []) if isinstance(result, dict) else []
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                raise ProbeError("没有返回唯一图片")
            encoded = rows[0].get("b64_json")
            if not isinstance(encoded, str) or len(encoded) > 90 * 1024 * 1024:
                raise ProbeError("返回图片编码无效")
            generated = base64.b64decode(encoded, validate=True)
            with Image.open(BytesIO(generated)) as decoded:
                image_format, dimensions = decoded.format, decoded.size
                decoded.verify()
            with Image.open(BytesIO(generated)) as decoded:
                decoded.load()
            suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}.get(image_format)
            if suffix is None:
                raise ProbeError("返回图片格式不支持")
            image_path = PROBE_OUTPUT_DIR / ("generated" + suffix)
            image_path.write_bytes(generated)
            image_path.chmod(0o600)
            generation.update(successful=True, image_count=1, image_file=image_path.name,
                              image_format=image_format, dimensions=list(dimensions),
                              image_bytes=len(generated), image_sha256=hashlib.sha256(generated).hexdigest())
        except Exception as exc:
            generation["failure_type"] = type(exc).__name__
        finally:
            save()
    return 0 if generation["successful"] else 2


def exit_code(report: dict) -> int:
    if report.get("worker_exit_code", 0) != 0:
        return 2
    if report.get("mode") != "capture" and not (report.get("generation") or {}).get("successful"):
        return 2
    presence = report["sse"]["resolved_model_slug"]["presence"]
    return {"present": 0, "absent": 1, "inconclusive": 2}[presence]


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    for name in ("sse", "har"):
        command = commands.add_parser(name, help="离线检测现有抓包，不生成图片")
        command.add_argument("file", type=Path)
        command.add_argument("--report", required=True, type=Path)
        if name == "har":
            command.add_argument("--entry-index", type=int, help="HAR entries 从 0 开始计数")
    live = commands.add_parser("live", help="使用线上代码、镜像和账号副本实测；消耗一次生图额度")
    live.add_argument("--container", default="chatgpt2api-image")
    live.add_argument("--model", choices=("gpt-image-2", "gpt-image-2.5"), default="gpt-image-2")
    live.add_argument("--prompt-file", required=True, type=Path)
    live.add_argument("--image-file", type=Path)
    live.add_argument("--account-email")
    live.add_argument("--token-fingerprint", help="令牌 SHA-256 的 12 到 64 位前缀，用于同邮箱多个会话的明确选择")
    live.add_argument("--output-dir", required=True, type=Path, help="必须是尚未存在的目录")
    live.add_argument("--timeout", type=int, default=1800)
    commands.add_parser("_worker", help="内部隔离 worker，不能在宿主机直接实测")
    args = parser.parse_args()
    try:
        if args.mode == "_worker":
            return run_worker()
        if args.mode == "live":
            if not 30 <= args.timeout <= 3600:
                raise ProbeError("超时必须在 30 到 3600 秒之间")
            if args.account_email and (len(args.account_email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+", args.account_email)):
                raise ProbeError("无效账号邮箱")
            report = run_live(args)
        elif args.mode == "har":
            report = analyze_har(args.file, args.entry_index)
            write_report(args.report, report)
        else:
            audit = StreamAudit()
            parse_sse(read_limited(args.file).decode("utf-8"), audit)
            report = audit.report(mode="capture")
            report["capture"] = {"format": "sse", "image_verified": False}
            write_report(args.report, report)
        print(json.dumps({"resolved_model_slug": report["sse"]["resolved_model_slug"],
                          "generation_successful": (report.get("generation") or {}).get("successful"),
                          "report": str(args.output_dir / "report.json" if args.mode == "live" else args.report)}, ensure_ascii=False))
        return exit_code(report)
    except (ProbeError, OSError, ValueError, RecursionError, subprocess.SubprocessError) as exc:
        print(str(exc) if isinstance(exc, ProbeError) else "测试未完成：" + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
