from __future__ import annotations

import codecs
import hashlib
import json
import os
import sys
import time
import zlib
from collections import Counter, deque
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from urllib.request import urlopen

import brotli
import tiktoken
import zstandard
try:
    from app.services.response_content_counter import ResponseContentCounter
except ModuleNotFoundError as exc:
    if exc.name != "app":
        raise
    from response_content_counter import ResponseContentCounter


def prepare_tokenizer_cache(cache: Path) -> None:
    """构建时保存分词数据，编译版运行时无需下载。"""
    url = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
    expected = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / hashlib.sha1(url.encode()).hexdigest()
    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        with urlopen(url, timeout=15) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("分词数据校验失败")
        target.write_bytes(content)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)


class ResponseTokenSpeedService:
    """后台解码响应并统计实际内容；队列、响应缓存和计数状态均有上限。"""

    def __init__(self, report) -> None:
        self._report = report
        self._buckets: deque[tuple[int, int]] = deque(maxlen=31)
        self._queue = Queue(maxsize=256)
        self._streams = {}
        self._encoding = None
        self._overflow = Event()
        self._stop = Event()
        self._thread = None
        self._diagnostics = Counter()
        self._diagnostic_at = time.monotonic()

    def start(self) -> None:
        self._thread = Thread(target=self._run, name="response-speed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def observe(self, key: str, content: bytes | str, encoding: str = "") -> None:
        if self._stop.is_set():
            return
        raw = content.encode("utf-8") if isinstance(content, str) else content
        now = time.monotonic()
        # 限制队列载荷为约 16 MiB，分词和解压均不阻塞代理转发。
        for offset in range(0, len(raw), 65536):
            if not self._submit(("data", key, raw[offset:offset + 65536], encoding, now)):
                break

    def end(self, key: str) -> None:
        self._submit(("end", key, b"", "", time.monotonic()))

    def message_end(self, key: str) -> None:
        self._submit(("message_end", key, b"", "", time.monotonic()))

    def _submit(self, item) -> bool:
        try:
            self._queue.put_nowait(item)
            return True
        except Full:
            self._overflow.set()
            return False

    def _get_encoding(self):
        if self._encoding is None:
            cache = Path(__file__).resolve().with_name("tokenizer_cache")
            if getattr(sys, "frozen", False):
                cache = Path(sys.executable).parent / "lib/app/services/tokenizer_cache"
            prepare_tokenizer_cache(cache)
            self._encoding = tiktoken.get_encoding("o200k_base")
        return self._encoding

    def _count(self, state, text: str, now: float) -> None:
        if state["skip_line"]:
            boundary = text.find("\n")
            if boundary < 0:
                return
            text = text[boundary + 1:]
            state["skip_line"] = False
        if state["skip_event"] and not state["sse"]:
            return
        state["buffer"] += text
        if not state["sse"] and state["buffer"].lstrip().startswith(("data:", ": ")):
            state["sse"] = True
        if state["sse"]:
            while "\n" in state["buffer"]:
                line, state["buffer"] = state["buffer"].split("\n", 1)
                line = line.rstrip("\r")
                if line.startswith("data:") and not state["skip_event"]:
                    state["event"].append(line[5:].lstrip(" "))
                    state["event_size"] += len(line)
                    if state["event_size"] > 512 * 1024:
                        state["event"].clear()
                        state["skip_event"] = True
                        self._diagnostics["oversize"] += 1
                elif not line:
                    if state["event"] and not state["skip_event"]:
                        self._payload(state, "\n".join(state["event"]), now)
                    state["event"].clear()
                    state["event_size"] = 0
                    state["skip_event"] = False
        if len(state["buffer"]) > 512 * 1024:
            state["buffer"] = ""
            state["skip_line"] = state["sse"]
            state["skip_event"] = True
            self._diagnostics["oversize"] += 1

    def _payload(self, state, text, now):
        if not text.strip() or text.strip() == "[DONE]":
            return
        try:
            payload = json.loads(text)
        except ValueError:
            self._diagnostics["invalid_json"] += 1
            return
        if isinstance(payload, dict):
            tag = payload.get("type") or payload.get("method") or "envelope"
            if isinstance(tag, str) and len(tag) <= 100 and all(c.isalnum() or c in "_./-" for c in tag):
                label = "event:" + tag
                if label in self._diagnostics or len(self._diagnostics) < 32:
                    self._diagnostics[label] += 1
        state["counter"].observe(payload, now)

    def _add_sample(self, change, now):
        self._diagnostics["tokens"] += change
        bucket = int(now * 10)
        if self._buckets and self._buckets[-1][0] == bucket:
            self._buckets[-1] = (bucket, self._buckets[-1][1] + change)
        else:
            self._buckets.append((bucket, change))

    def _handle(self, kind, key, raw, encoding, now) -> None:
        state = self._streams.get(key)
        if state is None:
            if kind in ("end", "message_end"):
                return
            decoder = None
            if encoding in ("gzip", "deflate"):
                decoder = zlib.decompressobj(31 if encoding == "gzip" else 15)
            elif encoding == "br":
                decoder = brotli.Decompressor()
            elif encoding == "zstd":
                decoder = zstandard.ZstdDecompressor().decompressobj()
            elif encoding not in ("", "identity"):
                raise ValueError("不支持的响应压缩格式")
            if len(self._streams) >= 32:
                self._streams.pop(next(iter(self._streams)))
            state = {"decoder": decoder, "utf8": codecs.getincrementaldecoder("utf-8")("replace"),
                     "buffer": "", "event": [], "event_size": 0, "sse": key.startswith("sse:"),
                     "skip_line": False, "skip_event": False,
                     "counter": ResponseContentCounter(self._get_encoding, self._add_sample)}
            self._streams[key] = state
        decoder = state["decoder"]
        if kind in ("end", "message_end"):
            if decoder is not None and hasattr(decoder, "flush"):
                self._count(state, state["utf8"].decode(decoder.flush()), now)
            self._count(state, state["utf8"].decode(b"", final=True), now)
            if state["sse"]:
                self._count(state, "\n\n", now)
            else:
                if not state["skip_event"]:
                    self._payload(state, state["buffer"], now)
            if kind == "end":
                self._streams.pop(key, None)
            else:
                state["buffer"] = ""
                state["skip_event"] = False
                state["utf8"] = codecs.getincrementaldecoder("utf-8")("replace")
        elif encoding in ("gzip", "deflate"):
            while raw:
                decoded = decoder.decompress(raw, 65536)
                self._count(state, state["utf8"].decode(decoded), now)
                raw = decoder.unconsumed_tail
        else:
            decoded = decoder.process(raw) if encoding == "br" else decoder.decompress(raw) if decoder else raw
            self._count(state, state["utf8"].decode(decoded), now)

    def _speed(self) -> float:
        cutoff = int(time.monotonic() * 10) - 30
        while self._buckets and self._buckets[0][0] <= cutoff:
            self._buckets.popleft()
        return max(0, sum(count for _, count in self._buckets) / 3)

    def _run(self) -> None:
        next_tick = time.monotonic() + 1
        while not self._stop.is_set() or not self._queue.empty():
            if self._overflow.is_set():
                self._streams.clear()
                while True:
                    try:
                        self._queue.get_nowait()
                    except Empty:
                        break
                self._overflow.clear()
                print("[TokenSpeed] 统计队列已满，已重置本次统计", flush=True)
            key = None
            try:
                item = self._queue.get(timeout=max(0, next_tick - time.monotonic()))
                key = item[1]
                self._handle(*item)
            except Empty:
                pass
            except Exception as exc:
                self._streams.pop(key, None)
                print(f"[TokenSpeed] 统计失败: {exc}", flush=True)
            if time.monotonic() >= next_tick:
                self._report({"speed": self._speed()})
                next_tick = time.monotonic() + 1
                if time.monotonic() - self._diagnostic_at >= 10:
                    if self._diagnostics:
                        # 代理脚本热加载后，旧版主程序也会保存 ProxyFlow 前缀。
                        print("[ProxyFlow] [TokenSpeed] 诊断: " + json.dumps(dict(self._diagnostics), ensure_ascii=False), flush=True)
                        self._diagnostics.clear()
                    self._diagnostic_at = time.monotonic()
        self._streams.clear()
        self._report({"speed": 0})
