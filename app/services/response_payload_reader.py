from __future__ import annotations

import json
import re
from tempfile import SpooledTemporaryFile


class PayloadLimitError(ValueError):
    pass


class PayloadReader:
    """从溢写文件逐块解析 JSON；图片和加密产物不会进入内存或分词。"""

    _ignored = {"result", "encrypted_content", "encrypted_function_args", "b64_json",
                "image_url", "audio", "logprobs"}
    _string_boundary = re.compile(r'["\\\x00-\x1f]')

    def __init__(self, source):
        self.source = source
        self.buffer = ""
        self.offset = 0
        self.issues = []

    def peek(self):
        if self.offset == len(self.buffer):
            self.buffer = self.source.read(65536)
            self.offset = 0
        return self.buffer[self.offset:self.offset + 1]

    def take(self):
        value = self.peek()
        self.offset += bool(value)
        return value

    def space(self):
        while self.peek() and self.peek().isspace():
            self.take()

    def string(self, keep):
        if self.take() != '"':
            raise ValueError("JSON 字符串缺少引号")
        parts = []
        size = 0
        escaped = False
        while True:
            # 按块查找引号/转义，避免逐字符处理大型图片数据。
            if not self.peek():
                raise ValueError("JSON 字符串未结束")
            start = self.offset
            while self.offset < len(self.buffer):
                if escaped:
                    self.offset += 1
                    escaped = False
                    continue
                match = self._string_boundary.search(self.buffer, self.offset)
                if match is None:
                    self.offset = len(self.buffer)
                    break
                char = match.group()
                self.offset = match.end()
                if char == '"':
                    fragment = self.buffer[start:self.offset - 1]
                    if keep:
                        if size + len(fragment) > 16 * 1024 * 1024:
                            self.issues.append("统计文本字段超过 16 MiB，已跳过估算并保留官方用量")
                            return None
                        parts.append(fragment)
                        return json.loads('"' + "".join(parts) + '"')
                    return None
                if ord(char) < 32:
                    raise ValueError("JSON 字符串含控制字符")
                escaped = char == "\\"
            if keep:
                fragment = self.buffer[start:self.offset]
                size += len(fragment)
                if size > 16 * 1024 * 1024:
                    self.issues.append("统计文本字段超过 16 MiB，已跳过估算并保留官方用量")
                    keep = False
                    parts.clear()
                else:
                    parts.append(fragment)

    def value(self, keep=True, depth=0):
        if depth > 64:
            raise PayloadLimitError("JSON 嵌套超过 64 层")
        self.space()
        char = self.peek()
        if char == '"':
            return self.string(keep)
        if char in ("{", "["):
            object_value = char == "{"
            self.take()
            end = "}" if object_value else "]"
            result = {} if object_value else []
            self.space()
            if self.peek() == end:
                self.take()
                return result if keep else None
            count = 0
            while True:
                self.space()
                key = self.string(True) if object_value else None
                if object_value:
                    self.space()
                    if self.take() != ":":
                        raise ValueError("JSON 缺少冒号")
                self.space()
                retain = keep and (key not in self._ignored or (key == "result" and self.peek() != '"'))
                value = self.value(retain, depth + 1)
                if retain:
                    if object_value:
                        result[key] = value
                    else:
                        result.append(value)
                count += 1
                if keep and count > 100000:
                    raise PayloadLimitError("JSON 条目过多")
                self.space()
                separator = self.take()
                if separator == end:
                    return result if keep else None
                if separator != ",":
                    raise ValueError("JSON 分隔符错误")
        literal = ""
        while self.peek() and self.peek() not in ",]} \r\n\t":
            literal += self.take()
            if len(literal) > 128:
                raise ValueError("JSON 字面值过长")
        return json.loads(literal) if keep else self._discard_literal(literal)

    @staticmethod
    def _discard_literal(literal):
        json.loads(literal)
        return None

    def read(self):
        result = self.value()
        self.space()
        if self.peek():
            raise ValueError("JSON 后有多余内容")
        return result


class ResponsePayloadBuffer:
    """JSON/SSE 帧缓冲；512 KiB 后溢写临时文件，单帧最多 256 MiB。"""

    def __init__(self, dispatch, incomplete, sse=False):
        self.dispatch = dispatch
        self.incomplete = incomplete
        self.sse = True if sse else None
        self.probe = ""
        self.file = None
        self.size = 0
        self.skipped = False
        self.line_head = ""
        self.line_data = False
        self.line_started = False
        self.line_size = 0
        self.data_lines = 0

    def append(self, text):
        if self.skipped or not text:
            return
        self.size += len(text.encode("utf-8"))
        if self.size > 256 * 1024 * 1024:
            self.incomplete("消息超过 256 MiB，统计不完整")
            self.skipped = True
            self.close()
            return
        if self.file is None:
            self.file = SpooledTemporaryFile(max_size=512 * 1024, mode="w+", encoding="utf-8", newline="")
        self.file.write(text)

    def emit(self, now):
        if self.file is not None and not self.skipped:
            self.file.seek(0)
            prefix = self.file.read(16).strip()
            self.file.seek(0)
            if prefix and prefix != "[DONE]":
                self.dispatch(self.file, now)
        self.close()
        self.size = 0
        self.skipped = False
        self.data_lines = 0

    def feed(self, text, now):
        if self.sse is None:
            self.probe += text
            start = self.probe.lstrip()
            if not start and len(self.probe) > 65536:
                self.probe = ""
                return
            if not start or (len(start) < 6 and not start.startswith(("{", "["))):
                return
            self.sse = start.startswith(("data:", "event:", ":", "id:", "retry:"))
            text, self.probe = self.probe, ""
        if not self.sse:
            self.append(text)
            return
        fragments = text.split("\n")
        for index, fragment in enumerate(fragments):
            ended = index < len(fragments) - 1
            if ended:
                fragment = fragment.rstrip("\r")
            self.line_size += len(fragment)
            if not self.line_started:
                self.line_head += fragment
                if ":" in self.line_head:
                    field, value = self.line_head.split(":", 1)
                    self.line_data = field == "data"
                    self.line_started = True
                    self.line_head = ""
                    if self.line_data:
                        if self.data_lines:
                            self.append("\n")
                        self.data_lines += 1
                        self.append(value[1:] if value.startswith(" ") else value)
                elif len(self.line_head) > 16:
                    self.line_started = True
                    self.line_head = ""
            elif self.line_data:
                self.append(fragment)
            if ended:
                if self.line_size == 0 or (self.line_size == 1 and self.line_head == "\r"):
                    self.emit(now)
                self.line_head = ""
                self.line_data = False
                self.line_started = False
                self.line_size = 0

    def end(self, now):
        if self.probe:
            self.append(self.probe)
            self.probe = ""
        self.emit(now)

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None
