"""只解析明确的图像限额证据，不把用户引用、网络或内容政策错误当成额度不足。"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from services.protocol.conversation_document import conversation_timestamp, iter_conversation_messages

ACTIVE_STATES = {'cooldown', 'awaiting_check', 'probe_ready', 'probing'}
CHECK_RETRY = 300
MAX_RESET_SECONDS = 30 * 86400


@dataclass(frozen=True)
class ImageQuotaEvidence:
    reason: str
    source: str
    conversation_id: str
    message_id: str
    event_id: str
    occurred_at: float
    observed_at: float
    restore_at: float | None
    time_source: str = 'unknown'
    next_check_at: float | None = None

    def record(self):
        return asdict(self)


class ImageQuotaExceeded(RuntimeError):
    def __init__(self, record):
        self.record = dict(record)
        self.conversation_id = str(record.get('conversation_id') or '')
        self.restore_at = _finite_number(record.get('restore_at'))
        deadline = None
        if self.restore_at is not None and self.restore_at > 0:
            try:
                deadline = datetime.fromtimestamp(self.restore_at, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
            except (OverflowError, OSError, ValueError):
                pass
        if deadline:
            detail = '\n预计恢复时间：' + deadline + '（北京时间，到期后仍需验证额度恢复）。'
        else:
            self.restore_at = None
            detail = '\n上游未提供恢复时间，稍后将重新查询额度。'
        super().__init__(str(record.get('reason') or '图像生成额度不足。') + detail)


def active_image_limit(account):
    record = account.get('image_limit') if isinstance(account, dict) else None
    return record if isinstance(record, dict) and record.get('state') in ACTIVE_STATES else None


def _text(content):
    if not isinstance(content, dict):
        return content if isinstance(content, str) else ''
    parts = content.get('parts')
    values = [part for part in parts if isinstance(part, str)] if isinstance(parts, list) else []
    if isinstance(content.get('text'), str):
        values.append(content['text'])
    return '\n'.join(values)


def _error_text(value, depth=0):
    if isinstance(value, str):
        if len(value) > 4096:
            return ''
        if value.strip().startswith('{') and depth < 3:
            try:
                return _error_text(json.loads(value), depth+1)
            except ValueError:
                pass
        return re.sub(r'^system_error\s*:\s*', '', value.strip())
    if not isinstance(value, dict) or depth > 3:
        return ''
    for key in ('message', 'detail', 'text', 'reason', 'error_message', 'error'):
        result = _error_text(value.get(key), depth+1)
        if result:
            return result
    return ''


def _clear_limit_text(text, user_text=''):
    if not isinstance(text, str) or not 1 <= len(text) <= 4096:
        return False
    plain = text.strip().replace('**', '').replace('__', '')
    if not re.search(r'图像生成|图片生成|生图|image[ _-]*(?:generation|creation)|generat\w*\s+(?:more\s+)?images?', plain, re.I):
        return False
    if not re.search(r'上限|限额|额度|配额|次数限制|\blimit\b|\bquota\b|maximum number', plain, re.I):
        return False
    if re.search(r'例如|假设|仅为示例|只是引用|并未达到|尚未达到|没有达到|not (?:reached|hit)', plain, re.I):
        return False
    prefix = (r'^(?:(?:很|非常)?(?:抱歉|对不起)[，,。!！\s]*)?(?:你|您)(?:已(?:经)?|目前已)?(?:达到|达到了|用尽|耗尽|触及)'
              r'|^(?:Sorry[,!:.\s]+)?You(?:\s+have|[’\x27]ve)?\s+(?:hit|reached|exhausted|used up)\b'
              r'|^(?:Image generation|图像生成|图片生成)[^\n。]{0,90}(?:limit (?:reached|exceeded)|已达.{0,12}上限)')
    if not re.search(prefix, plain, re.I):
        return False
    # 最终回复复述了用户给出的限额文案时，不把这种引用当作上游额度证据。
    core = re.split(r'[。.!\n]', plain, maxsplit=1)[0]
    compact = lambda value: re.sub(r'[\s*_:：，,]', '', value).lower()
    return not (user_text and compact(core) in compact(user_text))


def _number(raw):
    if raw == '半':
        return 0.5
    try:
        return float(raw)
    except ValueError:
        digits = {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
        if '十' in raw:
            head, tail = raw.split('十', 1)
            if (head and head not in digits) or (tail and tail not in digits):
                return None
            return digits.get(head, 1)*10 + digits.get(tail, 0)
        return digits.get(raw)


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str) and not 1 <= len(value) <= 128:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, OverflowError):
        return None


def resolve_image_reset(value, *, now, relative=False) -> float | None:
    """解析明确的秒数或带时区绝对时间；相对秒数必须由调用方显式指定。"""
    now = _finite_number(now)
    if now is None or now <= 0:
        return None
    number = _finite_number(value)
    if number is not None:
        if relative:
            return now + number if 0 <= number <= MAX_RESET_SECONDS else None
        if number >= 1e12:
            number /= 1000
        return number if 0 < number <= now + MAX_RESET_SECONDS else None
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        # 无时区文案不能假定是本机时间，否则会错误提前解除冷却。
        if parsed.tzinfo is None:
            return None
        number = parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return number if math.isfinite(number) and 0 < number <= now + MAX_RESET_SECONDS else None


def resolve_retry_after(value, *, now) -> float | None:
    """将 Retry-After 秒数或 HTTP 日期统一为绝对截止时间。"""
    now = _finite_number(now)
    if now is None or now <= 0:
        return None
    number = _finite_number(value)
    if number is not None:
        return resolve_image_reset(number, now=now, relative=True)
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
        if parsed.tzinfo is None:
            return None
        timestamp = parsed.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if math.isfinite(timestamp) and timestamp <= now + MAX_RESET_SECONDS:
        return max(now, timestamp)
    return None


def _timing_containers(message):
    pending = [(message, 0)]
    count = 0
    while pending and count < 32:
        item, depth = pending.pop(0)
        if isinstance(item, str) and 1 <= len(item) <= 4096 and item.lstrip().startswith('{'):
            try:
                item = json.loads(item)
            except ValueError:
                continue
        if not isinstance(item, dict):
            continue
        count += 1
        yield item
        if depth < 3:
            for key in ('metadata', 'content', 'error', 'system_error', 'detail', 'image_gen_message'):
                if isinstance(item.get(key), (dict, str)):
                    pending.append((item[key], depth + 1))


_AMOUNT = r'(?:\d{1,8}(?:\.\d{1,6})?|[一二两三四五六七八九十]{1,3}|半)'
_UNIT = r'(?:小时|分钟|秒钟?|天|hours?|minutes?|seconds?|days?)'
_DURATION_PART = rf'{_AMOUNT}\s*(?:个\s*)?{_UNIT}'
_DURATION = rf'(?<![\d.+\-一二两三四五六七八九十半]){_DURATION_PART}(?:\s*(?:and|又|零|,|，)?\s*{_DURATION_PART}){{0,5}}'
_RECOVERY_DURATIONS = (
    re.compile(rf'(?P<duration>{_DURATION})\s*(?:之?后|以后)\s*'
               r'(?:(?:即可|可以|可|再次|重新|再|请|将|会|才能)\s*){0,3}(?:重试|再试|恢复|重置|生成|创建)', re.I),
    re.compile(rf'(?P<duration>{_DURATION})\s*(?:恢复|重置)', re.I),
    re.compile(r'(?:等待|等候|(?:恢复|重置)(?:还需|需要|需|将在)|try\s+again\s+(?:in|after)|'
               r'retry\s+(?:in|after)|(?:please\s+)?wait(?:\s+for)?|'
               r'(?:reset|restore|recover)(?:s|ing)?\s+(?:in|after)|'
               rf'(?:create|generate)\s+more\s+images\s+in)\s*(?P<duration>{_DURATION})', re.I),
)


def _cooldown_seconds(text):
    if not isinstance(text, str) or not 1 <= len(text) <= 4096:
        return None
    durations = []
    for pattern in _RECOVERY_DURATIONS:
        for match in pattern.finditer(text):
            seconds = 0
            for number, unit in re.findall(rf'({_AMOUNT})\s*(?:个\s*)?({_UNIT})', match['duration'], re.I):
                amount = _number(number)
                if amount is None:
                    break
                unit = unit.lower()
                factor = 86400 if unit in ('天', 'day', 'days') else 3600 if unit.startswith(('小时', 'hour')) else 60 if unit.startswith(('分钟', 'minute')) else 1
                seconds += amount * factor
            else:
                if 0 <= seconds <= MAX_RESET_SECONDS:
                    durations.append(seconds)
    return max(durations) if durations else None


def resolve_image_cooldown(message, *, now, retry_after=None, occurred_at=None) -> dict:
    """真实恢复时间与未知时间的复查退避分别保存，不能用默认等待冒充上游证据。"""
    now = _finite_number(now)
    if now is None or now <= 0:
        raise ValueError('invalid image quota observation time')
    containers = list(_timing_containers(message))
    base = resolve_image_reset(occurred_at, now=now)
    if base is None or base > now + 300:
        base = now
    absolute = []
    relative = []
    for container in containers:
        for key in ('reset_at', 'restore_at', 'retry_at'):
            candidate = resolve_image_reset(container.get(key), now=now)
            if candidate is not None:
                absolute.append(candidate)
        value = container.get('reset_after')
        number = _finite_number(value)
        if number is None or number >= 1e9:
            candidate = resolve_image_reset(value, now=now)
            if candidate is not None:
                absolute.append(candidate)
        else:
            candidate = resolve_image_reset(number, now=base, relative=True)
            if candidate is not None:
                relative.append(candidate)
    retry = resolve_retry_after(retry_after, now=now)
    if absolute:
        restore, source = max(absolute), 'structured_absolute'
    elif retry is not None:
        restore, source = retry, 'retry_after'
    elif relative:
        restore, source = max(relative), 'structured_relative'
    else:
        texts = [message] if isinstance(message, str) else []
        for container in containers:
            texts.extend((_error_text(container), _text(container)))
        seconds = [value for text in texts if (value := _cooldown_seconds(text)) is not None]
        restore, source = (base + max(seconds), 'text_relative') if seconds else (None, 'unknown')
    return {'restore_at': restore, 'next_check_at': max(now, restore) if restore is not None else now + CHECK_RETRY,
            'time_source': source}


def _evidence(text, source, message, conversation_id, now, fallback_id='', *, retry_after=None):
    now = time.time() if now is None else now
    observed = conversation_timestamp(message.get('create_time') or message.get('created_at'))
    if observed > 1e12:
        observed /= 1000
    if not 0 < observed <= now + 300:
        observed = now
    message_id = message.get('id') or fallback_id
    message_id = message_id if isinstance(message_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', message_id) else ''
    cid = conversation_id if isinstance(conversation_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', conversation_id) else ''
    identity = [source, cid, message_id or hashlib.sha256(text.encode()).hexdigest()]
    event_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    timing = resolve_image_cooldown({**message, 'reason': text}, now=now, retry_after=retry_after, occurred_at=observed)
    return ImageQuotaEvidence(text.strip(), source, cid, message_id, event_id, observed, now,
                              timing['restore_at'], timing['time_source'], timing['next_check_at'])


def image_quota_evidence_from_http(error, *, now=None) -> ImageQuotaEvidence | None:
    """只接受生成端点明确的图片额度拒绝，读取端点的普通 429 不冻结生图额度。"""
    if (getattr(error, 'status_code', None) != 429 or getattr(error, 'context', '') not in {
            '/backend-api/f/conversation', '/backend-api/f/conversation/prepare'}):
        return None
    body = getattr(error, 'body', None)
    if not isinstance(body, dict):
        return None
    containers = list(_timing_containers(body))
    codes = [str(container.get(key) or '') for container in containers for key in ('code', 'type', 'error_type')]
    if any(re.search(r'policy|moderation', code, re.I) for code in codes):
        return None
    text = _error_text(body)
    explicit_code = any(code in {'image_quota_exceeded', 'image_generation_quota_exceeded',
                                'image_generation_limit_exceeded', 'image_generation_limit_reached'} for code in codes)
    if not explicit_code and not _clear_limit_text(text):
        return None
    cached = getattr(error, '_image_quota_evidence', None)
    if isinstance(cached, ImageQuotaEvidence):
        return cached
    now = time.time() if now is None else now
    reason = text if _clear_limit_text(text) else '图像生成额度已达到上游限额。'
    identity = hashlib.sha256(f'{error.context}:{now}:{id(error)}'.encode()).hexdigest()
    retry_after = getattr(error, 'retry_after', None)
    retry_at = _finite_number(getattr(error, 'retry_at', None))
    if retry_at is not None:
        # 错误跨层传播会耗时，不能在识别时重新加上原始等待秒数而推迟真实截止。
        retry_after = max(0, retry_at - now)
    evidence = _evidence(reason, 'upstream_http_image_quota', {**body, 'create_time': now}, '', now, identity,
                         retry_after=retry_after)
    error._image_quota_evidence = evidence
    return evidence


def image_throttle_evidence_from_http(error, *, now=None) -> ImageQuotaEvidence | None:
    """生成端点请求节流单独隔离，不能把普通 429 写成图片额度耗尽。"""
    if (getattr(error, 'status_code', None) != 429 or getattr(error, 'context', '') not in {
            '/backend-api/f/conversation', '/backend-api/f/conversation/prepare'}):
        return None
    if image_quota_evidence_from_http(error, now=now) is not None:
        return None
    body = getattr(error, 'body', None)
    containers = list(_timing_containers(body))
    signals = [str(container.get(key) or '') for container in containers for key in ('code', 'type', 'error_type')]
    signals.append(_error_text(body))
    excluded = (r'policy|moderation|auth|unauthori[sz]ed|'
                r'invalid[_ -](?:(?:access|refresh)[_ -])?token|token[_ -](?:expired|invalid)|'
                r'(?:session|access)[_ -](?:expired|terminated|denied)|invalid[_ -]api[_ -]key')
    if any(re.search(excluded, signal, re.I) for signal in signals):
        return None
    cached = getattr(error, '_image_throttle_evidence', None)
    if isinstance(cached, ImageQuotaEvidence):
        return cached
    now = time.time() if now is None else now
    retry_after = getattr(error, 'retry_after', None)
    retry_at = _finite_number(getattr(error, 'retry_at', None))
    if retry_at is not None:
        retry_after = max(0, retry_at - now)
    timing = resolve_image_cooldown(body, now=now, retry_after=retry_after)
    if timing['time_source'] in {'unknown', 'text_relative'}:
        # 普通节流文案不能证明账户额度周期；未知时间只安排短退避，不伪造恢复时刻。
        timing = {'restore_at': None, 'next_check_at': now + 30, 'time_source': 'unknown'}
    identity = hashlib.sha256(f'image-throttle:{error.context}:{now}:{id(error)}'.encode()).hexdigest()
    evidence = ImageQuotaEvidence('图片请求被上游暂时限流。', 'upstream_http_image_throttle', '', '',
                                  identity, now, now, timing['restore_at'], timing['time_source'], timing['next_check_at'])
    error._image_throttle_evidence = evidence
    return evidence


def image_readback_throttle_evidence_from_http(error, *, now=None) -> ImageQuotaEvidence | None:
    """原会话429只证明读取节流，记录短期冷却，不能伪造图片额度耗尽。"""
    path = getattr(error, 'context', '')
    if (getattr(error, 'status_code', None) != 429 or not isinstance(path, str)
            or not (match := re.fullmatch(r'/backend-api/conversations?/([A-Za-z0-9_-]{1,128})', path))):
        return None
    cached = getattr(error, '_image_readback_throttle_evidence', None)
    if isinstance(cached, ImageQuotaEvidence):
        return cached
    now = time.time() if now is None else now
    retry_at = _finite_number(getattr(error, 'retry_at', None))
    retry_after = max(0, retry_at - now) if retry_at is not None else getattr(error, 'retry_after', None)
    timing = resolve_image_cooldown(getattr(error, 'body', None), now=now, retry_after=retry_after)
    if timing['time_source'] in {'unknown', 'text_relative'}:
        timing = {'restore_at': None, 'next_check_at': now + 30, 'time_source': 'unknown'}
    identity = hashlib.sha256(f'image-readback:{path}:{now}:{id(error)}'.encode()).hexdigest()
    evidence = ImageQuotaEvidence('原会话读取被上游暂时限流，请稍后重试。',
        'upstream_http_conversation_throttle', match[1], '', identity, now, now,
        timing['restore_at'], timing['time_source'], timing['next_check_at'])
    error._image_readback_throttle_evidence = evidence
    return evidence


def detect_message_limit(message, conversation_id='', *, user_text='', now=None, fallback_id=''):
    if not isinstance(message, dict):
        return None
    author = message.get('author') or {}; metadata = message.get('metadata') or {}; content = message.get('content') or {}
    if not all(isinstance(item, dict) for item in (author, metadata, content)):
        return None
    if re.search(r'policy|moderation', str(metadata.get('error_type') or ''), re.I):
        return None
    role = author.get('role')
    if role == 'tool' and re.match(r'^(?:python|python_user_visible|web|browser|terminal|file_search)(?:[._]|$)', str(author.get('name') or ''), re.I):
        return None
    image_tool = role in ('tool','assistant') and (metadata.get('async_task_type') == 'image_gen'
        or role == 'tool' and re.search(r'image.?gen|text2im', str(author.get('name') or ''), re.I))
    if image_tool:
        values = [message.get('system_error'), metadata.get('system_error'), content.get('system_error')]
        for container in (message, metadata, content):
            error = container.get('error')
            if isinstance(error, dict) and (error.get('type') == 'system_error' or error.get('code') == 'system_error'):
                values.append(error)
        if content.get('content_type') == 'system_error' or metadata.get('is_error') is True:
            values.append(_text(content))
        for value in values:
            text = _error_text(value)
            if _clear_limit_text(text):
                return _evidence(text, 'image_tool_system_error', message, conversation_id, now, fallback_id)
    if (role == 'assistant' and message.get('recipient') in (None, '', 'all')
            and message.get('channel') in (None, 'final')
            and (message.get('channel') == 'final' or message.get('end_turn') is True)
            and message.get('status') in ('finished_successfully', 'finished_partial_completion')):
        text = _text(content)
        if _clear_limit_text(text, user_text):
            return _evidence(text, 'assistant_final', message, conversation_id, now, fallback_id)
    return None


def detect_task_limit(task, conversation_id='', *, user_text='', now=None):
    if not isinstance(task, dict):
        return None
    candidates = {value for value in (task.get('conversation_id'),task.get('original_conversation_id')) if isinstance(value,str) and value}
    if conversation_id and candidates and conversation_id not in candidates:
        return None
    cid = conversation_id or task.get('original_conversation_id') or task.get('conversation_id') or ''
    message = task.get('image_gen_message'); metadata = message.get('metadata') if isinstance(message, dict) else None
    if not isinstance(metadata, dict) or metadata.get('is_error') is not True:
        return None
    if (message.get('author') or {}).get('role') not in ('tool','assistant'):
        return None
    if re.search(r'policy|moderation', str(metadata.get('error_type') or ''), re.I):
        return None
    text = _text(message.get('content'))
    if not _clear_limit_text(text):
        return None
    message = {**message, 'create_time':message.get('create_time') or task.get('create_time') or task.get('created_at')}
    return _evidence(text, 'image_tool_system_error', message, cid, now, str(task.get('task_id') or ''))


def detect_document_limit(document, conversation_id='', *, user_text='', submitted_id='', now=None):
    if not isinstance(document, dict):
        return None
    mapping = document.get('mapping'); current = document.get('current_node')
    if isinstance(mapping, dict) and 'current_node' in document and (not isinstance(current, str) or current not in mapping):
        return None
    if isinstance(mapping, dict) and isinstance(current, str) and current in mapping:
        messages = []; seen = set()
        while isinstance(current, str) and current in mapping and current not in seen and len(seen) < 10000:
            seen.add(current); node = mapping[current]
            if not isinstance(node, dict):break
            if isinstance(node.get('message'), dict):messages.append(node['message'])
            current = node.get('parent')
        if current is not None:
            return None
        messages.reverse()
    else:
        messages = [message for _,message in iter_conversation_messages(document)]
    start = 0
    for index,message in enumerate(messages):
        if (message.get('author') or {}).get('role') == 'user':
            start = index+1
            user_text = _text(message.get('content')) or user_text
        if submitted_id and message.get('id') == submitted_id:
            start = index+1
    for message in messages[start:]:
        evidence = detect_message_limit(message, conversation_id, user_text=user_text, now=now, fallback_id=submitted_id)
        if evidence is not None:return evidence
    return None


def raise_image_quota(backend, evidence):
    from services.account_service import account_service
    token = getattr(backend, 'access_token', '')
    record = account_service.mark_image_quota_limit(token, evidence) if isinstance(token, str) else evidence.record()
    backend.image_conversation_id = evidence.conversation_id
    raise ImageQuotaExceeded(record)
