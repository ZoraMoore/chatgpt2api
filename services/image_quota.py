"""只解析明确的图像限额证据，不把用户引用、网络或内容政策错误当成额度不足。"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from services.protocol.conversation_document import conversation_timestamp, iter_conversation_messages

ACTIVE_STATES = {'cooldown', 'awaiting_check', 'probe_ready', 'probing'}
DEFAULT_COOLDOWN = 3600
CHECK_RETRY = 300


@dataclass(frozen=True)
class ImageQuotaEvidence:
    reason: str
    source: str
    conversation_id: str
    message_id: str
    event_id: str
    occurred_at: float
    observed_at: float
    restore_at: float

    def record(self):
        return asdict(self)


class ImageQuotaExceeded(RuntimeError):
    def __init__(self, record):
        self.record = dict(record)
        self.conversation_id = str(record.get('conversation_id') or '')
        self.restore_at = float(record['restore_at'])
        deadline = datetime.fromtimestamp(self.restore_at, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
        super().__init__(str(record['reason']) + '\n预计恢复时间：' + deadline + '（北京时间，到期后仍需验证额度恢复）。')


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
    try:
        return float(raw)
    except ValueError:
        digits = {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
        if '十' in raw:
            head, tail = raw.split('十', 1)
            return digits.get(head, 1)*10 + digits.get(tail, 0)
        return digits.get(raw, 0)


def _cooldown_seconds(text):
    if not re.search(r'重置|恢复|再试|再次|等待|reset|try again|create more|please wait', text, re.I):
        return DEFAULT_COOLDOWN
    seconds = 0
    for number, unit in re.findall(r'(\d+(?:\.\d+)?|[一二两三四五六七八九十]+)\s*(小时|分钟|秒钟?|天|hours?|minutes?|seconds?|days?)', text, re.I):
        factor = 86400 if unit in ('天','day','days') else 3600 if unit.startswith(('小时','hour')) else 60 if unit.startswith(('分钟','minute')) else 1
        seconds += _number(number)*factor
    return seconds if 0 < seconds <= 30*86400 else DEFAULT_COOLDOWN


def _evidence(text, source, message, conversation_id, now, fallback_id=''):
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
    restore = observed+_cooldown_seconds(text)
    metadata = message.get('metadata') if isinstance(message.get('metadata'), dict) else {}
    for container in (message, metadata):
        for key in ('reset_at','restore_at','retry_at'):
            candidate = conversation_timestamp(container.get(key))
            if candidate > 1e12:candidate /= 1000
            if observed < candidate <= observed+30*86400:
                restore = candidate
                return ImageQuotaEvidence(text.strip(), source, cid, message_id, event_id, observed, now, restore)
    return ImageQuotaEvidence(text.strip(), source, cid, message_id, event_id, observed, now, restore)


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
