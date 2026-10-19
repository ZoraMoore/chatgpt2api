"""合并网页语音的 v1 消息增量；不执行消息中的代码。"""
from __future__ import annotations

import copy
import json
import re


class DeltaDecoder:
    def __init__(self):
        self.values: dict[int, object] = {}
        self.previous = {"c": 0, "p": "", "o": "add"}
        self.sizes: dict[int, int] = {}

    def apply(self, delta: object):
        if not isinstance(delta, dict):
            raise ValueError("invalid_delta")
        expanded = {**self.previous, **delta}
        channel = expanded.get("c")
        if type(channel) is not int or not 0 <= channel < 32:
            raise ValueError("invalid_delta_channel")
        root = {"root": copy.deepcopy(self.values.get(channel))}
        self._apply(root, expanded, 0)
        size = len(json.dumps(root, ensure_ascii=False).encode("utf-8"))
        if size > 2 * 1024 * 1024 or sum(self.sizes.values()) - self.sizes.get(channel, 0) + size > 16 * 1024 * 1024:
            raise ValueError("delta_state_too_large")
        self.values[channel] = root["root"]
        self.sizes[channel] = size
        self.previous = {key: expanded[key] for key in ("c", "p", "o")}
        return root["root"]

    def _apply(self, root: dict, delta: dict, depth: int):
        path, operation = delta.get("p", delta.get("path", "")), delta.get("o", delta.get("op"))
        value = delta.get("v", delta.get("value"))
        if depth > 16 or not isinstance(path, str) or len(path) > 2048:
            raise ValueError("invalid_delta_path")
        parts = [] if not path else path.removeprefix("/").split("/")
        if len(parts) > 64:
            raise ValueError("invalid_delta_path")
        parts = [int(p) if re.fullmatch(r"0|[1-9]\d*", p) else p.replace("~1", "/").replace("~0", "~") for p in parts]
        if any(isinstance(p, int) and p > 4096 or p in {"__proto__", "constructor", "prototype"} for p in parts):
            raise ValueError("invalid_delta_path")
        path_parts = ["root", *parts]
        target = root
        for index, key in enumerate(path_parts[:-1]):
            next_key = path_parts[index + 1]
            if isinstance(target, list):
                if not isinstance(key, int):
                    raise ValueError("invalid_delta_index")
                if key >= len(target):
                    target.extend([None] * (key + 1 - len(target)))
                if target[key] is None:
                    target[key] = [] if isinstance(next_key, int) else {}
            elif isinstance(target, dict):
                if target.get(key) is None:
                    target[key] = [] if isinstance(next_key, int) else {}
            else:
                raise ValueError("invalid_delta_parent")
            target = target[key]
        key = path_parts[-1]
        if isinstance(target, list):
            if not isinstance(key, int) or key > len(target):
                raise ValueError("invalid_delta_index")
            old = target[key] if key < len(target) else None
        elif isinstance(target, dict):
            old = target.get(key)
        else:
            raise ValueError("invalid_delta_parent")
        if operation == "patch":
            if not isinstance(value, list) or len(value) > 256:
                raise ValueError("invalid_delta_patch")
            nested = {"root": old}
            for child in value:
                if not isinstance(child, dict):
                    raise ValueError("invalid_delta_patch")
                self._apply(nested, child, depth + 1)
            value = nested["root"]
        elif operation == "remove":
            if isinstance(target, list):
                if key < len(target):
                    target.pop(key)
            else:
                target.pop(key, None)
            return
        elif operation == "append":
            if isinstance(old, str) and isinstance(value, str):
                value = old + value
            elif isinstance(old, list):
                value = old + (value if isinstance(value, list) else [value])
            elif isinstance(old, dict) and isinstance(value, dict):
                value = {**old, **value}
        elif operation == "truncate":
            if type(value) is not int or not 0 <= value <= 2 * 1024 * 1024:
                raise ValueError("invalid_delta_truncate")
            value = old[:value] if isinstance(old, (list, str)) else old
        elif operation not in {"add", "replace"}:
            raise ValueError("invalid_delta_operation")
        if isinstance(target, list):
            if operation == "add":
                target.insert(key, value)
            elif key == len(target):
                target.append(value)
            else:
                target[key] = value
        else:
            target[key] = value
