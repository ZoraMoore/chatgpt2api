"""用本地静默 SSE 尾流验证终态收尾与原会话恢复，不访问外网上游。"""
from __future__ import annotations

import gc
import importlib
import json
import os
import select
import threading
import time
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")

from curl_cffi import CurlOpt, requests
from services.protocol.final_image import FinalImageTracker


CONVERSATION_ID = "local-conversation"
SUBMITTED_ID = "submitted"
FINAL_PATH = "/mnt/data/final.png"


def user_message(message_id=SUBMITTED_ID):
    return {"id": message_id, "author": {"role": "user"},
            "content": {"content_type": "text", "parts": ["synthetic image request"]}}


def image_message():
    return {"id": "generated-image", "author": {"role": "tool", "name": "image_gen"},
            "status": "finished_successfully", "metadata": {"image_gen_title": "synthetic"},
            "content": {"content_type": "multimodal_text", "parts": [
                {"content_type": "image_asset_pointer", "asset_pointer": "sediment://synthetic-image"}]}}


def final_message(*, finished=True, path=FINAL_PATH, message_id="final"):
    return {"id": message_id, "author": {"role": "assistant"}, "recipient": "all", "channel": "final",
            "status": "finished_successfully" if finished else "in_progress", "end_turn": finished,
            "content": {"content_type": "text", "parts": [f"![image](sandbox:{path})"]}}


def event(message):
    return {"conversation_id": CONVERSATION_ID, "message": message}


def completed_document():
    return {"conversation_id": CONVERSATION_ID,
            "messages": [user_message(), image_message(), final_message()]}


def final_events():
    return [event(image_message()), event(final_message())]


class LocalStreamServer(ThreadingHTTPServer):
    # 只有一条生成流；读取同一会话时最多增加一条短 GET，线程在关闭时全部回收。
    daemon_threads = False
    block_on_close = True

    def __init__(self, events, *, document=None, later_events=None, delay=0.0, statuses=None):
        super().__init__(("127.0.0.1", 0), LocalStreamHandler)
        self.events = events
        self.document = document if document is not None else completed_document()
        self.later_events = later_events or []
        self.delay = delay
        self.statuses = statuses or {}
        self.post_count = 0
        self.get_paths = []
        self.get_authorizations = []
        self.post_bodies = []
        self.peer_closed = threading.Event()
        self.body_sent = threading.Event()
        self.stopping = threading.Event()
        self.get_delay = 0.0
        self.worker = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01},
                                       name="local-image-stream-server")
        self.worker.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server_port}"

    def finish(self):
        self.stopping.set()
        self.shutdown()
        self.server_close()
        self.worker.join(timeout=1)


class LocalStreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def send_events(self, values):
        data = b"".join(("data: " + (value if isinstance(value, str) else json.dumps(value)) + "\n\n").encode()
                        for value in values)
        if data:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

    def do_POST(self):
        server = self.server
        server.post_count += 1
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        server.post_bodies.append(json.loads(body))
        if self.path != "/backend-api/f/conversation":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            self.send_events(server.events)
            server.body_sent.set()
            if server.later_events:
                if server.stopping.wait(server.delay):
                    return
                self.send_events(server.later_events)
            # 故意不写终止 chunk 或 [DONE]；必须由被测客户端主动释放连接。
            deadline = time.monotonic() + 2
            while not server.stopping.is_set() and time.monotonic() < deadline:
                readable, _, _ = select.select([self.connection], [], [], 0.02)
                if readable and not self.connection.recv(1):
                    server.peer_closed.set()
                    break
        except (BrokenPipeError, ConnectionResetError):
            server.peer_closed.set()
        finally:
            self.close_connection = True

    def do_GET(self):
        self.server.get_paths.append(self.path)
        self.server.get_authorizations.append(self.headers.get("Authorization"))
        self.server.stopping.wait(self.server.get_delay)
        status = self.server.statuses.get(self.path, 200)
        body = json.dumps(self.server.document if status == 200 else {"detail": "synthetic failure"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 超过恢复预算后客户端会主动中止慢 GET，这是该故障场景的预期行为。
            pass
        self.close_connection = True


class ImageGenerationStreamTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module("services.image_generation_stream")
        except ModuleNotFoundError as exc:
            if exc.name != "services.image_generation_stream":
                raise
            self.fail("缺少 services.image_generation_stream，尚未实现可中断生成流")
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for name, value in (("IMAGE_STREAM_IDLE_SECS", 0.03),
                            ("IMAGE_STREAM_RECOVERY_INTERVAL_SECS", 0.02),
                            ("IMAGE_STREAM_FINAL_RECOVERY_SECS", 0.08)):
            self.patches.enter_context(patch.object(self.module, name, value))

    def server(self, events, **kwargs):
        server = LocalStreamServer(events, **kwargs)
        self.addCleanup(server.finish)
        return server

    def backend(self, server):
        session = requests.Session(headers={"Authorization": "Bearer synthetic"}, retry=0,
                                   curl_options={CurlOpt.PROXY: ""})
        self.addCleanup(session.close)
        return SimpleNamespace(base_url=server.base_url, session=session, access_token="synthetic",
                               image_conversation_id="", _image_submitted_message_id=SUBMITTED_ID)

    def stream(self, backend, timeout=0.35):
        return self.module.stream_image_generation(
            backend, {"messages": [{"id": SUBMITTED_ID}]},
            {"Accept": "text/event-stream", "Authorization": "Bearer synthetic"}, timeout)

    @staticmethod
    def track(values):
        tracker = FinalImageTracker()
        for value in values:
            if value == "[DONE]":
                tracker.stream_complete = True
                continue
            try:
                payload = json.loads(value)
            except (TypeError, ValueError):
                continue
            tracker.update(payload)
        return tracker

    def assert_delivered(self, values, path=FINAL_PATH):
        tracker = self.track(values)
        self.assertTrue(tracker.generated, "必须见到本轮生成工具图片")
        self.assertTrue(tracker.is_complete(), "必须输出可供既有消费者确认的终态")
        self.assertEqual([path], tracker.paths())

    def assert_single_submission(self, server):
        self.assertEqual(1, server.post_count, "读取恢复不得重发生成 POST")
        self.assertEqual(SUBMITTED_ID, server.post_bodies[0]["messages"][0]["id"])
        self.assertTrue(server.peer_closed.wait(0.35), "生成连接未主动断开")

    def test_final_without_done_closes_immediately_without_history_get(self):
        server = self.server(final_events())
        backend = self.backend(server)
        started = time.monotonic()
        values = list(self.stream(backend, timeout=0.6))
        elapsed = time.monotonic() - started
        self.assert_delivered(values)
        self.assertLess(elapsed, 0.3, "最终图片已确认，不应继续等待静默尾流或总超时")
        self.assertEqual([], server.get_paths)
        self.assertEqual(CONVERSATION_ID, backend.image_conversation_id)
        self.assert_single_submission(server)

    def test_idle_tail_recovers_completed_same_conversation(self):
        server = self.server([{"conversation_id": CONVERSATION_ID}])
        backend = self.backend(server)
        started = time.monotonic()
        values = list(self.stream(backend, timeout=0.6))
        self.assert_delivered(values)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertIn(f"/backend-api/conversations/{CONVERSATION_ID}", server.get_paths)
        self.assertEqual(["Bearer synthetic"] * len(server.get_paths), server.get_authorizations)
        self.assert_single_submission(server)

    def test_transport_timeout_still_recovers_same_conversation_without_resubmit(self):
        server = self.server([{"conversation_id": CONVERSATION_ID}])
        backend = self.backend(server)
        with patch.object(self.module, "IMAGE_STREAM_IDLE_SECS", 10.0):
            started = time.monotonic()
            values = list(self.stream(backend, timeout=0.07))
        self.assertGreaterEqual(time.monotonic() - started, 0.045)
        self.assert_delivered(values)
        self.assertTrue(server.get_paths)
        self.assert_single_submission(server)

    def test_history_404_falls_back_to_singular_path(self):
        plural = f"/backend-api/conversations/{CONVERSATION_ID}"
        singular = f"/backend-api/conversation/{CONVERSATION_ID}"
        server = self.server([{"conversation_id": CONVERSATION_ID}], statuses={plural: 404})
        values = list(self.stream(self.backend(server)))
        self.assert_delivered(values)
        self.assertEqual([plural, singular], server.get_paths[:2])
        self.assert_single_submission(server)

    def test_history_non_404_never_falls_back_or_delivers(self):
        plural = f"/backend-api/conversations/{CONVERSATION_ID}"
        singular = f"/backend-api/conversation/{CONVERSATION_ID}"
        server = self.server([{"conversation_id": CONVERSATION_ID}], statuses={plural: 503})
        values = []
        try:
            values.extend(self.stream(self.backend(server), timeout=0.07))
        except Exception:
            pass
        self.assertIn(plural, server.get_paths)
        self.assertNotIn(singular, server.get_paths)
        self.assertNotIn("[DONE]", values, "读回失败不能发出成功终态")
        self.assertFalse(self.track(values).is_complete())
        self.assert_single_submission(server)

    def test_recovery_rejects_unfinished_wrong_conversation_and_other_turn(self):
        unfinished = completed_document()
        unfinished["messages"][-1] = final_message(finished=False)
        wrong_conversation = completed_document()
        wrong_conversation["conversation_id"] = "unrelated-conversation"
        missing_submission = completed_document()
        missing_submission["messages"][0] = user_message("unrelated-request")
        newer_turn = completed_document()
        newer_turn["messages"].insert(1, user_message("later-user-turn"))
        newer_turn_after_final = completed_document()
        newer_turn_after_final["messages"].append(user_message("later-user-turn"))
        no_image_artifact = completed_document()
        del no_image_artifact["messages"][1]
        no_image_artifact["messages"][-1]["content"]["parts"] = ["synthetic text without an image"]
        unsafe_path = completed_document()
        unsafe_path["messages"][-1] = final_message(path="/mnt/data/../private.png")
        for label, document in (("unfinished", unfinished), ("wrong conversation", wrong_conversation),
                                ("missing submitted message", missing_submission), ("new user turn", newer_turn),
                                ("new user turn after final", newer_turn_after_final),
                                ("no image artifact", no_image_artifact), ("unsafe sandbox path", unsafe_path)):
            with self.subTest(case=label):
                server = self.server([{"conversation_id": CONVERSATION_ID}], document=document)
                values = []
                try:
                    values.extend(self.stream(self.backend(server), timeout=0.07))
                except Exception:
                    pass
                self.assertNotIn("[DONE]", values, f"错误历史不能发出终态：{label}")
                tracker = self.track(values)
                try:
                    delivered = tracker.generated and tracker.is_complete() and bool(tracker.paths())
                except ValueError:
                    delivered = False
                self.assertFalse(delivered, f"错误恢复了 {label}")
                self.assertTrue(server.get_paths)
                self.assert_single_submission(server)

    def test_intermediate_image_is_not_treated_as_final(self):
        intermediate = final_message(finished=False, path="/mnt/data/intermediate.png", message_id="intermediate")
        server = self.server([event(image_message()), event(intermediate)],
                             later_events=[event(final_message())], delay=0.09)
        backend = self.backend(server)
        with patch.object(self.module, "IMAGE_STREAM_IDLE_SECS", 0.25):
            started = time.monotonic()
            values = list(self.stream(backend, timeout=0.6))
        self.assertGreaterEqual(time.monotonic() - started, 0.07)
        self.assert_delivered(values)
        self.assertEqual([], server.get_paths)
        self.assert_single_submission(server)

    def test_caller_close_releases_pending_connection(self):
        server = self.server([{"conversation_id": CONVERSATION_ID}])
        generator = self.stream(self.backend(server), timeout=0.6)
        next(generator)
        started = time.monotonic()
        generator.close()
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual([], server.get_paths)
        self.assert_single_submission(server)

    def test_cookie_merge_failure_does_not_prevent_connection_cleanup(self):
        server = self.server(final_events())
        backend = self.backend(server)
        with patch.object(backend.session.cookies, 'update', side_effect=RuntimeError('synthetic cookie failure')):
            values = list(self.stream(backend))
        self.assert_delivered(values)
        self.assert_single_submission(server)

    def test_oversized_history_is_not_parsed_or_recovered(self):
        server = self.server([{'conversation_id': CONVERSATION_ID}])
        values = []
        with patch.object(self.module, '_MAX_DOCUMENT_BYTES', 64):
            try:
                values.extend(self.stream(self.backend(server), timeout=.07))
            except Exception:
                pass
        self.assertNotIn('[DONE]', values)
        self.assert_single_submission(server)

    def test_live_tool_arriving_during_readback_prevents_old_result_recovery(self):
        new_call = {'id': 'later-edit-call', 'author': {'role': 'assistant'},
                    'recipient': 'python', 'status': 'finished_successfully',
                    'content': {'content_type': 'code', 'text': 'synthetic_edit()'}}
        server = self.server([{'conversation_id': CONVERSATION_ID}],
                             later_events=[event(new_call)], delay=.05)
        server.get_delay = .07
        values = []
        try:
            values.extend(self.stream(self.backend(server), timeout=.2))
        except Exception:
            pass
        self.assertNotIn('[DONE]', values)
        self.assertTrue(any('later-edit-call' in value for value in values))
        self.assert_single_submission(server)

    def test_done_is_delivered_only_after_upstream_connection_closes(self):
        server = self.server(final_events())
        generator = self.stream(self.backend(server), timeout=0.6)
        self.addCleanup(generator.close)
        values = []
        for value in generator:
            values.append(value)
            if value == "[DONE]":
                break
        # 消费者拿到 DONE 后通常直接结束，不能要求它再次 next 或等待垃圾回收。
        self.assertIn("[DONE]", values)
        self.assert_delivered(values)
        self.assertEqual([], server.get_paths)
        self.assert_single_submission(server)

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "文件描述符检查仅适用于 Linux")
    def test_repeated_completed_streams_release_threads_and_descriptors(self):
        server = self.server(final_events())
        gc.collect()
        baseline_fds = len(os.listdir("/proc/self/fd"))
        baseline_threads = {thread.ident for thread in threading.enumerate()}
        for _ in range(4):
            server.peer_closed.clear()
            backend = self.backend(server)
            values = list(self.stream(backend, timeout=0.6))
            self.assert_delivered(values)
            self.assertTrue(server.peer_closed.wait(0.35))
            backend.session.close()
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            gc.collect()
            if (len(os.listdir("/proc/self/fd")) <= baseline_fds
                    and {thread.ident for thread in threading.enumerate()} <= baseline_threads):
                break
            threading.Event().wait(0.01)
        self.assertEqual(4, server.post_count)
        self.assertEqual([], server.get_paths)
        self.assertLessEqual(len(os.listdir("/proc/self/fd")), baseline_fds)
        self.assertLessEqual({thread.ident for thread in threading.enumerate()}, baseline_threads)


if __name__ == "__main__":
    unittest.main()
