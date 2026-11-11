"""图片会话流的有限读取与原会话恢复；生成 POST 始终只发送一次。"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from curl_cffi import CurlOpt, requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests.exceptions import RequestException, Timeout

from services.protocol.final_image import FinalImageTracker, is_image_generation_failure
from services.protocol.conversation_document import allows_image_terminal_check
from services.protocol.terminal_image_delivery import _branch, _successful, select_terminal_image_delivery
from services.image_quota import ImageQuotaExceeded, image_readback_throttle_evidence_from_http, raise_image_quota
from utils.helper import ensure_ok, UpstreamHTTPError
from utils.log import logger


IMAGE_STREAM_IDLE_SECS = 15.0
IMAGE_STREAM_RECOVERY_INTERVAL_SECS = 5.0
IMAGE_STREAM_FINAL_RECOVERY_SECS = 10.0
IMAGE_STREAM_MAX_PROBE_INTERVAL_SECS = 30.0
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


def _terminal_reply(state: FinalImageTracker) -> bool:
    final = state.final
    if (not final or final.get('id') != state.current.get('id')
            or final.get('end_turn') is not True
            or not _successful({**final, 'metadata': final.get('metadata') or {}})):
        return False
    try:
        return state.generated or state.image_generation_failed or bool(state.paths())
    except ValueError:
        return False


def _completed_messages(document, conversation_id: str, submitted_id: str, *, observed=None,
                        required_tool_calls=None, diagnostic=None):
    """只读回本次提交所在的完整当前分支，不能用旧轮次或中间图片兜底。"""
    if diagnostic is not None:
        diagnostic.clear()
    def reject(reason):
        if diagnostic is not None:
            diagnostic['reason'] = reason
        return None
    if not isinstance(document, dict):
        return reject('invalid_document')
    if not isinstance(submitted_id, str) or not _SAFE_ID.fullmatch(submitted_id):
        return reject('invalid_submission_id')
    ids = [document[k] for k in ('conversation_id', 'id') if k in document]
    if not ids or any(value != conversation_id for value in ids):
        return reject('conversation_mismatch')
    messages = _branch(document)
    if not messages:
        return reject('invalid_branch')
    users = [i for i, message in enumerate(messages) if message['author']['role'] == 'user']
    if not users or messages[users[-1]]['id'] != submitted_id:
        return reject('submission_mismatch')
    if not allows_image_terminal_check(document):
        status = document.get('async_status')
        if diagnostic is not None and type(status) is int and status == 3:
            last = messages[-1]
            diagnostic['async_status'] = 3
            diagnostic['terminal_message_ready'] = bool(
                last['author']['role'] == 'assistant' and last.get('recipient', 'all') == 'all'
                and last.get('end_turn') is True and _successful(last))
        return reject('document_in_progress' if type(status) is int and status == 3 else 'unsupported_async_status')
    turn = messages[users[-1]:]
    tools = {m['id'] for m in turn if m['author']['role'] == 'tool'}
    if observed is not None and not observed._seen_tool_messages.issubset(tools):
        return reject('observed_tools_missing')
    calls = {m['id']: m.get('recipient') for m in turn if m['author']['role'] == 'assistant'}
    if any(calls.get(key) != value for key, value in (required_tool_calls or {}).items()):
        return reject('calls_missing')
    if any(not _successful(m) and not is_image_generation_failure(m)
           for m in turn if m['author']['role'] == 'tool'):
        return reject('tool_incomplete')
    tracker = FinalImageTracker()
    for message in turn:
        tracker.update({'conversation_id': conversation_id, 'message': message})
    if not _terminal_reply(tracker):
        return reject('final_incomplete')
    try:
        paths = tracker.paths()
    except ValueError:
        return reject('invalid_final_path')
    if tracker.postprocessing and not paths and not tracker.image_generation_failed and select_terminal_image_delivery(
            document, conversation_id=conversation_id, submitted_message_id=submitted_id) is None:
        return reject('postprocess_artifact_missing')
    if diagnostic is not None:
        diagnostic['reason'] = 'complete'
    return turn


def _make_async_session(session, timeout_secs: float):
    # 复用原账号的 Cookie 域/路径、代理和 TLS 指纹；不能重新选号或更换出口。
    names = ('headers', 'cookies', 'proxies', 'proxy_auth', 'verify', 'trust_env', 'impersonate',
             'http_version', 'default_headers', 'ja3', 'akamai', 'perk', 'extra_fp', 'interface', 'cert')
    settings = {name: getattr(session, name) for name in names if hasattr(session, name)}
    settings['curl_options'] = dict(getattr(session, 'curl_options', {}))
    # 共享硬超时会覆盖恢复 GET 的短预算；墙钟截止时间由下面的事件循环统一执行。
    settings['curl_options'].pop(CurlOpt.TIMEOUT_MS, None)
    return requests.AsyncSession(**settings, max_clients=2, retry=0, timeout=timeout_secs)


async def _read_completed_turn(session, backend, submitted_id: str, timeout_secs: float, *,
                               observed=None, required_tool_calls=None, diagnostic=None):
    info = diagnostic if diagnostic is not None else {}
    info.clear()
    info['reason'] = 'no_conversation_or_budget'
    info['duration_ms'] = 0
    cid = getattr(backend, 'image_conversation_id', '')
    if not isinstance(cid, str) or not _SAFE_ID.fullmatch(cid) or timeout_secs <= 0:
        return None
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = loop.time() + timeout_secs
    for prefix in ('/backend-api/conversations/', '/backend-api/conversation/'):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        path = prefix + cid
        response = None
        body = bytearray()
        too_large = False
        def collect(chunk):
            nonlocal too_large
            if len(body) + len(chunk) > _MAX_DOCUMENT_BYTES:
                # libcurl 的明确中止返回值，不先把无界历史正文装入内存。
                too_large = True
                return CURL_WRITEFUNC_ERROR
            body.extend(chunk)
            return len(chunk)
        try:
            response = await asyncio.wait_for(session.get(
                backend.base_url + path,
                headers={'Accept': 'application/json', 'X-OpenAI-Target-Path': path,
                         'X-OpenAI-Target-Route': path},
                timeout=remaining, allow_redirects=False, content_callback=collect,
            ), timeout=remaining)
            response.content = bytes(body)
            info['http_status'] = response.status_code
            if response.status_code == 404 and prefix.endswith('conversations/'):
                continue
            if too_large or len(response.content) > _MAX_DOCUMENT_BYTES:
                info['reason'] = 'document_too_large'
                return None
            if response.status_code != 200:
                info['reason'] = 'http_status'
                if response.status_code == 429:
                    try:
                        ensure_ok(response, path)
                    except UpstreamHTTPError as exc:
                        evidence = image_readback_throttle_evidence_from_http(exc)
                        if evidence is not None:
                            raise_image_quota(backend, evidence)
                return None
            try:
                document = response.json()
            except (ValueError, TypeError) as exc:
                info['reason'] = 'invalid_json'
                info['exception_type'] = type(exc).__name__
                return None
            gate = {}
            messages = _completed_messages(document, cid, submitted_id, observed=observed,
                                           required_tool_calls=required_tool_calls, diagnostic=gate)
            info.update(gate)
            return messages
        except (RequestException, asyncio.TimeoutError, ValueError, TypeError) as exc:
            info['reason'] = 'document_too_large' if too_large else 'transport_error'
            info['exception_type'] = type(exc).__name__
            code = getattr(exc, 'code', None)
            if isinstance(code, int):
                info['exception_code'] = code
            return None
        finally:
            info['duration_ms'] = round((loop.time() - started) * 1000, 3)
            if response is not None:
                await response.aclose()
    return None


def _recovered_payloads(backend, messages, reason: str):
    cid = backend.image_conversation_id
    logger.info({'event': 'image_stream_recovered', 'conversation_id': cid, 'reason': reason,
                 'message_count': len(messages)})
    # 原始流可能只送达部分消息；按 ID 重放完整本轮，现有 tracker 会去重工具计数。
    for message in messages:
        yield json.dumps({'conversation_id': cid, 'message': message}, ensure_ascii=False)
    yield '[DONE]'


def _observe_line(line, backend, state, observed_calls):
    line = line.decode('utf-8', errors='replace') if isinstance(line, bytes) else str(line)
    if not line.startswith('data:') or not (value := line[5:].strip()):
        return '', False
    if value == '[DONE]':
        return value, True
    try:
        event = json.loads(value)
    except (ValueError, TypeError):
        return value, False
    if not isinstance(event, dict):
        return value, False
    previous = copy.deepcopy(state.current)
    previous_cid, previous_complete = state.conversation_id, state.stream_complete
    state.update(event)
    message = state.current
    author = message.get('author') or {}
    recipient = message.get('recipient')
    if author.get('role') == 'assistant' and isinstance(recipient, str) and recipient != 'all':
        mid = message.get('id')
        if (not isinstance(mid, str) or not _SAFE_ID.fullmatch(mid)
                or len(observed_calls) >= 4096 and mid not in observed_calls):
            raise ValueError('invalid image tool call identity')
        observed_calls[mid] = recipient
    if state.conversation_id and _SAFE_ID.fullmatch(state.conversation_id):
        backend.image_conversation_id = state.conversation_id
    # v 也会承载心跳和无关元数据；收到帧不能等同于生成取得进展。
    progress = (state.current != previous or state.conversation_id != previous_cid
                or state.stream_complete != previous_complete)
    return value, progress


async def _stream(backend, payload, headers, timeout_secs):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_secs
    last_probe_at = loop.time()
    next_probe = last_probe_at + IMAGE_STREAM_IDLE_SECS
    session = _make_async_session(backend.session, timeout_secs)
    response = iterator = pending = probe = None
    submitted_id = backend._image_submitted_message_id
    state = FinalImageTracker()
    observed_calls = {}
    failure = None
    readback_limit = None
    recovery_deadline = None
    probe_info = {}
    probe_cid = ''
    attempts = 0
    last_report = None
    last_report_at = 0.0

    def end_without_result():
        # 新普通会话选择了无图即失败；真实断流也不能再隐式等到整段恢复预算耗尽。
        # 已见图片仍保留原会话恢复，避免丢掉只差终态确认的成品。
        if getattr(backend, '_image_fail_without_result_on_stream_end', False) is not True:
            return False
        if state.generated:
            return False
        try:
            if state.paths():
                return False
        except ValueError:
            pass
        logger.info({'event': 'image_stream_ended_without_image',
                     'conversation_id': getattr(backend, 'image_conversation_id', ''),
                     'recovery_skipped': True})
        return True

    def mark_interrupted(exc):
        nonlocal failure, recovery_deadline, next_probe
        if readback_limit is not None:
            # 主流确已断开，而读取也受限时才结束；不能重放已经提交的生成。
            raise readback_limit
        if isinstance(exc, asyncio.TimeoutError):
            exc = Timeout('image generation stream deadline exceeded', code=28)
        cid = getattr(backend, 'image_conversation_id', '')
        if not isinstance(cid, str) or not _SAFE_ID.fullmatch(cid):
            raise exc
        if failure is None:
            failure = exc
            # 生成只提交一次；失败后继续只读原会话，最多增加一次有限的最终确认窗口。
            recovery_deadline = max(deadline, loop.time() + IMAGE_STREAM_FINAL_RECOVERY_SECS)
            logger.warning({'event': 'image_stream_read_interrupted', 'conversation_id': cid,
                            'error_type': type(exc).__name__, 'error_code': getattr(exc, 'code', None),
                            'recovery_attempts': attempts})
        next_probe = min(next_probe, loop.time())

    def report_probe(info):
        nonlocal last_report, last_report_at
        signature = (info.get('reason'), info.get('http_status'), info.get('exception_type'))
        if signature != last_report or info.get('reason') == 'complete' or loop.time() - last_report_at >= 30:
            # 只输出固定原因、状态码和计数，不记录响应正文、提示词、URL或授权头。
            logger.info({'event': 'image_stream_readback',
                         'conversation_id': getattr(backend, 'image_conversation_id', ''),
                         'attempt': attempts, 'observed_tools': len(state._seen_tool_messages),
                         'observed_calls': len(observed_calls), **info})
            last_report, last_report_at = signature, loop.time()

    try:
        try:
            response = await asyncio.wait_for(session.post(
                backend.base_url + '/backend-api/f/conversation', headers=headers, json=payload,
                timeout=(min(30.0, timeout_secs), timeout_secs), stream=True, allow_redirects=False,
            ), timeout=timeout_secs)
            if not 200 <= response.status_code < 300:
                body = bytearray()
                async def error_body():
                    async for chunk in response.aiter_content():
                        body.extend(chunk[:65536 - len(body)])
                        if len(body) >= 65536:
                            break
                await asyncio.wait_for(error_body(), timeout=max(0.001, deadline - loop.time()))
                response.content = bytes(body)
            ensure_ok(response, '/backend-api/f/conversation')
            iterator = response.aiter_lines().__aiter__()
        except (RequestException, asyncio.TimeoutError) as exc:
            if end_without_result():
                return
            mark_interrupted(exc)

        while True:
            now = loop.time()
            active_deadline = recovery_deadline if recovery_deadline is not None else deadline
            if now >= active_deadline:
                if readback_limit is not None:
                    raise readback_limit
                if failure is not None:
                    logger.warning({'event': 'image_stream_recovery_exhausted',
                                    'conversation_id': getattr(backend, 'image_conversation_id', ''),
                                    'attempts': attempts, 'last_reason': probe_info.get('reason'),
                                    'last_http_status': probe_info.get('http_status')})
                    raise failure
                if end_without_result():
                    return
                mark_interrupted(Timeout('image generation stream deadline exceeded', code=28))
                active_deadline = recovery_deadline
            if iterator is not None and pending is None:
                pending = asyncio.create_task(anext(iterator))
            if readback_limit is None and probe is None and loop.time() >= next_probe:
                attempts += 1
                probe_info = {}
                probe_cid = getattr(backend, 'image_conversation_id', '')
                # 固定本次GET的会话身份，不能在直播切换ID后把旧快照重新贴成新会话。
                probe_backend = SimpleNamespace(image_conversation_id=probe_cid, base_url=backend.base_url,
                                                access_token=getattr(backend, 'access_token', ''))
                probe = asyncio.create_task(_read_completed_turn(
                    session, probe_backend, submitted_id,
                    min(IMAGE_STREAM_FINAL_RECOVERY_SECS, max(0.001, active_deadline - loop.time())),
                    observed=state, required_tool_calls=observed_calls, diagnostic=probe_info,
                ))
            tasks = [task for task in (pending, probe) if task is not None]
            remaining = max(0.0, active_deadline - loop.time())
            wait = remaining if probe is not None else min(remaining, max(0.0, next_probe - loop.time()))
            if tasks:
                await asyncio.wait(tasks, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.sleep(wait)

            # GET与直播并行；先消费已就绪直播，不能让慢GET阻挡已经到达的最终回复。
            # 有界批次避免持续流量独占循环；缓冲区尚有帧时下一轮继续消费，再判断候选。
            for _ in range(256):
                if pending is None or not pending.done():
                    break
                task, pending = pending, None
                try:
                    line = task.result()
                except StopAsyncIteration:
                    iterator = None
                    if readback_limit is not None:
                        raise readback_limit
                    if end_without_result():
                        return
                    mark_interrupted(RequestException('image generation stream ended before its final result', code=18))
                    break
                except (RequestException, asyncio.TimeoutError) as exc:
                    iterator = None
                    if readback_limit is not None:
                        raise readback_limit
                    if end_without_result():
                        return
                    mark_interrupted(exc)
                    break
                value, progress = _observe_line(line, backend, state, observed_calls)
                if value:
                    yield value
                    if value == '[DONE]':
                        return
                    if _terminal_reply(state):
                        logger.info({'event': 'image_stream_terminal_received',
                                     'conversation_id': state.conversation_id, 'recovery_attempts': attempts})
                        yield '[DONE]'
                        return
                if progress and probe is None and readback_limit is None:
                    next_probe = min(loop.time() + IMAGE_STREAM_IDLE_SECS,
                                     last_probe_at + IMAGE_STREAM_MAX_PROBE_INTERVAL_SECS)
                if iterator is not None:
                    pending = asyncio.create_task(anext(iterator))
                    await asyncio.sleep(0)
            if pending is not None and pending.done():
                continue
            if probe is not None and probe.done():
                try:
                    messages = probe.result()
                except ImageQuotaExceeded as exc:
                    if exc.record.get('source') != 'upstream_http_conversation_throttle':
                        raise
                    # 读接口429已登记账号冷却；当前生成已提交，继续接收它的SSE结果。
                    # 本次请求停止后续GET，交付层也不能再次轮询同一受限接口。
                    readback_limit = exc
                    backend._image_readback_limit_error = exc
                    probe = None
                    next_probe = float('inf')
                    if iterator is None or failure is not None:
                        raise
                    logger.warning({'event': 'image_readback_throttled_stream_continues',
                                    'conversation_id': probe_cid, 'http_status': 429})
                    continue
                probe = None
                last_probe_at = loop.time()
                next_probe = last_probe_at + IMAGE_STREAM_RECOVERY_INTERVAL_SECS
                if messages is not None:
                    # 候选已通过完整文档校验，仍须以查询期间新增的工具观察重验。
                    # ping/空行不会改变工具证据，不能再无条件丢弃已完成结果。
                    latest = {}
                    cid = getattr(backend, 'image_conversation_id', '')
                    if cid != probe_cid:
                        messages = None
                        latest['reason'] = 'conversation_changed'
                    else:
                        messages = _completed_messages(
                            {'conversation_id': probe_cid, 'messages': messages}, probe_cid, submitted_id,
                            observed=state, required_tool_calls=observed_calls, diagnostic=latest,
                        )
                    probe_info.update(latest)
                report_probe(probe_info)
                if messages is not None:
                    for value in _recovered_payloads(backend, messages, 'stream_error' if failure else 'idle'):
                        yield value
                    return
    finally:
        for task in (pending, probe):
            if task is not None and not task.done():
                task.cancel()
        try:
            backend.session.cookies.update(session.cookies)
        except Exception:
            logger.warning({'event': 'image_stream_cookie_merge_failed'})
        # 必须先主动移除curl句柄，不能等待静默响应自己关闭。
        await session.close()
        await asyncio.gather(*(task for task in (pending, probe) if task is not None), return_exceptions=True)
        if response is not None:
            await response.aclose()
        if iterator is not None:
            await iterator.aclose()


def _close_image_stream_loop(loop, stream):
    def close():
        try:
            try:
                loop.run_until_complete(stream.aclose())
            finally:
                # 此循环仅归本次图片任务所有；取消时也必须回收尚未完成的读取协程。
                tasks = list(asyncio.all_tasks(loop))
                for task in tasks:
                    task.cancel()
                if tasks:
                    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        close()
    else:
        # 外部 close 或 GC 可能发生在另一个运行中循环的线程；仍在独占循环上收尾，
        # 但不能在该线程嵌套 run_until_complete。等待收尾完成，并向调用方保留异常。
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='image-stream-cleanup') as worker:
            worker.submit(close).result()


def stream_image_generation(backend, payload, headers, timeout_secs):
    if (isinstance(timeout_secs, bool) or not isinstance(timeout_secs, (int, float))
            or not math.isfinite(timeout_secs) or timeout_secs <= 0):
        raise ValueError('invalid image generation stream timeout')
    loop = asyncio.new_event_loop()
    stream = _stream(backend, payload, headers, timeout_secs)
    try:
        while True:
            try:
                value = loop.run_until_complete(anext(stream))
            except StopAsyncIteration:
                return
            if value == '[DONE]':
                # 消费者收到 DONE 后通常立即停止迭代；交付它之前先完成连接回收。
                loop.run_until_complete(stream.aclose())
                yield value
                return
            yield value
    finally:
        _close_image_stream_loop(loop, stream)
