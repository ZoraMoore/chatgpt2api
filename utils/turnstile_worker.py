"""受限子进程入口：只交换校验数据，不继承服务端密钥。"""
import json
import resource
import sys
import time
from pathlib import Path


def main() -> None:
    result = {"token": None, "reason": "worker_failed"}
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (1, 2))
        resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("input limit")
        payload = json.loads(raw)
        import quickjs
        context = quickjs.Context()
        context.set_memory_limit(32 * 1024 * 1024)
        context.set_max_stack_size(512 * 1024)
        context.set_time_limit(0.75)
        context.eval(Path(__file__).with_name("turnstile_vm.js").read_text(encoding="utf-8"))
        deadline = time.monotonic() + 0.9
        context.get("startSentinel")(payload["program"], json.dumps(payload["context"]), payload["key"])
        for _ in range(25000):
            encoded = context.eval("globalThis.__sentinelResult")
            if encoded is not None:
                result = json.loads(encoded)
                break
            if time.monotonic() > deadline or not context.execute_pending_job():
                result = {"token": None, "reason": "execution_limit"}
                break
    except Exception:
        # 引擎异常可能包含挑战片段，禁止输出原始异常和回溯。
        pass
    sys.stdout.write(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
