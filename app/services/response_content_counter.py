from __future__ import annotations

import json
from collections import deque
from enum import StrEnum


_RAW_RESPONSE_COMPLETED_METHOD = 'rawResponse/completed'
_RAW_RESPONSE_ITEM_COMPLETED_METHOD = 'rawResponseItem/completed'
_TURN_COMPLETED_METHOD = 'turn/completed'
_ITEM_COMPLETED_METHOD = 'item/completed'
_RAW_RESPONSE_COMPLETED_EVENT = 'raw_response_completed'
_RAW_RESPONSE_ITEM_EVENT = 'raw_response_item'
_AGENT_MESSAGE_EVENT = 'agent_message'
_AUDIO_TRANSCRIPT_DELTA_EVENT = 'response.output_audio_transcript.delta'
_AUDIO_TRANSCRIPT_DONE_EVENT = 'response.output_audio_transcript.done'
_OUTPUT_ITEM_ADDED_EVENT = 'response.output_item.added'
_OUTPUT_ITEM_DONE_EVENT = 'response.output_item.done'
_CONTENT_PART_DONE_EVENT = 'response.content_part.done'
_RESPONSE_COMPLETED_EVENT = 'response.completed'
_RESPONSE_INCOMPLETE_EVENT = 'response.incomplete'
_RESPONSE_FAILED_EVENT = 'response.failed'
_RESPONSE_DONE_EVENT = 'response.done'
_ANONYMOUS_RESPONSE = 'anonymous'


class ResponseItemType(StrEnum):
    MESSAGE = "message"
    REASONING = "reasoning"
    FUNCTION_CALL = "function_call"
    CUSTOM_TOOL_CALL = "custom_tool_call"
    LOCAL_SHELL_CALL = "local_shell_call"
    TOOL_SEARCH_CALL = "tool_search_call"
    WEB_SEARCH_CALL = "web_search_call"
    AGENT_MESSAGE = "agentMessage"
    PLAN = "plan"


class ResponseStatus(StrEnum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class ResponseContentCounter:
    """只统计内容字段；无需客户端请求，增量和最终完整内容共用计数状态。"""

    _fields = {"output_text": "text", "refusal": "refusal", "function_call_arguments": "arguments",
               "custom_tool_call_input": "input", "reasoning_text": "text", "reasoning_summary_text": "text"}

    def __init__(self, encoding, sample, ledger=None, scope=_ANONYMOUS_RESPONSE, incomplete=None) -> None:
        self._encoding = encoding
        self._sample = sample
        self._responses = {}
        self._finished = deque(maxlen=16)
        self._current = _ANONYMOUS_RESPONSE
        self._ledger = ledger
        self._scope = scope
        self._incomplete = incomplete or (lambda reason: None)
        self._generation = 0

    def _response_key(self):
        if self._current == _ANONYMOUS_RESPONSE:
            return f"{self._scope}:anonymous:{self._generation}"
        return str(self._current)

    def _tokens(self, text):
        encoding = self._encoding()
        if len(text) <= 32768:
            return len(encoding.encode(text, disallowed_special=()))
        # 限制单次分词长度，避免超长无空格文本的 BPE 耗时失控。
        total, tail, tail_count = 0, "", 0
        for offset in range(0, len(text), 8192):
            joined = tail + text[offset:offset + 8192]
            total += len(encoding.encode(joined, disallowed_special=())) - tail_count
            tail = joined[-128:]
            tail_count = len(encoding.encode(tail, disallowed_special=()))
        return total

    def _identity(self, state, event, item=None):
        item = item or {}
        aliases = []
        if "output_index" in event:
            aliases.append(("index", event["output_index"]))
        for name, value in (("item", event.get("item_id")), ("item", item.get("id")),
                            ("call", event.get("call_id")), ("call", item.get("call_id"))):
            if value:
                aliases.append((name, value))
        identity = next((state["aliases"][a] for a in aliases if a in state["aliases"]),
                        next((a for a in aliases if a[0] in ("item", "call")), aliases[0] if aliases else ("index", 0)))
        for alias in aliases:
            state["aliases"][alias] = identity
        if len(state["aliases"]) > 32768:
            self._incomplete("响应条目身份过多，统计不完整")
            state["aliases"].clear()
        return identity

    def _part(self, state, key, text, now, complete=False):
        if not isinstance(text, str) or not text:
            return
        previous = state["parts"].get(key)
        recorded = self._ledger.part(self._response_key(), key) if self._ledger else (0, False, "", 0)
        if recorded[1]:
            return
        if previous is None:
            previous = state["retired"].pop(key, None)
            if len(state["parts"]) >= 128:
                oldest = next(iter(state["parts"]))
                saved = state["parts"].pop(oldest)
                if len(state["retired"]) >= 512:
                    state["retired"].pop(next(iter(state["retired"])))
                # 旧片段只保留计数用于全文去重，释放文本尾部；新片段继续统计。
                state["retired"][oldest] = {"tail": "", "tail_count": 0, "total": saved["total"], "done": saved["done"]}
            if self._ledger and recorded[0]:
                previous = {"tail": recorded[2], "tail_count": recorded[3], "total": recorded[0], "done": False}
            previous = previous or {"tail": "", "tail_count": 0, "total": recorded[0], "done": False}
        if previous["done"]:
            return
        if complete:
            total = self._tokens(text)
            change = total - previous["total"]
            previous = {"tail": "", "tail_count": 0, "total": total, "done": True}
        else:
            joined = previous["tail"] + text
            change = self._tokens(joined) - previous["tail_count"]
            tail = joined[-128:]
            previous = {"tail": tail, "tail_count": self._tokens(tail),
                        "total": previous["total"] + change, "done": False}
        state["parts"][key] = previous
        if self._ledger:
            change = self._ledger.set_part(self._response_key(), key, previous["total"], previous["done"], now,
                                           previous["tail"], previous["tail_count"])
        if change:
            self._sample(change, now)

    def _item(self, state, event, item, now):
        if not isinstance(item, dict):
            return
        identity = self._identity(state, event, item)
        kind = item.get("type")
        if kind in (ResponseItemType.MESSAGE, ResponseItemType.REASONING):
            for field in ("content", "summary"):
                for index, content in enumerate(item.get(field) or []):
                    if not isinstance(content, dict):
                        continue
                    name = content.get("type")
                    name = "reasoning_summary_text" if name == "summary_text" else name
                    if kind == ResponseItemType.REASONING and name == "text":
                        name = "reasoning_text"
                    if name in self._fields:
                        self._part(state, (name, identity, index), content.get(self._fields[name]), now, True)
        elif kind in (ResponseItemType.FUNCTION_CALL, ResponseItemType.CUSTOM_TOOL_CALL):
            name = "function_call_arguments" if kind == ResponseItemType.FUNCTION_CALL else "custom_tool_call_input"
            self._part(state, (name, identity, 0), item.get(self._fields[name]), now, True)
        elif kind == ResponseItemType.LOCAL_SHELL_CALL and isinstance(item.get("action"), dict):
            self._part(state, (kind, identity, 0), json.dumps(item["action"], ensure_ascii=False), now, True)
        elif kind in (ResponseItemType.TOOL_SEARCH_CALL, ResponseItemType.WEB_SEARCH_CALL):
            value = item.get("arguments" if kind == ResponseItemType.TOOL_SEARCH_CALL else "action")
            if value is not None:
                self._part(state, (kind, identity, 0), json.dumps(value, ensure_ascii=False), now, True)

    def observe(self, event, now):
        if isinstance(event, list):
            for message in event:
                self.observe(message, now)
            return
        if not isinstance(event, dict):
            return
        # 云端通知可能在 data/msg 中封装 Codex 内容事件。
        for _ in range(4):
            nested = next((event.get(key) for key in ("msg", "data", "result") if isinstance(event.get(key), dict)
                           and any(field in event[key] for field in ("type", "method", "msg", "object", "choices"))), None)
            if nested is None:
                break
            event = nested
        method = event.get("method")
        params = event.get("params")
        if isinstance(method, str) and isinstance(params, dict):
            if isinstance(params.get("msg"), dict):
                self.observe(params["msg"], now)
                return
            methods = {"item/agentMessage/delta": "output_text", "item/plan/delta": "plan_text",
                       "item/reasoning/summaryTextDelta": "reasoning_summary_text", "item/reasoning/textDelta": "reasoning_text"}
            response_id = str(params.get("threadId", "")) + ":" + str(params.get("turnId", ""))
            common = {"response_id": response_id, "item_id": params.get("itemId"),
                      "summary_index": params.get("summaryIndex", params.get("contentIndex", 0))}
            if method == _RAW_RESPONSE_COMPLETED_METHOD:
                identity = params.get("responseId")
                if identity and self._ledger:
                    accepted = self._ledger.usage(identity, params.get("tokenUsage"), now)
                    if params.get("threadId") and params.get("turnId") and accepted:
                        self._ledger.cover_estimate(response_id, now)
                return
            if method == _RAW_RESPONSE_ITEM_COMPLETED_METHOD:
                self.observe({**common, "type": _OUTPUT_ITEM_DONE_EVENT, "item": params.get("item")}, now)
                return
            if method == _TURN_COMPLETED_METHOD:
                self._responses.pop(response_id, None)
                return
            if method in methods:
                self.observe({**common, "type": "response." + methods[method] + ".delta", "delta": params.get("delta")}, now)
            elif method == _ITEM_COMPLETED_METHOD and isinstance(params.get("item"), dict):
                item = params["item"]
                if item.get("type") in (ResponseItemType.AGENT_MESSAGE, ResponseItemType.PLAN):
                    name = "output_text" if item["type"] == ResponseItemType.AGENT_MESSAGE else "plan_text"
                    self.observe({**common, "item_id": item.get("id"), "type": "response." + name + ".done", "text": item.get("text")}, now)
                elif item.get("type") == ResponseItemType.REASONING:
                    for field, name in (("summary", "reasoning_summary_text"), ("content", "reasoning_text")):
                        for index, text in enumerate(item.get(field) or []):
                            self.observe({**common, "item_id": item.get("id"), "summary_index": index,
                                          "type": "response." + name + ".done", "text": text}, now)
            return
        aliases = {"agent_message_delta": "output_text", "agent_message_content_delta": "output_text",
                   "agent_reasoning_delta": "reasoning_summary_text", "agent_reasoning_raw_content_delta": "reasoning_text",
                   "reasoning_content_delta": "reasoning_summary_text", "reasoning_raw_content_delta": "reasoning_text",
                   "plan_delta": "plan_text"}
        if event.get("type") in aliases:
            identity = (str(event.get("thread_id", "")) + ":" + str(event.get("turn_id", ""))) if event.get("thread_id") else None
            event = {**event, "type": "response." + aliases[event["type"]] + ".delta",
                     "response_id": identity or event.get("response_id")}
        if event.get("type") == _RAW_RESPONSE_COMPLETED_EVENT:
            if event.get("response_id") and self._ledger:
                self._ledger.usage(event["response_id"], event.get("token_usage"), now)
            return
        if event.get("type") == _RAW_RESPONSE_ITEM_EVENT:
            event = {**event, "type": _OUTPUT_ITEM_DONE_EVENT}
        if event.get("type") == _AGENT_MESSAGE_EVENT and isinstance(event.get("message"), str):
            event = {**event, "type": "response.output_text.done", "text": event["message"]}
        if event.get("type") in (_AUDIO_TRANSCRIPT_DELTA_EVENT, _AUDIO_TRANSCRIPT_DONE_EVENT):
            event = {**event, "type": event["type"].replace("output_audio_transcript", "output_text"),
                     "text": event.get("transcript")}
        response = event.get("response") or event
        if not isinstance(response, dict):
            return
        response_id = event.get("response_id") or (response.get("id") if "response" in event or "choices" in event or response.get("object") in ("response", "chat.completion", "chat.completion.chunk") else None)
        if response_id is None:
            identifiers = [("item", event.get("item_id")), ("call", event.get("call_id"))]
            candidates = [identity for identity, state in self._responses.items()
                          if any(value and (name, value) in state["aliases"] for name, value in identifiers)]
            if len(candidates) == 1:
                response_id = candidates[0]
            elif len(candidates) > 1:
                self._incomplete("交错响应条目身份不明确，统计不完整")
                return
        if response_id is not None and (not isinstance(response_id, str) or len(response_id) > 512):
            self._incomplete("响应 ID 无效，统计不完整")
            return
        if response_id:
            if response_id not in self._responses and _ANONYMOUS_RESPONSE in self._responses and len(self._responses) == 1:
                if self._ledger:
                    self._ledger.rename(self._response_key(), response_id)
                self._responses[response_id] = self._responses.pop(_ANONYMOUS_RESPONSE)
            self._current = response_id
        response_id = response_id or self._current
        if self._ledger:
            self._ledger.usage(self._response_key(), response.get("usage"), now)
        if response_id in self._finished or (self._ledger and self._ledger.finished(self._response_key())):
            self._current = next(reversed(self._responses), _ANONYMOUS_RESPONSE)
            return
        if response_id not in self._responses:
            if len(self._responses) >= 128:
                self._responses.pop(next(iter(self._responses)))
                self._incomplete("并发响应状态已淘汰，统计不完整")
            self._responses[response_id] = {"parts": {}, "retired": {}, "aliases": {}}
        state = self._responses[response_id]
        event_type = event.get("type", "")
        name, _, suffix = event_type.rpartition(".")
        name = name.removeprefix("response.")
        index = event.get("summary_index", event.get("content_index", 0))
        if suffix in ("delta", "done"):
            if name in self._fields:
                identity = self._identity(state, event)
                field = "delta" if suffix == "delta" else self._fields[name]
                self._part(state, (name, identity, index), event.get(field), now, suffix == "done")
            elif isinstance(event.get("delta"), str) and "audio" not in name:
                identity = self._identity(state, event)
                # 新增文本增量事件可立即统计，避免依赖固定事件白名单。
                self._part(state, (name, identity, index), event["delta"], now)
            elif suffix == "done" and isinstance(event.get("text"), str) and "audio" not in name:
                self._part(state, (name, self._identity(state, event), index), event["text"], now, True)
        if event_type == _OUTPUT_ITEM_ADDED_EVENT:
            self._identity(state, event, event.get("item"))
        elif event_type == _OUTPUT_ITEM_DONE_EVENT:
            self._item(state, event, event.get("item"), now)
        elif event_type == _CONTENT_PART_DONE_EVENT and isinstance(event.get("part"), dict):
            part = event["part"]
            name = part.get("type")
            if name in self._fields:
                self._part(state, (name, self._identity(state, event), index), part.get(self._fields[name]), now, True)
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, dict):
                continue
            complete = "message" in choice
            choice_id = choice.get("index", 0)
            for field in ("content", "reasoning_content", "reasoning"):
                self._part(state, (field, choice_id, 0), delta.get(field), now, complete)
            for tool_index, tool in enumerate(delta.get("tool_calls") or []):
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict):
                    self._part(state, ("tool", choice_id, tool.get("index", tool_index)), tool["function"].get("arguments"), now, complete)
        chat_final = bool(event.get("choices")) and all(isinstance(choice, dict) and ("message" in choice or choice.get("finish_reason") is not None) for choice in event["choices"])
        terminal = event_type in (_RESPONSE_COMPLETED_EVENT, _RESPONSE_INCOMPLETE_EVENT, _RESPONSE_FAILED_EVENT, _RESPONSE_DONE_EVENT) or chat_final
        final = terminal or response.get("object") == "response"
        if final:
            for output_index, item in enumerate(response.get("output") or []):
                self._item(state, {"output_index": output_index}, item, now)
            if terminal or response.get("status") in (ResponseStatus.COMPLETED, ResponseStatus.INCOMPLETE, ResponseStatus.FAILED):
                self._responses.pop(response_id, None)
                if self._ledger and response_id != _ANONYMOUS_RESPONSE:
                    self._ledger.finish(self._response_key())
                if response_id == _ANONYMOUS_RESPONSE:
                    self._generation += 1
                else:
                    self._finished.append(response_id)
                    self._current = next(reversed(self._responses), _ANONYMOUS_RESPONSE)
                    self._generation += 1
