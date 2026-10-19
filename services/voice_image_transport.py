"""用持续的静音 WebRTC 会话承载图片消息，复用现有图片调度和交付链。"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import queue
import re
import threading
import time
import uuid

from aiortc import AudioStreamTrack, RTCBundlePolicy, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from curl_cffi import CurlMime

from services.voice_delta import DeltaDecoder
from services.voice_protocol import normalize_bundle_candidates, pack_event, session_payload, unpack_event
from utils.log import logger


def voice_images_enabled() -> bool:
    return os.getenv("CHATGPT2API_IMAGE_TRANSPORT", "").strip().lower() == "voice"


class VoiceImageError(RuntimeError):
    def __init__(self, code: str, conversation_id: str = ""):
        self.code = code
        self.conversation_id = conversation_id
        super().__init__({
            "handshake_rejected": "语音会话创建被上游拒绝。",
            "connection_failed": "语音连接中断，未重复提交生成请求。",
            "deadline_exceeded": "语音生成等待超时，未重复提交生成请求。",
            "proxy_not_supported": "语音 WebRTC 需要与主机一致的网络出口，暂不支持单独的 HTTP 代理。",
            "invalid_event": "语音消息解析失败，未重复提交生成请求。",
            "consumer_stalled": "图片事件处理超时，语音连接已结束。",
            "upstream_error": "上游语音任务未完成。",
        }.get(code, "语音图片生成未完成。"))


class VoiceTurnState:
    """完成判定同时检查本轮消息和 spawn；LISTENING 仅用于连接就绪。"""
    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.active_spawns: set[str] = set()
        self.saw_spawn = False
        self.saw_spawn_end = False
        self.cancelled = False
        self.last_change = time.monotonic()
        self.conversation_id = ""
        self.message_sizes: dict[str, int] = {}

    def message(self, value: dict):
        message_id = value.get("id")
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 128:
            raise VoiceImageError("invalid_event")
        if message_id not in self.messages and len(self.messages) >= 2000:
            raise VoiceImageError("invalid_event")
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if size > 1024 * 1024 or sum(self.message_sizes.values()) - self.message_sizes.get(message_id, 0) + size > 8 * 1024 * 1024:
            raise VoiceImageError("invalid_event")
        self.messages[message_id] = copy.deepcopy(value)
        self.message_sizes[message_id] = size
        self.last_change = time.monotonic()

    def spawn(self, payload: dict):
        spawn_id, state = payload.get("spawn_id"), payload.get("state")
        if not isinstance(spawn_id, str) or not 1 <= len(spawn_id) <= 128:
            raise VoiceImageError("invalid_event")
        if state not in {"start", "update", "end", "cancel"}:
            raise VoiceImageError("invalid_event")
        self.saw_spawn = True
        if state in {"end", "cancel"}:
            self.active_spawns.discard(spawn_id)
            self.saw_spawn_end = self.saw_spawn_end or state == "end"
            self.cancelled = self.cancelled or state == "cancel"
        else:
            if len(self.active_spawns) >= 128:
                raise VoiceImageError("invalid_event")
            self.active_spawns.add(spawn_id)
        self.last_change = time.monotonic()

    def complete(self, now: float) -> bool:
        if self.cancelled or self.active_spawns or not self.saw_spawn_end or now - self.last_change < 1.0:
            return False
        visible = [m for m in self.messages.values()
                   if (m.get("author") or {}).get("role") in {"assistant", "tool"}
                   and not (m.get("metadata") or {}).get("is_visually_hidden_from_conversation")]
        if not visible or any(m.get("status") == "in_progress" for m in visible):
            return False
        return any(m.get("status") == "finished_successfully" for m in visible)


class VoiceImageSession:
    def __init__(self, backend, message: dict, model: str, timeout_secs: float):
        if not isinstance(message, dict) or len(json.dumps(message, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
            raise VoiceImageError("invalid_event")
        self.backend, self.message, self.model = backend, copy.deepcopy(message), model
        self.started = time.monotonic()
        self.deadline = self.started + timeout_secs
        self.events: queue.Queue = queue.Queue(maxsize=16)
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.state = VoiceTurnState()
        self.decoder = DeltaDecoder()
        self.thread: threading.Thread | None = None
        self.sent = False
        self.completed = False
        self.fatal: VoiceImageError | None = None
        self.pc = None
        self.channel = None
        self.audio = None
        self.loop = None
        self.runner = None
        self.stop_async = None
        self.media_tasks: set[asyncio.Task] = set()
        self.diagnostics = {"transport": "voice_webrtc", "session_id": uuid.uuid4().hex,
                            "stage": "created", "text_submissions": 0, "events": {}, "states": []}

    def diagnostic_snapshot(self):
        with self.lock:
            return copy.deepcopy(self.diagnostics)

    def _diagnostic(self, **fields):
        with self.lock:
            self.diagnostics.update(fields)

    def _state_snapshot(self):
        def state(obj, field):
            value = getattr(obj, field, None)
            return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,32}", value) else None
        return {"elapsed_ms": round((time.monotonic() - self.started) * 1000),
                "peer": state(self.pc, "connectionState"), "ice": state(self.pc, "iceConnectionState"),
                "channel": state(self.channel, "readyState")}

    def _record_state(self, source: str, **fields):
        with self.lock:
            states = self.diagnostics["states"]
            if len(states) < 64:
                states.append({"source": source, **self._state_snapshot(), **fields})

    def _stage(self, stage: str):
        with self.lock:
            self.diagnostics["stage"] = stage
        self._record_state(stage)

    async def _pause(self, seconds: float):
        try:
            await asyncio.wait_for(self.stop_async.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def _emit(self, value):
        if isinstance(value, dict) and len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 2 * 1024 * 1024:
            self._error("invalid_event")
            self.stop.set()
            return
        try:
            self.events.put_nowait(value)
        except queue.Full:
            self._error("consumer_stalled", "event_queue")
            self.stop.set()

    def _error(self, code: str, source: str = "event", exc: Exception | None = None):
        # 只保留首次故障，避免清理时的异常覆盖原始原因；绝不记录异常原文和会话凭据。
        with self.lock:
            if self.fatal is not None:
                return
            self.fatal = VoiceImageError(code, self.state.conversation_id)
            self.diagnostics["failure"] = {"code": code, "source": source,
                "stage": self.diagnostics["stage"], **self._state_snapshot()}
            if exc is not None:
                self.diagnostics["failure"]["exception_type"] = type(exc).__name__[:80]

    def _received(self, raw):
        with self.lock:
            self._receive(raw)

    def _receive(self, raw):
        event = unpack_event(raw)
        kind = event.get("type")
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z_]{1,80}", kind):
            self._error("invalid_event")
            return
        counts = self.diagnostics["events"]
        counts[kind] = counts.get(kind, 0) + 1
        self.diagnostics["last_event_ms"] = round((time.monotonic() - self.started) * 1000)
        # 网页接收器明确允许 spawn_update 不包在 payload 中。
        payload = event if kind in {"spawn_update", "wingman_session_debug"} and "payload" not in event else event.get("payload", {})
        if not isinstance(payload, dict):
            self._error("invalid_event")
            return
        try:
            if kind == "state_update" and payload.get("new_state") == "listening":
                self.ready.set()
            if kind in {"goodbye", "error"} and not self.completed:
                self._error("upstream_error", kind)
                return
            cid = payload.get("conversation_id")
            if isinstance(cid, str) and re.fullmatch(r"[a-f0-9-]{36}", cid):
                with self.lock:
                    self.state.conversation_id = cid
                    self.backend.image_conversation_id = cid
                if not self.completed:
                    self._emit({"conversation_id": cid, "type": "voice_conversation_bound"})
            if not self.sent or self.completed:
                return
            if kind == "spawn_update":
                self.state.spawn(payload)
                self.diagnostics["active_spawns"] = len(self.state.active_spawns)
                if payload.get("state") in {"end", "cancel"}:
                    self.diagnostics["spawn_terminal_events"] = self.diagnostics.get("spawn_terminal_events", 0) + 1
            message = payload.get("message") if kind == "full_chat_message" else None
            if kind == "chat_message_delta":
                decoded = self.decoder.apply(payload.get("delta"))
                message = decoded.get("message") if isinstance(decoded, dict) else None
            if isinstance(message, dict):
                with self.lock:
                    self.state.message(message)
                self._emit({"conversation_id": self.state.conversation_id, "message": message,
                            "_voice_transport": True})
        except (ValueError, TypeError, KeyError, IndexError, VoiceImageError):
            self._error("invalid_event")

    def _handshake(self, offer: str):
        mime = CurlMime()
        try:
            payload = session_payload("wm", self.model, str(uuid.uuid4()))
            mime.addpart(name="sdp", data=offer)
            mime.addpart(name="session", data=json.dumps(payload))
            headers = self.backend._headers("/realtime/wm", {
                "OAI-Device-Id": self.backend.device_id,
                "OAI-Client-Version": self.backend.client_version,
                "x-openai-web-frontend": "codex_webview",
            })
            headers.pop("Content-Type", None)
            response = self.backend.session.post(self.backend.base_url + "/realtime/wm?dcid=0",
                headers=headers, multipart=mime, timeout=max(1.0, min(30.0, self.deadline - time.monotonic())))
            self._diagnostic(handshake_status=response.status_code)
            try:
                if response.status_code not in {200, 201}:
                    raise VoiceImageError("handshake_rejected")
                answer = response.text
                if len(answer) > 262144 or not answer.startswith("v=0"):
                    raise VoiceImageError("invalid_event")
                return answer
            finally:
                response.close()
        finally:
            mime.close()

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.runner = asyncio.current_task()
        if self.stop.is_set():
            return
        self.ready = asyncio.Event()
        self.stop_async = asyncio.Event()
        self.pc = RTCPeerConnection(RTCConfiguration(bundlePolicy=RTCBundlePolicy.MAX_BUNDLE))
        self.audio = AudioStreamTrack()
        self.pc.addTransceiver(self.audio, direction="sendrecv")
        self.pc.addTransceiver("video", direction="sendonly")
        self.channel = self.pc.createDataChannel("", negotiated=True, id=0)

        @self.channel.on("open")
        def opened():
            self._record_state("data_channel_open")
            if self.stop.is_set():
                return
            self.channel.send(pack_event("track_state", {"type": "track_state", "track_id": "microphone",
                "media_type": "audio", "media_source": "microphone", "state": "muted"}))
            self._diagnostic(muted=True)

        self.channel.on("message", self._received)

        @self.channel.on("close")
        def channel_closed():
            self._record_state("data_channel_closed")
            if not self.stop.is_set() and not self.completed:
                self._error("connection_failed", "data_channel_closed")

        @self.pc.on("connectionstatechange")
        async def connection_changed():
            self._record_state("peer_state")
            if self.pc.connectionState in {"failed", "closed"} and not self.stop.is_set():
                if self.completed:
                    self._diagnostic(delivery_disconnected=True)
                else:
                    self._error("connection_failed", "peer_state")

        @self.pc.on("iceconnectionstatechange")
        def ice_changed():
            self._record_state("ice_state")

        # 公开的状态事件足够定位传输层；不打开含地址、SDP 和凭据的全局 DEBUG 日志。
        transports = {t.sender.transport for t in self.pc.getTransceivers()}
        if self.pc.sctp is not None:
            transports.add(self.pc.sctp.transport)
        for transport in transports:
            def dtls_changed(transport=transport):
                self._record_state("dtls_state", dtls=transport.state)
            transport.on("statechange", dtls_changed)

        @self.pc.on("track")
        def track_received(track):
            async def discard():
                try:
                    while True:
                        await track.recv()
                except Exception:
                    return
            task = asyncio.create_task(discard())
            self.media_tasks.add(task)
            task.add_done_callback(self.media_tasks.discard)

        try:
            async with asyncio.timeout(max(0.01, self.deadline - time.monotonic())):
                self._stage("offer")
                await self.pc.setLocalDescription(await self.pc.createOffer())
                if self.stop.is_set():
                    return
                self._stage("handshake")
                answer = await asyncio.to_thread(self._handshake, self.pc.localDescription.sdp)
                if self.stop.is_set():
                    return
                answer, _, repaired = normalize_bundle_candidates(answer)
                self._diagnostic(bundle_sections_repaired=repaired)
                await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
                self._stage("waiting_ready")
                while not self.ready.is_set():
                    if self.fatal or self.stop.is_set():
                        raise self.fatal or VoiceImageError("connection_failed")
                    await self._pause(0.05)
                if self.pc.connectionState != "connected" or self.channel.readyState != "open":
                    raise VoiceImageError("connection_failed")
                self.backend._report_progress("generating")
                with self.lock:
                    if self.fatal or self.stop.is_set() or time.monotonic() >= self.deadline:
                        raise self.fatal or VoiceImageError("connection_failed")
                    # 传输报错时也不能重发；远端可能已经接收。与 close 共用锁，禁止停止后提交。
                    self.sent = True
                    self._stage("submitting")
                    self.diagnostics["text_submissions"] = 1
                    self.channel.send(pack_event("relay_message", {"type": "relay_message", "message": self.message}))
                self._stage("generating")
                while not self.stop.is_set():
                    if self.fatal:
                        raise self.fatal
                    if self.state.cancelled:
                        raise VoiceImageError("upstream_error", self.state.conversation_id)
                    if self.state.complete(time.monotonic()):
                        self.completed = True
                        self._diagnostic(turn_complete=True)
                        self._stage("delivery")
                        self._emit({"type": "voice_turn_complete", "conversation_id": self.state.conversation_id})
                        self._emit("[DONE]")
                        break
                    await self._pause(0.1)
            # 下载和最终图处理仍使用同一账号；保持语音到 backend.close，而不是首次图指针出现就断开。
            delivery_deadline = time.monotonic() + 180
            while self.completed and not self.stop.is_set() and time.monotonic() < delivery_deadline:
                await self._pause(0.1)
        except (TimeoutError, asyncio.TimeoutError):
            self._error("deadline_exceeded", "session_deadline")
            self._emit(self.fatal)
        except Exception as exc:
            self._error(exc.code if isinstance(exc, VoiceImageError) else "connection_failed", "session_loop", exc)
            self._emit(self.fatal)
        finally:
            self.stop.set()
            self._stage("closing")
            # pc.close 已负责关闭 SCTP；单独 channel.close 会再发重配置包，在断链时制造二次异常。
            self.audio.stop()
            await self.pc.close()
            for task in list(self.media_tasks):
                task.cancel()
            await asyncio.gather(*self.media_tasks, return_exceptions=True)
            self._diagnostic(connection_closed=True)
            self._stage("closed")

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._error("connection_failed", "session_thread", exc)
            self._emit(self.fatal)
        finally:
            self.stop.set()
            # 上游异常的原文可能含请求凭据，只记录固定状态和计数。
            logger.info({"event": "voice_image_session_closed", **self.diagnostic_snapshot()})

    def stream(self):
        if self.stop.is_set():
            raise VoiceImageError("connection_failed")
        self.thread = threading.Thread(target=self._thread_main, name="image-voice-session", daemon=True)
        self.thread.start()
        while True:
            if self.fatal:
                raise self.fatal
            if time.monotonic() >= self.deadline:
                raise VoiceImageError("deadline_exceeded", self.state.conversation_id)
            try:
                event = self.events.get(timeout=max(0.05, min(1.0, self.deadline - time.monotonic())))
            except queue.Empty:
                if self.fatal:
                    raise self.fatal
                if time.monotonic() >= self.deadline or self.stop.is_set():
                    raise VoiceImageError("deadline_exceeded", self.state.conversation_id)
                continue
            if isinstance(event, Exception):
                raise event
            if event == "[DONE]":
                yield "[DONE]"
                return
            yield json.dumps(event, ensure_ascii=False, separators=(",", ":"))

    def snapshot(self, conversation_id: str):
        with self.lock:
            if not self.completed or conversation_id != self.state.conversation_id:
                return None
            return {"conversation_id": conversation_id, "messages": copy.deepcopy(list(self.state.messages.values())),
                    "voice_turn_complete": True}

    def close(self):
        with self.lock:
            self.stop.set()
        # 协作式唤醒等待，不取消 runner：它可能已在等待 aiortc 共享的关闭 Future。
        # 握手 HTTP 自身有 30 秒上限；返回后检查 stop，关闭期间不再提交文字。
        if self.loop and self.stop_async:
            try:
                self.loop.call_soon_threadsafe(self.stop_async.set)
            except RuntimeError:
                pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=35)
            if self.thread.is_alive():
                logger.error({"event": "voice_image_close_timeout", "session_id": self.diagnostics["session_id"]})


def stream_voice_image(backend, prompt: str, model: str, images: list[str], timeout_secs: float):
    from services.proxy_service import proxy_settings
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= 65536 or not isinstance(images, list) or len(images) > 10:
        raise VoiceImageError("invalid_event")
    if proxy_settings.get_profile(account=backend.account).proxy_url:
        raise VoiceImageError("proxy_not_supported")
    backend._report_progress("uploading")
    references = [backend._upload_image(image, f"image_{index}.png") for index, image in enumerate(images, 1)]
    message = backend._image_generation_payload(prompt, model, references)["messages"][0]
    backend._report_progress("preparing_conversation")
    session = VoiceImageSession(backend, message, backend._image_model_slug(model), timeout_secs)
    backend._voice_image_session = session
    yield from session.stream()
