"""所有图片入口共用工作队列，许可覆盖完整的一次图片处理。"""
from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor


try:
    IMAGE_WORKER_LIMIT = max(1, int(os.getenv("CHATGPT2API_IMAGE_WORKER_LIMIT", "40")))
except ValueError:
    IMAGE_WORKER_LIMIT = 40


class ImageWorkPool:
    def __init__(self, limit: int):
        self.executor = ThreadPoolExecutor(max_workers=limit, thread_name_prefix="image-worker")
        self.local = threading.local()

    def _invoke(self, function, args, kwargs):
        self.local.active = True
        try:
            return function(*args, **kwargs)
        finally:
            self.local.active = False

    def submit(self, function, *args, **kwargs) -> Future:
        if getattr(self.local, "active", False):
            # 异步任务已经占用队列中的一个工作线程；内层生图原地执行，避免重复占槽死锁。
            future = Future()
            try:
                future.set_result(function(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)
            return future
        return self.executor.submit(self._invoke, function, args, kwargs)

    def run(self, function, *args, **kwargs):
        return self.submit(function, *args, **kwargs).result()


image_work_pool = ImageWorkPool(IMAGE_WORKER_LIMIT)
