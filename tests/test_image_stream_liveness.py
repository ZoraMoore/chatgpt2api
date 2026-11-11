"""以容器内本地 SSE 复现活跃尾流、回读并发及候选身份约束；不访问上游。"""
from __future__ import annotations

import gc
import json
import os
import select
import threading
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "synthetic-admin")

from curl_cffi import CurlOpt, requests
from services import image_generation_stream as module
from tests import test_image_generation_stream as fixtures


class PulseHandler(fixtures.LocalStreamHandler):
    def do_POST(self):
        server = self.server
        server.post_count += 1
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        server.post_bodies.append(json.loads(body))
        if getattr(server, 'header_delay', 0):
            server.stopping.wait(server.header_delay)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        try:
            self.send_events(server.events)
            server.body_sent.set()
            began = time.monotonic()
            while not server.stopping.wait(server.pulse_interval):
                elapsed = time.monotonic() - began
                if getattr(server, 'terminal_at', None) and elapsed >= server.terminal_at:
                    self.send_events(fixtures.final_events())
                    server.terminal_at = None
                if not server.pulse_on_get or server.get_started.is_set():
                    server.get_started.clear()
                    self.send_events([server.pulse(elapsed) if callable(server.pulse) else server.pulse])
        except (BrokenPipeError, ConnectionResetError):
            server.peer_closed.set()
        finally:
            self.close_connection = True

    def do_GET(self):
        self.server.get_started.set()
        if self.server.track_get_close:
            self.server.get_paths.append(self.path)
            self.server.get_authorizations.append(self.headers.get('Authorization'))
            deadline = time.monotonic() + self.server.get_delay
            while time.monotonic() < deadline and not self.server.stopping.is_set():
                readable, _, _ = select.select([self.connection], [], [], .005)
                if readable and not self.connection.recv(1):
                    self.server.get_closed.set()
                    self.close_connection = True
                    return
            data = json.dumps(self.server.document).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.server.get_closed.set()
            self.close_connection = True
            return
        return super().do_GET()


def run_case(name, *, pulse, get_delay=0.012, timeout=.30, pulse_interval=.005, terminal_at=None,
             header_delay=0, pulse_on_get=False, known_cid=False, recovery_budget=.08,
             max_probe_interval=None, track_get_close=False, close_at=None, document=None):
    server = fixtures.LocalStreamServer([{'conversation_id': fixtures.CONVERSATION_ID}])
    server.RequestHandlerClass = PulseHandler
    server.pulse = pulse
    server.pulse_interval = pulse_interval
    server.get_delay = get_delay
    server.terminal_at = terminal_at
    server.header_delay = header_delay
    server.pulse_on_get = pulse_on_get
    server.get_started = threading.Event()
    server.get_closed = threading.Event()
    server.track_get_close = track_get_close
    if document is not None:
        server.document = document
    session = requests.Session(headers={'Authorization': 'Bearer synthetic'}, retry=0,
                               curl_options={CurlOpt.PROXY: ''})
    backend = SimpleNamespace(base_url=server.base_url, session=session, access_token='synthetic',
                              image_conversation_id=fixtures.CONVERSATION_ID if known_cid else '',
                              _image_submitted_message_id=fixtures.SUBMITTED_ID)
    values, readbacks, error = [], [], None
    original_read = module._read_completed_turn
    start = time.monotonic()
    async def traced_read(*args, **kwargs):
        at = time.monotonic() - start
        result = await original_read(*args, **kwargs)
        readbacks.append({'start_s': round(at, 4), 'complete': result is not None,
                          'reason': kwargs.get('diagnostic', {}).get('reason')})
        return result
    try:
        with ExitStack() as stack:
            if max_probe_interval is not None:
                stack.enter_context(patch.object(module, 'IMAGE_STREAM_MAX_PROBE_INTERVAL_SECS', max_probe_interval))
            for key, value in [('IMAGE_STREAM_IDLE_SECS', .03), ('IMAGE_STREAM_RECOVERY_INTERVAL_SECS', .02),
                               ('IMAGE_STREAM_FINAL_RECOVERY_SECS', recovery_budget), ('_read_completed_turn', traced_read)]:
                stack.enter_context(patch.object(module, key, value))
            try:
                stream = module.stream_image_generation(backend, {'messages': [{'id': fixtures.SUBMITTED_ID}]},
                                                        {'Accept': 'text/event-stream'}, timeout)
                try:
                    for value in stream:
                        values.append(value)
                        if close_at is not None and time.monotonic() - start >= close_at:
                            stream.close()
                            break
                finally:
                    stream.close()
            except Exception as exc:
                error = {'type': type(exc).__name__, 'message': str(exc).split('\n')[0][:200],
                         'code': getattr(exc, 'code', None)}
        tracker = fixtures.ImageGenerationStreamTests.track(values)
        elapsed = time.monotonic() - start
        get_closed = server.get_closed.wait(.12) if track_get_close else None
        result = {'case': name, 'elapsed_s': round(elapsed, 4), 'post_count': server.post_count,
                  'get_count': len(server.get_paths), 'readbacks': readbacks, 'done': '[DONE]' in values,
                  'generated': tracker.generated, 'paths': tracker.paths(), 'error': error,
                  'frame_count': len(values), 'get_peer_closed': get_closed,
                  'conversation_id': backend.image_conversation_id}
        return result
    finally:
        session.close()
        server.finish()



class StreamLivenessRegressionTests(unittest.TestCase):
    def assert_prompt_delivery(self, result, limit=.15):
        self.assertEqual(1, result['post_count'], '读回不得重发生成 POST')
        self.assertIsNone(result['error'], '完整最终快照不能因无关事件被丢弃至超时')
        self.assertTrue(result['done'])
        self.assertEqual(['/mnt/data/final.png'], result['paths'])
        self.assertLess(result['elapsed_s'], limit, '已经完成的会话必须及时交付')

    def test_noop_v_updates_do_not_postpone_recovery_until_deadline(self):
        result = run_case('noop_v_heartbeat', pulse={'v': {'heartbeat': 1}})
        self.assert_prompt_delivery(result)

    def test_ping_during_each_get_cannot_invalidate_completed_snapshot(self):
        result = run_case('nonprogress_event_race', pulse={'type': 'ping'}, pulse_on_get=True)
        self.assertTrue(any(row['complete'] for row in result['readbacks']))
        self.assert_prompt_delivery(result)

    def test_slow_history_get_does_not_block_live_terminal_frames(self):
        result = run_case('slow_get_hides_live_final', pulse={'type': 'ping'}, get_delay=.22,
                          timeout=.6, terminal_at=.06, recovery_budget=.25)
        self.assert_prompt_delivery(result)

    def test_real_progress_still_reads_history_by_fixed_interval(self):
        def progress(elapsed):
            return fixtures.event({'id': 'thinking', 'author': {'role': 'assistant'}, 'recipient': 'all',
                                   'status': 'in_progress', 'content': {'content_type': 'text',
                                                                      'parts': [str(elapsed)]}})
        result = run_case('real_progress_fixed_probe', pulse=progress, max_probe_interval=.09)
        self.assert_prompt_delivery(result)
        self.assertGreaterEqual(result['readbacks'][0]['start_s'], .08)
        self.assertLess(result['readbacks'][0]['start_s'], .13)

    def test_new_tool_during_get_rejects_preexisting_completed_image(self):
        new_call = {'id': 'new-edit-call', 'author': {'role': 'assistant'}, 'recipient': 'python',
                    'status': 'in_progress', 'content': {'content_type': 'code', 'text': 'synthetic_edit()'}}
        result = run_case('new_tool_rejects_old_image', pulse=fixtures.event(new_call), pulse_on_get=True)
        self.assertEqual(1, result['post_count'])
        self.assertEqual('Timeout', result['error']['type'])
        self.assertFalse(result['done'])
        self.assertEqual([], result['paths'])
        self.assertTrue(any(row['reason'] == 'calls_missing' for row in result['readbacks']))

    def test_live_terminal_cancels_slow_get_and_releases_descriptors(self):
        gc.collect()
        before = len(os.listdir('/proc/self/fd'))
        threads = {thread.ident for thread in threading.enumerate()}
        for index in range(4):
            result = run_case('slow_get_cancel_cleanup_' + str(index), pulse={'type': 'ping'}, get_delay=.22,
                              timeout=.6, terminal_at=.06, recovery_budget=.25, track_get_close=True)
            self.assert_prompt_delivery(result)
            self.assertTrue(result['get_peer_closed'], '终态交付后恢复GET连接必须关闭')
        gc.collect()
        self.assertLessEqual(len(os.listdir('/proc/self/fd')), before)
        self.assertLessEqual({thread.ident for thread in threading.enumerate()}, threads)

    def test_caller_close_cancels_pending_get(self):
        result = run_case('caller_close_pending_get', pulse={'type': 'ping'}, get_delay=.22,
                          timeout=.6, recovery_budget=.25, track_get_close=True, close_at=.06)
        self.assertEqual(1, result['post_count'])
        self.assertIsNone(result['error'])
        self.assertFalse(result['done'])
        self.assertLess(result['elapsed_s'], .15)
        self.assertTrue(result['get_peer_closed'])

    def test_post_headers_timeout_without_cid_retains_curl_error_code(self):
        result = run_case('headers_timeout_without_cid', pulse={'type': 'ping'}, header_delay=.2, timeout=.06)
        self.assertEqual(1, result['post_count'])
        self.assertEqual(0, result['get_count'])
        self.assertEqual('Timeout', result['error']['type'])
        self.assertEqual(28, result['error']['code'])

    def test_conversation_changed_during_get_never_relabels_old_snapshot(self):
        result = run_case('conversation_changed_during_get', pulse={'conversation_id': 'different-conversation'},
                          pulse_on_get=True)
        self.assertEqual(1, result['post_count'])
        self.assertFalse(result['done'], '原会话快照不能被重标为直播新出现的另一会话')
        self.assertEqual([], result['paths'])


if __name__ == '__main__':
    unittest.main()
