from __future__ import annotations

import json
import asyncio
import concurrent.futures
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")

from services.openai_backend_api import OpenAIBackendAPI
from services.voice_delta import DeltaDecoder
from services.voice_image_transport import VoiceImageError, VoiceImageSession, VoiceTurnState, stream_voice_image
from services.voice_protocol import pack_event, unpack_event


CONVERSATION_A = "11111111-1111-4111-8111-111111111111"
CONVERSATION_B = "22222222-2222-4222-8222-222222222222"


def assistant_message(message_id="assistant-a", status="finished_successfully"):
    return {
        "id": message_id, "author": {"role": "assistant"}, "recipient": "all",
        "channel": "final", "status": status, "end_turn": None,
        "content": {"content_type": "multimodal_text", "parts": ["完成"]},
    }


def user_message():
    return {"id": "user-a", "author": {"role": "user"},
            "content": {"content_type": "text", "parts": ["画一只小猫"]}}


def simple_backend():
    return SimpleNamespace(image_conversation_id="", _report_progress=Mock())


class FakeChannel:
    def __init__(self, on_relay):
        self.handlers = {}
        self.readyState = "connecting"
        self.sent = []
        self.on_relay = on_relay

    def on(self, event, handler=None):
        def register(callback):
            self.handlers[event] = callback
            return callback
        return register(handler) if handler is not None else register

    def send(self, raw):
        event = unpack_event(raw)
        self.sent.append(event)
        if event.get("type") == "relay_message":
            self.on_relay(self, event)

    def close(self):
        self.readyState = "closed"


class FakePeerConnection:
    def __init__(self, on_relay, on_ready=None):
        self.channel = FakeChannel(on_relay)
        self.on_ready = on_ready
        self.connectionState = "new"
        self.handlers = {}
        self.transceivers = []
        self.closed = False
        self.sctp = None

    def on(self, event):
        def register(callback):
            self.handlers[event] = callback
            return callback
        return register

    def addTransceiver(self, track, **options):
        self.transceivers.append((track, options))

    def createDataChannel(self, label, **options):
        self.channel_options = (label, options)
        return self.channel

    def getTransceivers(self):
        return []

    async def createOffer(self):
        return SimpleNamespace(sdp="v=0\r\n", type="offer")

    async def setLocalDescription(self, description):
        self.localDescription = description

    async def setRemoteDescription(self, description):
        self.connectionState = "connected"
        self.channel.readyState = "open"
        self.channel.handlers["open"]()
        self.channel.handlers["message"](pack_event("state_update", {"new_state": "listening"}))
        if self.on_ready:
            self.on_ready()

    async def close(self):
        self.connectionState = "closed"
        self.channel.close()
        self.closed = True


class VoiceTurnStateTests(unittest.TestCase):
    def test_initial_or_listening_state_cannot_finish_a_turn(self):
        state = VoiceTurnState()
        self.assertFalse(state.complete(state.last_change + 20))
        state.message(assistant_message())
        self.assertFalse(state.complete(state.last_change + 20))

    def test_active_spawn_and_in_progress_message_both_prevent_completion(self):
        state = VoiceTurnState()
        state.spawn({"spawn_id": "spawn-a", "state": "start"})
        state.message(assistant_message(status="in_progress"))
        self.assertFalse(state.complete(state.last_change + 20))
        state.spawn({"spawn_id": "spawn-a", "state": "end"})
        self.assertFalse(state.complete(state.last_change + 20))
        state.message(assistant_message())
        self.assertFalse(state.complete(state.last_change + 0.5))
        self.assertTrue(state.complete(state.last_change + 2))

    def test_all_spawns_must_end_even_after_a_successful_message(self):
        state = VoiceTurnState()
        state.message(assistant_message())
        for spawn_id in ("spawn-a", "spawn-b"):
            state.spawn({"spawn_id": spawn_id, "state": "start"})
        state.spawn({"spawn_id": "spawn-a", "state": "end"})
        self.assertFalse(state.complete(state.last_change + 20))
        state.spawn({"spawn_id": "spawn-b", "state": "end"})
        self.assertTrue(state.complete(state.last_change + 2))

    def test_cancel_remains_failure_even_if_a_different_spawn_ends(self):
        state = VoiceTurnState()
        state.message(assistant_message())
        state.spawn({"spawn_id": "spawn-a", "state": "cancel"})
        state.spawn({"spawn_id": "spawn-b", "state": "end"})
        self.assertTrue(state.cancelled)
        self.assertFalse(state.complete(state.last_change + 20))

    def test_hidden_success_message_cannot_finish_a_turn(self):
        state = VoiceTurnState()
        message = assistant_message()
        message["metadata"] = {"is_visually_hidden_from_conversation": True}
        state.message(message)
        state.spawn({"spawn_id": "spawn-a", "state": "end"})
        self.assertFalse(state.complete(state.last_change + 20))

    def test_message_limit_counts_utf8_bytes_and_rejects_before_storing(self):
        state = VoiceTurnState()
        message = assistant_message()
        message["content"]["parts"] = ["猫" * 400000]
        with self.assertRaises(VoiceImageError) as raised:
            state.message(message)
        self.assertEqual("invalid_event", raised.exception.code)
        self.assertEqual({}, state.messages)

    def test_total_message_cache_is_bounded_and_replacements_do_not_double_count(self):
        state = VoiceTurnState()
        for index in range(15):
            message = assistant_message("message-" + str(index))
            message["content"]["parts"] = ["x" * (512 * 1024)]
            state.message(message)
        state.message(message)
        self.assertEqual(15, len(state.messages))
        message = dict(message, id="overflow")
        with self.assertRaises(VoiceImageError) as raised:
            state.message(message)
        self.assertEqual("invalid_event", raised.exception.code)
        self.assertNotIn("overflow", state.messages)
        self.assertEqual(15, len(state.messages))


class VoiceSessionTests(unittest.TestCase):
    def test_spawn_update_accepts_top_level_and_payload_envelopes(self):
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                session = VoiceImageSession(simple_backend(), user_message(), "auto", 5)
                session.sent = True
                for state in ("start", "end"):
                    payload = {"spawn_id": "spawn-a", "state": state}
                    event = {"type": "spawn_update", "payload": payload} if wrapped else {"type": "spawn_update", **payload}
                    session._received(json.dumps(event))
                    self.assertIsNone(session.fatal)
                    if state == "start":
                        self.assertEqual({"spawn-a"}, session.state.active_spawns)
                self.assertEqual(set(), session.state.active_spawns)
                self.assertTrue(session.state.saw_spawn_end)

    def test_sessions_isolate_message_state_and_snapshots(self):
        source = user_message()
        backend_a, backend_b = simple_backend(), simple_backend()
        first = VoiceImageSession(backend_a, source, "auto", 5)
        second = VoiceImageSession(backend_b, source, "auto", 5)
        first.message["content"]["parts"].append("仅属于账号 A")
        self.assertEqual(["画一只小猫"], second.message["content"]["parts"])
        self.assertEqual(["画一只小猫"], source["content"]["parts"])
        for session, cid, message_id in ((first, CONVERSATION_A, "message-a"), (second, CONVERSATION_B, "message-b")):
            session.sent = True
            session._received(pack_event("full_chat_message", {"conversation_id": cid, "message": assistant_message(message_id)}))
            session.completed = True
        self.assertEqual(CONVERSATION_A, backend_a.image_conversation_id)
        self.assertEqual(CONVERSATION_B, backend_b.image_conversation_id)
        self.assertIsNone(first.snapshot(CONVERSATION_B))
        self.assertIsNone(second.snapshot(CONVERSATION_A))
        snapshot = first.snapshot(CONVERSATION_A)
        snapshot["messages"][0]["content"]["parts"].append("外部修改")
        self.assertEqual(["完成"], first.snapshot(CONVERSATION_A)["messages"][0]["content"]["parts"])
        self.assertEqual({"message-b"}, set(second.state.messages))

    def test_handshake_uses_its_own_account_session_and_closes_responses(self):
        sessions = []
        for account in ("a", "b"):
            response = Mock(status_code=201, text="v=0\r\n")
            backend = SimpleNamespace(session=Mock(), base_url="https://upstream.invalid",
                                      device_id="device-" + account, client_version="test-version",
                                      _headers=Mock(return_value={"Authorization": "Bearer test-account-" + account,
                                                                  "Content-Type": "application/json"}))
            backend.session.post.return_value = response
            session = VoiceImageSession(backend, user_message(), "auto", 5)
            sessions.append(session)
            with patch("services.voice_image_transport.CurlMime") as mime:
                self.assertEqual("v=0\r\n", session._handshake("v=0\r\n"))
                mime.return_value.close.assert_called_once()
            backend.session.post.assert_called_once()
            args, options = backend.session.post.call_args
            self.assertEqual("https://upstream.invalid/realtime/wm?dcid=0", args[0])
            self.assertEqual("Bearer test-account-" + account, options["headers"]["Authorization"])
            self.assertNotIn("Content-Type", options["headers"])
            response.close.assert_called_once()
        self.assertIsNot(sessions[0].backend.session, sessions[1].backend.session)

    def run_mock_stream(self, on_relay, on_ready=None):
        session = VoiceImageSession(simple_backend(), user_message(), "auto", 5)
        pc = FakePeerConnection(lambda channel, event: on_relay(session, channel, event),
                                (lambda: on_ready(session)) if on_ready else None)
        audio = Mock()
        with patch("services.voice_image_transport.RTCPeerConnection", return_value=pc), \
                patch("services.voice_image_transport.AudioStreamTrack", return_value=audio), \
                patch.object(session, "_handshake", return_value="v=0\r\n"), \
                patch("services.voice_image_transport.normalize_bundle_candidates", return_value=("v=0\r\n", [], 0)):
            try:
                events = list(session.stream())
            finally:
                session.close()
                self.assertTrue(pc.closed)
                self.assertEqual("closed", pc.channel.readyState)
                audio.stop.assert_called_once()
        return session, pc, events

    def test_stream_submits_once_and_only_emits_done_after_spawn_end(self):
        def finish(session, channel, event):
            self.assertEqual(user_message(), event["payload"]["message"])
            channel.handlers["message"](pack_event("spawn_update", {"spawn_id": "spawn-a", "state": "start"}))
            channel.handlers["message"](pack_event("full_chat_message", {"conversation_id": CONVERSATION_A,
                                                                            "message": assistant_message()}))
            self.assertFalse(session.state.complete(session.state.last_change + 20))
            channel.handlers["message"](pack_event("spawn_update", {"spawn_id": "spawn-a", "state": "end"}))
            # 跳过完成稳定窗口，避免测试依赖真实等待时间。
            session.state.last_change = time.monotonic() - 2

        session, pc, events = self.run_mock_stream(finish)
        sent_types = [event["type"] for event in pc.channel.sent]
        self.assertEqual(["track_state", "relay_message"], sent_types)
        self.assertEqual("muted", pc.channel.sent[0]["payload"]["state"])
        self.assertEqual(1, session.diagnostics["text_submissions"])
        self.assertEqual(1, events.count("[DONE]"))
        self.assertEqual("[DONE]", events[-1])
        self.assertEqual("voice_turn_complete", json.loads(events[-2])["type"])

    def test_ambiguous_send_failure_is_not_resubmitted_and_releases_peer(self):
        calls = []

        def fail_after_send(session, channel, event):
            calls.append(event)
            raise OSError("synthetic transport failure after remote acceptance")

        with self.assertRaises(VoiceImageError) as raised:
            self.run_mock_stream(fail_after_send)
        self.assertEqual("connection_failed", raised.exception.code)
        self.assertEqual(1, len(calls))

    def test_cancelled_spawn_raises_instead_of_reporting_done(self):
        def cancel(session, channel, event):
            channel.handlers["message"](pack_event("spawn_update", {"spawn_id": "spawn-a", "state": "cancel"}))

        with self.assertRaises(VoiceImageError) as raised:
            self.run_mock_stream(cancel)
        self.assertEqual("upstream_error", raised.exception.code)

    def test_listening_with_stop_error_or_expired_deadline_never_submits(self):
        for mode in ("stop", "error", "deadline"):
            with self.subTest(mode=mode):
                relay = Mock()
                channels = []

                def interrupt_on_ready(session):
                    channels.append(session.channel)
                    if mode == "stop":
                        session.stop.set()
                    elif mode == "error":
                        session._received(pack_event("error", {"message": "synthetic upstream rejection"}))
                    else:
                        session.deadline = time.monotonic() - 1

                with self.assertRaises(VoiceImageError):
                    self.run_mock_stream(relay, interrupt_on_ready)
                relay.assert_not_called()
                self.assertEqual(["track_state"], [event["type"] for event in channels[0].sent])

    def test_oversized_outgoing_message_is_rejected_before_peer_creation(self):
        message = user_message()
        message["content"]["parts"] = ["猫" * 400000]
        with patch("services.voice_image_transport.RTCPeerConnection") as peer:
            with self.assertRaises(VoiceImageError) as raised:
                VoiceImageSession(simple_backend(), message, "auto", 5)
            peer.assert_not_called()
        self.assertEqual("invalid_event", raised.exception.code)

    def test_fatal_or_deadline_is_not_hidden_by_queued_events(self):
        for mode, expected_code in (("fatal", "upstream_error"), ("deadline", "deadline_exceeded")):
            with self.subTest(mode=mode):
                session = VoiceImageSession(simple_backend(), user_message(), "auto", 5)
                for index in range(5):
                    session._emit({"message": assistant_message(str(index))})
                if mode == "fatal":
                    session._error(expected_code)
                else:
                    session.deadline = time.monotonic() - 1
                # 不启动线程；只验证消费者不能先吐出积压结果再报告失败。
                with patch("services.voice_image_transport.threading.Thread"):
                    stream = session.stream()
                    with self.assertRaises(VoiceImageError) as raised:
                        next(stream)
                    self.assertEqual(expected_code, raised.exception.code)
                    self.assertEqual(5, session.events.qsize())

    def test_slow_consumer_is_stopped_when_queue_limit_is_reached(self):
        session = VoiceImageSession(simple_backend(), user_message(), "auto", 5)
        for index in range(16):
            session._emit({"sequence": index})
        self.assertIsNone(session.fatal)
        session._emit({"sequence": 16})
        self.assertEqual("consumer_stalled", session.fatal.code)
        self.assertTrue(session.stop.is_set())
        self.assertEqual(16, session.events.qsize())

    def test_first_failure_is_preserved_without_exception_text_or_prompt(self):
        session = VoiceImageSession(simple_backend(), user_message(), "auto", 5)
        session._stage("generating")
        session._error("connection_failed", "peer_state", OSError("Bearer secret-account-token"))
        session._stage("closing")
        session._error("invalid_event", "session_thread", ValueError("private prompt"))
        snapshot = session.diagnostic_snapshot()
        self.assertEqual("connection_failed", session.fatal.code)
        self.assertEqual("peer_state", snapshot["failure"]["source"])
        self.assertEqual("generating", snapshot["failure"]["stage"])
        self.assertEqual("OSError", snapshot["failure"]["exception_type"])
        serialized = json.dumps(snapshot, ensure_ascii=False)
        for secret in ("secret-account-token", "private prompt", "画一只小猫"):
            self.assertNotIn(secret, serialized)
        snapshot["failure"]["code"] = "changed"
        self.assertEqual("connection_failed", session.diagnostic_snapshot()["failure"]["code"])

    def test_diagnostic_state_history_is_bounded(self):
        session = VoiceImageSession(simple_backend(), user_message(), "auto", 5)
        for _ in range(100):
            session._record_state("peer_state")
        self.assertEqual(64, len(session.diagnostic_snapshot()["states"]))

    def test_concurrent_close_cannot_cancel_a_peer_cleanup_already_in_progress(self):
        cleanup_started = threading.Event()
        cancelled = []
        close_calls = []

        class SlowClosePeer(FakePeerConnection):
            async def close(self):
                close_calls.append(1)
                cleanup_started.set()
                try:
                    await asyncio.sleep(0.15)
                    await super().close()
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise

        def fail_after_send(channel, event):
            raise OSError("synthetic accepted request; do not replay")

        session = VoiceImageSession(simple_backend(), user_message(), "auto", 5)
        pc = SlowClosePeer(fail_after_send)
        audio = Mock()
        with patch("services.voice_image_transport.RTCPeerConnection", return_value=pc), \
                patch("services.voice_image_transport.AudioStreamTrack", return_value=audio), \
                patch.object(session, "_handshake", return_value="v=0\r\n"), \
                patch("services.voice_image_transport.normalize_bundle_candidates", return_value=("v=0\r\n", [], 0)):
            with self.assertRaises(VoiceImageError):
                list(session.stream())
            self.assertTrue(cleanup_started.wait(1))
            callers = [threading.Thread(target=session.close) for _ in range(8)]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(timeout=2)
                self.assertFalse(caller.is_alive())
        self.assertEqual([], cancelled)
        self.assertEqual([1], close_calls)
        self.assertTrue(pc.closed)
        self.assertFalse(session.thread.is_alive())
        self.assertEqual(1, session.diagnostics["text_submissions"])
        self.assertTrue(session.diagnostics["connection_closed"])
        self.assertEqual("OSError", session.diagnostics["failure"]["exception_type"])

    def test_stop_during_handshake_never_submits_when_response_arrives(self):
        started, release = threading.Event(), threading.Event()
        session = VoiceImageSession(simple_backend(), user_message(), "auto", 5)
        relay = Mock()
        pc = FakePeerConnection(relay)

        def handshake(offer):
            started.set()
            release.wait(timeout=2)
            return "v=0\r\n"

        def consume():
            try:
                list(session.stream())
            except VoiceImageError:
                pass

        with patch("services.voice_image_transport.RTCPeerConnection", return_value=pc), \
                patch("services.voice_image_transport.AudioStreamTrack", return_value=Mock()), \
                patch.object(session, "_handshake", side_effect=handshake):
            consumer = threading.Thread(target=consume)
            consumer.start()
            self.assertTrue(started.wait(1))
            closer = threading.Thread(target=session.close)
            closer.start()
            self.assertTrue(session.stop.wait(1))
            release.set()
            closer.join(timeout=2)
            consumer.join(timeout=2)
        relay.assert_not_called()
        self.assertTrue(pc.closed)
        self.assertFalse(closer.is_alive())
        self.assertFalse(consumer.is_alive())
        self.assertEqual(0, session.diagnostics["text_submissions"])

    def test_forty_parallel_failed_sessions_all_release_without_replaying(self):
        peers = []

        class SlowClosePeer(FakePeerConnection):
            async def close(self):
                await asyncio.sleep(0.03)
                await super().close()

        def accepted_then_disconnected(channel, event):
            raise OSError("synthetic disconnect after acceptance")

        def create_peer(*args, **kwargs):
            peer = SlowClosePeer(accepted_then_disconnected)
            peers.append(peer)
            return peer

        def run_one(index):
            session = VoiceImageSession(simple_backend(), user_message(), "auto", 5)
            try:
                with self.assertRaises(VoiceImageError):
                    list(session.stream())
            finally:
                session.close()
                session.close()
            self.assertFalse(session.thread.is_alive())
            return session.diagnostic_snapshot()

        with patch("services.voice_image_transport.RTCPeerConnection", side_effect=create_peer), \
                patch("services.voice_image_transport.AudioStreamTrack", side_effect=Mock), \
                patch.object(VoiceImageSession, "_handshake", return_value="v=0\r\n"), \
                patch("services.voice_image_transport.normalize_bundle_candidates", return_value=("v=0\r\n", [], 0)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=40) as pool:
                summaries = list(pool.map(run_one, range(40)))
        self.assertEqual(40, len(peers))
        self.assertTrue(all(peer.closed for peer in peers))
        self.assertTrue(all(s.get("connection_closed") and s["text_submissions"] == 1 for s in summaries))
        self.assertEqual(40, len({s["session_id"] for s in summaries}))


class VoiceBackendIntegrationTests(unittest.TestCase):
    def backend(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "test-account-token"
        backend.account = {"id": "test-account"}
        backend.progress_callback = None
        backend.session = Mock()
        backend._image_client_context = Mock(return_value={})
        backend._timezone_fields = Mock(return_value={"timezone": "America/Los_Angeles", "timezone_offset_min": 420})
        backend._bootstrap = Mock()
        backend._get_chat_requirements = Mock()
        backend._prepare_image_conversation = Mock()
        backend._start_image_generation = Mock(side_effect=AssertionError("旧普通 POST 不应执行"))
        backend._post_image_conversation = Mock(side_effect=AssertionError("不应补发生成请求"))
        return backend

    def test_reference_upload_preserves_attachment_metadata_in_relay_message(self):
        backend = self.backend()
        reference = {"file_id": "file-test", "library_file_id": "libfile_test", "mime_type": "image/png",
                     "file_name": "reference.png", "width": 64, "height": 32, "file_size": 123}
        backend._upload_image = Mock(return_value=reference)
        with patch("services.proxy_service.proxy_settings.get_profile", return_value=SimpleNamespace(proxy_url="")), \
                patch("services.voice_image_transport.VoiceImageSession") as session_class:
            session_class.return_value.stream.return_value = iter(["[DONE]"])
            self.assertEqual(["[DONE]"], list(stream_voice_image(backend, "让小猫戴帽子", "gpt-image-2", ["synthetic-image"], 30)))
            sent_backend, message, model, timeout = session_class.call_args.args
            self.assertIs(backend, sent_backend)
            self.assertEqual(30, timeout)
            self.assertEqual("multimodal_text", message["content"]["content_type"])
            self.assertEqual("让小猫戴帽子", message["content"]["parts"][-1])
            self.assertEqual("sediment://file-test", message["content"]["parts"][0]["asset_pointer"])
            self.assertEqual([{"id": "file-test", "library_file_id": "libfile_test", "mime_type": "image/png",
                               "name": "reference.png", "size": 123, "width": 64, "height": 32}],
                             message["metadata"]["attachments"])
            backend._upload_image.assert_called_once_with("synthetic-image", "image_1.png")
            self.assertIs(session_class.return_value, backend._voice_image_session)
        backend.close()

    def test_voice_error_never_falls_back_to_ordinary_generation_post(self):
        backend = self.backend()
        try:
            with patch.dict(os.environ, {"CHATGPT2API_IMAGE_TRANSPORT": "voice"}), \
                    patch("services.voice_image_transport.stream_voice_image", side_effect=VoiceImageError("connection_failed")) as stream:
                with self.assertRaises(VoiceImageError):
                    list(backend._stream_picture_conversation("画猫", "gpt-image-2", []))
                stream.assert_called_once()
            backend._bootstrap.assert_not_called()
            backend._get_chat_requirements.assert_not_called()
            backend._prepare_image_conversation.assert_not_called()
            backend._start_image_generation.assert_not_called()
            backend._post_image_conversation.assert_not_called()
            backend.session.post.assert_not_called()
        finally:
            backend.close()

    def test_backend_close_releases_voice_before_http_session_and_is_idempotent(self):
        backend = self.backend()
        backend._voice_image_session = Mock()
        lifecycle = Mock()
        lifecycle.attach_mock(backend._voice_image_session.close, "voice")
        lifecycle.attach_mock(backend.session.close, "http")
        backend.close()
        backend.close()
        self.assertEqual([call.voice(), call.http()], lifecycle.mock_calls)

    def test_conversation_snapshot_avoids_http_and_is_account_scoped(self):
        backend = self.backend()
        voice = VoiceImageSession(backend, user_message(), "auto", 5)
        backend._voice_image_session = voice
        voice.state.conversation_id = CONVERSATION_A
        voice.state.message(assistant_message())
        voice.completed = True
        try:
            document = backend._get_conversation(CONVERSATION_A)
            self.assertTrue(document["voice_turn_complete"])
            self.assertEqual(CONVERSATION_A, document["conversation_id"])
            backend.session.get.assert_not_called()
            self.assertIsNone(voice.snapshot(CONVERSATION_B))
        finally:
            backend.close()

    def test_account_slot_is_released_only_after_backend_close_on_success_or_failure(self):
        from services.protocol import conversation

        for failed in (False, True):
            with self.subTest(failed=failed):
                backend = self.backend()
                backend._voice_image_session = Mock(sent=True)
                backend._voice_image_session.diagnostic_snapshot.return_value = {
                    "session_id": "test-diagnostic-id", "failure": {"source": "peer_state"}}
                backend.image_conversation_id = ""
                accounts = Mock()
                accounts.get_available_access_token.return_value = "synthetic-account-token"
                accounts.get_account.return_value = {"email": "synthetic@example.test"}
                backend._voice_image_session.close.side_effect = accounts.mark_image_result.assert_not_called
                lifecycle = Mock()
                lifecycle.attach_mock(backend._voice_image_session.close, "voice_close")
                lifecycle.attach_mock(backend.session.close, "http_close")
                lifecycle.attach_mock(accounts.mark_image_result, "release")
                output = conversation.ImageOutput(kind="result", model="gpt-image-2", index=1, total=1,
                                                   data=[{"b64_json": "synthetic"}])
                request = conversation.ConversationRequest(model="gpt-image-2", prompt="画猫")
                with patch.object(conversation, "account_service", accounts), \
                        patch.object(conversation, "OpenAIBackendAPI", return_value=backend), \
                        patch.object(conversation, "stream_image_outputs") as generate:
                    if failed:
                        generate.side_effect = VoiceImageError("connection_failed")
                        with self.assertRaises(conversation.ImageGenerationError) as raised:
                            conversation._generate_single_image_impl(request, 1, 1)
                        self.assertEqual("test-diagnostic-id",
                                         raised.exception.image_execution[-1]["voice_transport"]["session_id"])
                    else:
                        generate.return_value = iter([output])
                        self.assertEqual([output], conversation._generate_single_image_impl(request, 1, 1))
                self.assertEqual([call.voice_close(), call.http_close(),
                                  call.release("synthetic-account-token", not failed)], lifecycle.mock_calls)


class VoiceDeltaTests(unittest.TestCase):
    def test_delta_channels_remain_isolated_and_message_is_reconstructed(self):
        decoder = DeltaDecoder()
        original = {"message": assistant_message("a", "in_progress")}
        decoder.apply({"c": 0, "p": "", "o": "add", "v": original})
        decoder.apply({"c": 1, "p": "", "o": "add", "v": {"message": assistant_message("b")}})
        result = decoder.apply({"c": 0, "p": "/message/content/parts/0", "o": "append", "v": "一只小猫"})
        self.assertEqual(["完成一只小猫"], result["message"]["content"]["parts"])
        self.assertEqual("b", decoder.values[1]["message"]["id"])
        self.assertEqual(["完成"], original["message"]["content"]["parts"])

    def test_malformed_delta_paths_and_channels_are_rejected(self):
        for delta in ({"c": -1}, {"c": True}, {"c": 32}, {"p": "/__proto__/value", "v": "bad"},
                      {"p": "/message/parts/999999", "v": "bad"}, {"p": "x" * 2049}):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                DeltaDecoder().apply(delta)

    def test_total_delta_cache_is_bounded_without_double_counting_replacements(self):
        decoder = DeltaDecoder()
        payload = "x" * (1024 * 1024 + 1024)
        for channel in range(15):
            decoder.apply({"c": channel, "p": "", "o": "add", "v": payload})
        decoder.apply({"c": 14, "p": "", "o": "replace", "v": payload})
        self.assertEqual(15, len(decoder.values))
        with self.assertRaisesRegex(ValueError, "delta_state_too_large"):
            decoder.apply({"c": 15, "p": "", "o": "add", "v": payload})
        self.assertNotIn(15, decoder.values)


if __name__ == "__main__":
    unittest.main()
