"""验证同步会话生成器及时向上游传递关闭，并回收其独占事件循环。"""
from __future__ import annotations

import asyncio
import gc
import json
import sys
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services import image_generation_stream as transport_stream
from services.image_quota import ImageQuotaExceeded
from services.protocol import conversation
import test_image_generation_stream as transport


def progress_payload():
    return json.dumps({"conversation_id": transport.CONVERSATION_ID, "message": {
        "id": "synthetic-progress", "author": {"role": "assistant"}, "status": "in_progress",
        "content": {"content_type": "text", "parts": ["synthetic progress"]}}})


def quota_event():
    return {"conversation_id": transport.CONVERSATION_ID, "message": {
        "id": "synthetic-quota", "author": {"role": "assistant"}, "channel": "final",
        "status": "finished_successfully", "end_turn": True,
        "content": {"content_type": "text", "parts": ["你已达到图像生成请求上限。"]}}}


class RetainedPayloads:
    """测试保留强引用，防止 CPython 引用计数替错误实现代为关闭资源。"""

    def __init__(self, values):
        self.closed = False
        self.values = iter(values)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.values)

    def close(self):
        self.closed = True


class ConversationCleanupTests(unittest.TestCase):
    def setUp(self):
        self.accounts = Mock()
        self.accounts.mark_image_quota_limit.side_effect = lambda token, evidence: evidence.record()
        self.enterContext(patch.object(conversation, "account_service", self.accounts))
        self.enterContext(patch("services.account_service.account_service", self.accounts))

    def outputs(self, payloads):
        backend = SimpleNamespace(access_token="synthetic", stream_conversation=lambda **kwargs: payloads)
        request = conversation.ConversationRequest(model="gpt-image-2.6-sunburst", prompt="synthetic",
                                                   image_transport="conversation")
        return conversation.stream_image_outputs(backend, request)

    def test_done_closes_retained_payload_source(self):
        payloads = RetainedPayloads([progress_payload(), "[DONE]", "must not be consumed"])
        events = list(conversation.iter_conversation_payloads(payloads))
        self.assertEqual("conversation.done", events[-1]["type"])
        self.assertTrue(payloads.closed, "遇到 DONE 提早结束时必须关闭上游")
        self.assertEqual("must not be consumed", next(payloads))

    def test_external_close_propagates_through_payload_adapter(self):
        payloads = RetainedPayloads([progress_payload(), "[DONE]"])
        events = conversation.iter_conversation_payloads(payloads)
        next(events)
        events.close()
        self.assertTrue(payloads.closed, "关闭事件适配器必须关闭其拥有的上游")

    def test_quota_exception_closes_upstream_before_it_reaches_caller(self):
        payloads = RetainedPayloads([json.dumps(quota_event()), "[DONE]"])
        with self.assertRaises(ImageQuotaExceeded):
            list(self.outputs(payloads))
        self.assertTrue(payloads.closed, "额度异常返回调用方前必须完成关闭链")

    def test_external_close_propagates_through_image_output_adapter(self):
        payloads = RetainedPayloads([progress_payload(), "[DONE]"])
        outputs = self.outputs(payloads)
        self.assertEqual("progress", next(outputs).kind)
        outputs.close()
        self.assertTrue(payloads.closed, "消费者关闭输出流时不能依赖后续 GC")

    def test_cleanup_failure_is_reported(self):
        payloads = RetainedPayloads([progress_payload(), "[DONE]"])
        payloads.close = Mock(side_effect=RuntimeError("synthetic cleanup failure"))
        outputs = self.outputs(payloads)
        next(outputs)
        with self.assertRaisesRegex(RuntimeError, "synthetic cleanup failure"):
            outputs.close()


class StreamLoopCleanupTests(unittest.TestCase):
    def setUp(self):
        self.h = transport.ImageGenerationStreamTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.accounts = Mock()
        self.accounts.mark_image_quota_limit.side_effect = lambda token, evidence: evidence.record()
        self.enterContext(patch.object(conversation, "account_service", self.accounts))
        self.enterContext(patch("services.account_service.account_service", self.accounts))

    def stream(self, values):
        server = self.h.server(values)
        backend = self.h.backend(server)
        return server, backend, self.h.stream(backend, timeout=.5)

    def capture_loop(self, stream):
        next(stream)
        loop = stream.gi_frame.f_locals["loop"]
        inner = stream.gi_frame.f_locals["stream"]
        self.addCleanup(self.finish_loop, loop, inner)
        return loop, inner

    @staticmethod
    def finish_loop(loop, inner):
        # 红灯阶段也须回收被测实现泄漏的本地连接，避免污染后续用例。
        if loop.is_closed():
            return
        try:
            loop.run_until_complete(inner.aclose())
        except asyncio.CancelledError:
            pass
        finally:
            tasks = list(asyncio.all_tasks(loop))
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def test_done_closes_connection_before_delivery(self):
        server, _, stream = self.stream(["[DONE]"])
        self.addCleanup(stream.close)
        self.assertEqual("[DONE]", next(stream))
        self.assertTrue(server.peer_closed.wait(.35))
        self.assertEqual(1, server.post_count)

    def test_explicit_close_reclaims_loop_and_connection_without_resubmit(self):
        server, _, stream = self.stream([json.loads(progress_payload())])
        loop, _ = self.capture_loop(stream)
        stream.close()
        self.assertTrue(loop.is_closed())
        self.h.assert_single_submission(server)

    def test_quota_error_closes_real_transport_before_caller_can_run_gc(self):
        server, backend, stream = self.stream([quota_event()])
        self.addCleanup(stream.close)
        backend.stream_conversation = lambda **kwargs: stream
        request = conversation.ConversationRequest(model="gpt-image-2.6-sunburst", prompt="synthetic",
                                                   image_transport="conversation")
        with self.assertRaises(ImageQuotaExceeded):
            list(conversation.stream_image_outputs(backend, request))
        self.assertTrue(server.peer_closed.wait(.35), "额度异常仍保留了真实 curl 连接")
        self.assertIsNone(stream.gi_frame)
        self.assertEqual(1, server.post_count)

    def test_explicit_close_inside_another_running_loop(self):
        server, _, stream = self.stream([json.loads(progress_payload())])
        loop, _ = self.capture_loop(stream)

        async def close_from_async_consumer():
            stream.close()

        asyncio.run(close_from_async_consumer())
        self.assertTrue(loop.is_closed())
        self.h.assert_single_submission(server)

    def test_gc_inside_another_running_loop_reclaims_owned_loop(self):
        server, _, stream = self.stream([json.loads(progress_payload())])
        loop, inner = self.capture_loop(stream)
        cycle = [stream]
        cycle.append(cycle)
        del stream, cycle
        unraisable = []

        async def collect_from_async_consumer():
            gc.collect()
            await asyncio.sleep(0)

        with warnings.catch_warnings(record=True) as emitted, patch.object(sys, "unraisablehook", unraisable.append):
            warnings.simplefilter("always", RuntimeWarning)
            asyncio.run(collect_from_async_consumer())
        closed_at_collection = loop.is_closed()
        self.finish_loop(loop, inner)
        self.assertEqual([], unraisable, "GC 清理不得抛出跨循环运行异常")
        self.assertEqual([], [str(w.message) for w in emitted if issubclass(w.category, RuntimeWarning)])
        self.assertTrue(closed_at_collection, "GC 后本次任务的独占循环仍未关闭")
        self.h.assert_single_submission(server)

    def test_parallel_stream_cleanup_keeps_requests_independent(self):
        servers = [self.h.server([json.loads(progress_payload())]) for _ in range(3)]
        backends = [self.h.backend(server) for server in servers]

        def consume(backend):
            stream = self.h.stream(backend, timeout=.5)
            next(stream)
            stream.close()

        with ThreadPoolExecutor(max_workers=3) as workers:
            list(workers.map(consume, backends))
        for server in servers:
            self.h.assert_single_submission(server)


if __name__ == "__main__":
    unittest.main()
