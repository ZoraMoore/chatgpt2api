"""图片接口传输日志仅描述 ASGI 发送过程，不能当作下游业务回执。"""
from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


class ImageDeliveryTraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.trace = importlib.import_module("services.image_delivery_trace")
        self.logger = Mock()
        fake_log_module = ModuleType("services.log_service")
        fake_log_module.log_service = self.logger
        self.log_patch = patch.dict(sys.modules, {"services.log_service": fake_log_module})
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    @staticmethod
    def scope(headers=(), *, path="/v1/images/generations", type="http"):
        return {"type": type, "path": path, "headers": list(headers)}

    async def run_request(self, app, scope=None, *, receiver=None, sender=None):
        sent = []

        async def receive():
            self.fail("追踪中间件不得主动读取请求")

        async def send(message):
            sent.append(message)

        await self.trace.ImageDeliveryTraceMiddleware(app)(
            scope or self.scope(), receiver or receive, sender or send
        )
        return sent

    def detail(self):
        self.logger.add.assert_called_once()
        category, summary, detail = self.logger.add.call_args.args
        self.assertEqual(category, "delivery")
        self.assertIsInstance(summary, str)
        return detail

    async def test_body_passthrough_and_completed_send_is_not_delivery_ack(self):
        start = {"type": "http.response.start", "status": 200, "headers": [(b"x-secret", b"hidden")]}
        first = {"type": "http.response.body", "body": b"first", "more_body": True}
        last = {"type": "http.response.body", "body": b"last"}
        observed = []

        async def app(scope, receive, send):
            observed.append(self.trace.get_image_delivery_trace())
            await send(start)
            await send(first)
            await send(last)

        messages = await self.run_request(app, self.scope([
            (b"x-request-id", b"req.valid_01:2-3"),
            (b"authorization", b"must-never-be-logged"),
        ]))
        self.assertTrue(all(left is right for left, right in zip(messages, [start, first, last])))
        detail = self.detail()
        self.assertEqual(detail["client_request_id"], "req.valid_01:2-3")
        self.assertEqual(detail["call_id"], observed[0]["call_id"])
        self.assertEqual(detail["response_status"], 200)
        self.assertEqual(detail["response_bytes"], 9)
        self.assertEqual(detail["send_state"], "response_send_completed")
        self.assertFalse(detail["client_disconnected"])
        self.assertGreaterEqual(detail["duration_ms"], 0)
        self.assertNotIn("received", detail)
        self.assertNotIn("must-never", repr(detail))
        self.assertNotIn("hidden", repr(detail))
        self.assertEqual(self.trace.get_image_delivery_trace(), {})

    async def test_trace_is_safe_copy_and_concurrent_requests_are_isolated(self):
        ready = asyncio.Event()
        observations = {}

        async def app(scope, receive, send):
            trace = self.trace.get_image_delivery_trace()
            request_id = trace["client_request_id"]
            trace["call_id"] = "must-not-change-context"
            trace["secret"] = "must-not-be-logged"
            if request_id == "first":
                await ready.wait()
            else:
                ready.set()
            observations[request_id] = self.trace.get_image_delivery_trace()
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b""})

        await asyncio.gather(*[
            self.run_request(app, self.scope([(b"x-request-id", request_id)]))
            for request_id in (b"first", b"second")
        ])
        self.assertNotEqual(observations["first"]["call_id"], observations["second"]["call_id"])
        for request_id, trace in observations.items():
            self.assertEqual(trace["client_request_id"], request_id)
            self.assertNotEqual(trace["call_id"], "must-not-change-context")
            self.assertNotIn("secret", trace)
        self.assertEqual(self.logger.add.call_count, 2)
        self.assertEqual(self.trace.get_image_delivery_trace(), {})

    async def test_untrusted_request_ids_are_rejected(self):
        cases = [
            [(b"x-request-id", b"")],
            [(b"x-request-id", b"x" * 129)],
            [(b"x-request-id", b"bad\r\nheader")],
            [(b"x-request-id", b"one,two")],
            [(b"x-request-id", "中文".encode())],
            [(b"x-request-id", b"one"), (b"x-request-id", b"two")],
            [(b"x-client-request-id", b"one"), (b"x-client-request-id", b"two")],
        ]
        async def app(scope, receive, send):
            self.assertNotIn("client_request_id", self.trace.get_image_delivery_trace())
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body"})

        for headers in cases:
            with self.subTest(headers=headers):
                self.logger.reset_mock()
                await self.run_request(app, self.scope(headers))
                self.assertNotIn("client_request_id", self.detail())

    async def test_client_request_id_fallback_and_primary_precedence(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body"})

        for headers, expected in [
            ([(b"x-client-request-id", b"fallback")], "fallback"),
            ([(b"x-request-id", b"primary"), (b"x-client-request-id", b"fallback")], "primary"),
            ([(b"x-request-id", b"bad,primary"), (b"x-client-request-id", b"fallback")], None),
        ]:
            self.logger.reset_mock()
            await self.run_request(app, self.scope(headers))
            self.assertEqual(self.detail().get("client_request_id"), expected)

    async def test_receive_passthrough_records_only_observed_disconnect(self):
        events = [{"type": "http.request", "body": b"private-input"}, {"type": "http.disconnect"}]
        received = []

        async def receive():
            event = events[len(received)]
            received.append(event)
            return event

        async def app(scope, receive, send):
            self.assertIs(await receive(), events[0])
            self.assertIs(await receive(), events[1])

        await self.run_request(app, receiver=receive)
        detail = self.detail()
        self.assertTrue(detail["client_disconnected"])
        self.assertEqual(detail["send_state"], "response_incomplete")
        self.assertEqual(detail["response_bytes"], 0)
        self.assertIsNone(detail["response_status"])
        self.assertNotIn("private-input", repr(detail))

    async def test_send_failure_preserves_exception_and_counts_only_sent_bytes(self):
        error = ConnectionResetError("secret connection detail")

        async def send(message):
            if message.get("body") == b"failed":
                raise error

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"sent", "more_body": True})
            await send({"type": "http.response.body", "body": b"failed"})

        with self.assertRaises(ConnectionResetError) as caught:
            await self.run_request(app, sender=send)
        self.assertIs(caught.exception, error)
        detail = self.detail()
        self.assertEqual(detail["send_state"], "send_failed")
        self.assertEqual(detail["response_bytes"], 4)
        self.assertNotIn("secret", repr(detail))
        self.assertEqual(self.trace.get_image_delivery_trace(), {})

    async def test_app_failure_and_cancellation_preserve_original_exception(self):
        for error in (RuntimeError("secret app detail"), asyncio.CancelledError("secret cancel detail")):
            with self.subTest(error=type(error).__name__):
                self.logger.reset_mock()

                async def app(scope, receive, send):
                    raise error

                with self.assertRaises(type(error)) as caught:
                    await self.run_request(app)
                self.assertIs(caught.exception, error)
                detail = self.detail()
                self.assertEqual(detail["send_state"], "app_failed")
                self.assertNotIn("secret", repr(detail))
                self.assertEqual(self.trace.get_image_delivery_trace(), {})

    async def test_caught_send_failure_cannot_be_relabelled_completed(self):
        async def send(message):
            if message.get("body") == b"failed":
                raise ConnectionResetError("secret connection detail")

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            try:
                await send({"type": "http.response.body", "body": b"failed", "more_body": True})
            except ConnectionResetError:
                pass
            await send({"type": "http.response.body", "body": b""})

        await self.run_request(app, sender=send)
        self.assertEqual(self.detail()["send_state"], "send_failed")

    async def test_log_failure_does_not_break_response_or_hide_app_failure(self):
        self.logger.add.side_effect = OSError("secret storage detail")

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"image"})

        sent = await self.run_request(app)
        self.assertEqual(sent[-1]["body"], b"image")
        self.assertEqual(self.trace.get_image_delivery_trace(), {})
        error = RuntimeError("original error")

        async def broken_app(scope, receive, send):
            raise error

        with self.assertRaises(RuntimeError) as caught:
            await self.run_request(broken_app)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.trace.get_image_delivery_trace(), {})

    async def test_non_image_http_and_non_http_scopes_are_untouched(self):
        for scope in (
            self.scope(path="/v1/chat/completions"),
            self.scope(path="/v1/images-extra/generations"),
            self.scope(type="websocket"),
        ):
            with self.subTest(scope=scope):
                original = SimpleNamespace()

                async def app(seen_scope, receive, send):
                    self.assertIs(seen_scope, scope)
                    self.assertIs(receive, original.receive)
                    self.assertIs(send, original.send)
                    self.assertEqual(self.trace.get_image_delivery_trace(), {})

                async def receive():
                    return {"type": "http.request"}

                async def send(message):
                    pass

                original.receive, original.send = receive, send
                await self.trace.ImageDeliveryTraceMiddleware(app)(scope, receive, send)
        self.logger.add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
