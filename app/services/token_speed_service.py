from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from urllib.request import urlopen
import os
import sys

import tiktoken


def prepare_tokenizer_cache(cache: Path) -> None:
    """构建时预存数据；源码运行首次加载也使用系统信任链下载。"""
    hashes = {"cl100k_base": "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
              "o200k_base": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"}
    cache.mkdir(parents=True, exist_ok=True)
    for name, expected in hashes.items():
        url = f"https://openaipublic.blob.core.windows.net/encodings/{name}.tiktoken"
        target = cache / hashlib.sha1(url.encode()).hexdigest()
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            continue
        with urlopen(url, timeout=15) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("分词数据校验失败")
        target.write_bytes(content)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)


class TokenSpeedService:
    """在后台观察响应；实时值仅估算可见文本和工具参数。"""

    def __init__(self, report) -> None:
        self._report = report
        self._queue = Queue(maxsize=4096)
        self._stop = Event()
        self._states = {}
        self._samples = deque()
        self._encodings = {}
        self._thread = None
        self._overflow = Event()
        self._ws_pending = {}
        self._ws_active = {}
        self._cache_ready = False

    def start(self) -> None:
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def submit(self, kind: str, flow_id: str, data=None) -> None:
        try:
            self._queue.put_nowait((kind, flow_id, data, time.monotonic()))
        except Full:
            # 统计拥堵时放弃该次统计，绝不阻塞代理转发。
            self._overflow.set()

    def _encoding(self, model: str):
        if model not in self._encodings:
            cache = Path(__file__).resolve().with_name("tokenizer_cache")
            if getattr(sys, "frozen", False):
                cache = Path(sys.executable).parent / "lib" / "app" / "services" / "tokenizer_cache"
            if not self._cache_ready:
                prepare_tokenizer_cache(cache)
                self._cache_ready = True
            try:
                name = tiktoken.encoding_name_for_model(model)
            except KeyError:
                name = "o200k_base"
            if name not in ("cl100k_base", "o200k_base", "o200k_harmony"):
                name = "o200k_base"
            encoding = tiktoken.get_encoding(name)
            self._encodings[model] = encoding
        return self._encodings[model]

    def _run(self) -> None:
        next_tick = time.monotonic() + 1
        while not self._stop.is_set():
            key = None
            if self._overflow.is_set():
                self._states.clear()
                self._samples.clear()
                self._ws_pending.clear()
                self._ws_active.clear()
                while True:
                    try:
                        self._queue.get_nowait()
                    except Empty:
                        break
                self._overflow.clear()
                self._report({"speed": None})
            try:
                kind, key, data, now = self._queue.get(timeout=max(0, next_tick - time.monotonic()))
                self._handle(kind, key, data, now)
            except Empty:
                pass
            except Exception as exc:
                # 无法解析的响应不会影响网络或下一次统计。
                self._states.pop(key, None)
                print(f"[TokenSpeed] 统计失败: {exc}", flush=True)
            now = time.monotonic()
            if now >= next_tick:
                while self._samples and self._samples[0][0] <= now - 3:
                    self._samples.popleft()
                self._report({"speed": max(0, sum(n for _, n in self._samples) / 3)})
                for key, state in list(self._states.items()):
                    if now - state["last"] > 300:
                        self._states.pop(key, None)
                next_tick = now + 1
        self._report({"speed": 0})

    def _handle(self, kind, key, data, now) -> None:
        if kind == "ws_start":
            try:
                payload = json.loads(data[1])
            except (ValueError, UnicodeDecodeError):
                return
            if not isinstance(payload, dict) or payload.get("type") != "response.create":
                return
            pending = self._ws_pending.setdefault(key, deque(maxlen=128))
            pending.append((data[0], str(payload.get("model") or ""), now, data[2] if len(data) > 2 else ""))
            return
        if kind == "ws_end":
            self._ws_pending.pop(key, None)
            for response_id in self._ws_active.pop(key, {}):
                self._states.pop(f"{key}:{response_id}", None)
            return
        if kind == "ws_json":
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                return
            if not isinstance(payload, dict):
                return
            response = payload.get("response") or {}
            response_id = payload.get("response_id") or response.get("id")
            active = self._ws_active.setdefault(key, {})
            if payload.get("type") == "response.created" and response_id:
                pending = self._ws_pending.get(key)
                if not pending:
                    return
                token, model, started, account_id = pending.popleft()
                state_key = f"{key}:{response_id}"
                self._handle("start", state_key, (token, model, account_id), started)
                active[response_id] = state_key
            if not response_id and len(active) == 1:
                response_id = next(iter(active))
            state_key = active.get(response_id)
            if state_key in self._states:
                self._payload(state_key, self._states[state_key], data, now)
                if state_key not in self._states:
                    active.pop(response_id, None)
            return
        if kind == "start":
            token, model = data[:2]
            self._states[key] = {"token": hashlib.sha256(token.encode()).hexdigest(),
                                 "account_id": data[2] if len(data) > 2 else "",
                                 "model": model, "start": now, "last": now,
                                 "buffer": b"", "parts": {}, "count": 0}
            return
        state = self._states.get(key)
        if state is None:
            return
        state["last"] = now
        if kind == "end":
            self._states.pop(key, None)
            return
        if kind == "sse":
            state["buffer"] += data
            # 按行缓存原始字节，兼容跨块 UTF-8 和 CRLF。
            while b"\n" in state["buffer"]:
                line, state["buffer"] = state["buffer"].split(b"\n", 1)
                line = line.rstrip(b"\r")
                if line.startswith(b"data:"):
                    state.setdefault("event_lines", []).append(line[5:].lstrip(b" "))
                elif not line and state.get("event_lines"):
                    self._payload(key, state, b"\n".join(state.pop("event_lines")), now)
            if len(state["buffer"]) > 4 * 1024 * 1024:
                self._states.pop(key, None)
        elif kind == "json":
            self._payload(key, state, data, now)

    def _payload(self, key, state, raw, now) -> None:
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(event, dict):
            return
        event_type = event.get("type", "")
        response = event.get("response", event)
        if not isinstance(response, dict):
            return
        if response.get("model"):
            state["model"] = response["model"]
        if event_type in ("response.output_text.delta", "response.function_call_arguments.delta"):
            delta = event.get("delta")
            if isinstance(delta, str):
                part = (event_type, event.get("item_id", event.get("output_index", 0)), event.get("content_index", 0))
                # 保留尾部重新分词，避免每个网络分片单独分词造成明显高估。
                tail, old_count = state["parts"].get(part, ("", 0))
                text = tail + delta
                encoding = self._encoding(state["model"])
                count = len(encoding.encode(text, disallowed_special=()))
                change = count - old_count
                tail = text[-256:]
                state["parts"][part] = (tail, len(encoding.encode(tail, disallowed_special=())))
                state["count"] += change
                self._samples.append((now, change))
        if event_type in ("response.completed", "response.incomplete", "response.failed") or response.get("object") == "response" and response.get("status") in ("completed", "incomplete", "failed"):
            usage = response.get("usage") or {}
            count = usage.get("output_tokens")
            elapsed = max(0.001, now - state["start"])
            self._report({"token": state["token"], "account_id": state["account_id"], "output_tokens": count if isinstance(count, int) else state["count"],
                          "estimated": not isinstance(count, int), "seconds": elapsed,
                          "average": (count if isinstance(count, int) else state["count"]) / elapsed,
                          "status": response.get("status", event_type.removeprefix("response."))})
            self._states.pop(key, None)
