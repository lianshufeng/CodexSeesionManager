from __future__ import annotations

import json
from collections import deque


class ResponseContentCounter:
    """只统计内容字段；无需客户端请求，增量和最终完整内容共用计数状态。"""

    _fields = {"output_text": "text", "refusal": "refusal", "function_call_arguments": "arguments",
               "custom_tool_call_input": "input", "reasoning_text": "text", "reasoning_summary_text": "text"}

    def __init__(self, encoding, sample) -> None:
        self._encoding = encoding
        self._sample = sample
        self._responses = {}
        self._finished = deque(maxlen=16)
        self._current = "anonymous"

    def _identity(self, state, event, item=None):
        item = item or {}
        aliases = []
        if "output_index" in event:
            aliases.append(("index", event["output_index"]))
        for name, value in (("item", event.get("item_id")), ("item", item.get("id")),
                            ("call", event.get("call_id")), ("call", item.get("call_id"))):
            if value:
                aliases.append((name, value))
        identity = next((state["aliases"][a] for a in aliases if a in state["aliases"]), aliases[0] if aliases else ("index", 0))
        for alias in aliases:
            if len(state["aliases"]) < 512:
                state["aliases"][alias] = identity
        return identity

    def _part(self, state, key, text, now, complete=False):
        if not isinstance(text, str) or not text:
            return
        previous = state["parts"].get(key)
        if previous is None:
            previous = state["retired"].pop(key, None)
            if len(state["parts"]) >= 128:
                oldest = next(iter(state["parts"]))
                saved = state["parts"].pop(oldest)
                if len(state["retired"]) >= 512:
                    state["retired"].pop(next(iter(state["retired"])))
                # 旧片段只保留计数用于全文去重，释放文本尾部；新片段继续统计。
                state["retired"][oldest] = {"tail": "", "tail_count": 0, "total": saved["total"], "done": saved["done"]}
            previous = previous or {"tail": "", "tail_count": 0, "total": 0, "done": False}
        if previous["done"]:
            return
        encoding = self._encoding()
        if complete:
            total = len(encoding.encode(text, disallowed_special=()))
            change = total - previous["total"]
            previous = {"tail": "", "tail_count": 0, "total": total, "done": True}
        else:
            joined = previous["tail"] + text
            change = len(encoding.encode(joined, disallowed_special=())) - previous["tail_count"]
            tail = joined[-128:]
            previous = {"tail": tail, "tail_count": len(encoding.encode(tail, disallowed_special=())),
                        "total": previous["total"] + change, "done": False}
        state["parts"][key] = previous
        if change:
            self._sample(change, now)

    def _item(self, state, event, item, now):
        if not isinstance(item, dict):
            return
        identity = self._identity(state, event, item)
        kind = item.get("type")
        if kind in ("message", "reasoning"):
            for field in ("content", "summary"):
                for index, content in enumerate(item.get(field) or []):
                    if not isinstance(content, dict):
                        continue
                    name = content.get("type")
                    name = "reasoning_summary_text" if name == "summary_text" else name
                    if name in self._fields:
                        self._part(state, (name, identity, index), content.get(self._fields[name]), now, True)
        elif kind in ("function_call", "custom_tool_call"):
            name = "function_call_arguments" if kind == "function_call" else "custom_tool_call_input"
            self._part(state, (name, identity, 0), item.get(self._fields[name]), now, True)
        elif kind == "local_shell_call" and isinstance(item.get("action"), dict):
            self._part(state, (kind, identity, 0), json.dumps(item["action"], ensure_ascii=False), now, True)

    def observe(self, event, now):
        if not isinstance(event, dict):
            return
        # 云端通知可能在 data/msg 中封装 Codex 内容事件。
        for _ in range(4):
            nested = next((event.get(key) for key in ("msg", "data") if isinstance(event.get(key), dict)
                           and any(field in event[key] for field in ("type", "method", "msg"))), None)
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
            if method in methods:
                self.observe({**common, "type": "response." + methods[method] + ".delta", "delta": params.get("delta")}, now)
            elif method == "item/completed" and isinstance(params.get("item"), dict):
                item = params["item"]
                if item.get("type") in ("agentMessage", "plan"):
                    name = "output_text" if item["type"] == "agentMessage" else "plan_text"
                    self.observe({**common, "item_id": item.get("id"), "type": "response." + name + ".done", "text": item.get("text")}, now)
                elif item.get("type") == "reasoning":
                    for field, name in (("summary", "reasoning_summary_text"), ("content", "reasoning_text")):
                        for index, text in enumerate(item.get(field) or []):
                            self.observe({**common, "item_id": item.get("id"), "summary_index": index,
                                          "type": "response." + name + ".done", "text": text}, now)
            return
        aliases = {"agent_message_delta": "output_text", "agent_message_content_delta": "output_text",
                   "agent_reasoning_delta": "reasoning_summary_text", "agent_reasoning_raw_content_delta": "reasoning_text"}
        if event.get("type") in aliases:
            event = {**event, "type": "response." + aliases[event["type"]] + ".delta"}
        response = event.get("response") or event
        if not isinstance(response, dict):
            return
        response_id = event.get("response_id") or response.get("id")
        if response_id:
            if response_id not in self._responses and "anonymous" in self._responses and len(self._responses) == 1:
                self._responses[response_id] = self._responses.pop("anonymous")
            self._current = response_id
        response_id = response_id or self._current
        if response_id in self._finished:
            return
        if response_id not in self._responses:
            if len(self._responses) >= 8:
                self._responses.pop(next(iter(self._responses)))
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
        if event_type == "response.output_item.added":
            self._identity(state, event, event.get("item"))
        elif event_type == "response.output_item.done":
            self._item(state, event, event.get("item"), now)
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
            for tool in delta.get("tool_calls") or []:
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict):
                    self._part(state, ("tool", choice_id, tool.get("index", 0)), tool["function"].get("arguments"), now, complete)
        final = event_type in ("response.completed", "response.incomplete", "response.failed") or response.get("object") == "response"
        if final:
            for output_index, item in enumerate(response.get("output") or []):
                self._item(state, {"output_index": output_index}, item, now)
            if event_type in ("response.completed", "response.incomplete", "response.failed") or response.get("status") in ("completed", "incomplete", "failed"):
                self._responses.pop(response_id, None)
                self._finished.append(response_id)
