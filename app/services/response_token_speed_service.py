from __future__ import annotations

import codecs
import hashlib
import json
import os
import sys
import time
import uuid
import zlib
from collections import Counter, deque
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from tempfile import SpooledTemporaryFile
from urllib.request import urlopen

import brotli
import tiktoken
import zstandard
try:
    from app.services.response_content_counter import ResponseContentCounter
    from app.services.response_payload_reader import PayloadReader, PayloadLimitError, ResponsePayloadBuffer
    from app.services.response_usage_ledger import ResponseUsageLedger
except ModuleNotFoundError as exc:
    if exc.name != "app":
        raise
    from response_content_counter import ResponseContentCounter
    from response_payload_reader import PayloadReader, PayloadLimitError, ResponsePayloadBuffer
    from response_usage_ledger import ResponseUsageLedger


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

    def __init__(self, report, ledger_path=None) -> None:
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
        self._ledger = None
        self._ledger_path = ledger_path
        self._session = uuid.uuid4().hex
        self._sequence = 0
        self._pending_report = None
        self._incomplete = set()
        self._capture_lock = Lock()
        self._capture_bytes = 0

    def _mark_incomplete(self, reason):
        self._incomplete.add(reason)
        if self._ledger:
            try:
                self._ledger.mark_issue(reason)
            except Exception as exc:
                print(f"[TokenSpeed] 保存统计缺口失败: {exc}", flush=True)

    def _close_stream(self, key):
        state = self._streams.pop(key, None)
        if state:
            state["payload"].close()

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
        if len(raw) > 65536:
            if len(raw) > 256 * 1024 * 1024:
                self._submit(("issue", key, "消息超过 256 MiB，统计不完整", "", now))
                return
            with self._capture_lock:
                if self._capture_bytes + len(raw) > 256 * 1024 * 1024:
                    self._overflow.set()
                    return
                self._capture_bytes += len(raw)
            capture = None
            try:
                capture = SpooledTemporaryFile(max_size=512 * 1024, mode="w+b")
                capture.write(raw)
                capture.seek(0)
                capture.capture_size = len(raw)
            except OSError:
                if capture is not None:
                    capture.close()
                with self._capture_lock:
                    self._capture_bytes -= len(raw)
                self._submit(("issue", key, "消息溢写失败，统计不完整", "", now))
                return
            if not self._submit(("file", key, capture, encoding, now)):
                self._release_capture(capture)
            return
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

    def _release_capture(self, capture):
        with self._capture_lock:
            self._capture_bytes -= capture.capture_size
        capture.close()

    def _get_encoding(self):
        if self._encoding is None:
            cache = Path(__file__).resolve().with_name("tokenizer_cache")
            if getattr(sys, "frozen", False):
                cache = Path(sys.executable).parent / "lib/app/services/tokenizer_cache"
            prepare_tokenizer_cache(cache)
            self._encoding = tiktoken.get_encoding("o200k_base")
        return self._encoding

    def _count(self, state, text: str, now: float) -> None:
        state["payload"].feed(text, now)

    def _payload(self, state, text, now):
        try:
            if hasattr(text, "read"):
                reader = PayloadReader(text)
                payload = reader.read()
                for reason in reader.issues:
                    self._mark_incomplete(reason)
            else:
                payload = json.loads(text)
        except PayloadLimitError as exc:
            self._diagnostics["oversize"] += 1
            self._mark_incomplete(str(exc))
            return
        except ValueError:
            self._diagnostics["invalid_json"] += 1
            self._mark_incomplete("消息解析失败，统计不完整")
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
        if kind == "issue":
            self._mark_incomplete(raw)
            return
        if kind == "file":
            try:
                while chunk := raw.read(65536):
                    self._handle("data", key, chunk, encoding, now)
            finally:
                self._release_capture(raw)
            return
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
            if len(self._streams) >= 128:
                self._close_stream(next(iter(self._streams)))
                self._mark_incomplete("并发连接状态已淘汰，统计不完整")
            if self._ledger is None:
                self._ledger = ResponseUsageLedger(self._ledger_path)
            state = {"decoder": decoder, "utf8": codecs.getincrementaldecoder("utf-8")("replace"),
                     "buffer": "", "event": [], "event_size": 0, "sse": key.startswith("sse:"),
                     "skip_line": False, "skip_event": False,
                     "counter": ResponseContentCounter(self._get_encoding, self._add_sample, self._ledger,
                                                       self._session + ":" + key, self._mark_incomplete)}
            state["payload"] = ResponsePayloadBuffer(lambda source, stamp: self._payload(state, source, stamp),
                                                     self._mark_incomplete, key.startswith("sse:"))
            self._streams[key] = state
        decoder = state["decoder"]
        if kind in ("end", "message_end"):
            if decoder is not None and hasattr(decoder, "flush"):
                self._count(state, state["utf8"].decode(decoder.flush()), now)
            self._count(state, state["utf8"].decode(b"", final=True), now)
            if state["payload"].sse:
                self._count(state, "\n\n", now)
            state["payload"].end(now)
            if kind == "end":
                self._close_stream(key)
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
        if self._ledger_path is not None:
            try:
                self._ledger = ResponseUsageLedger(self._ledger_path)
                self._incomplete.update(self._ledger.issues())
            except Exception as exc:
                self._incomplete.add("统计账本无法打开，统计不完整")
                print(f"[TokenSpeed] 账本打开失败: {exc}", flush=True)
                self._publish(speed=0)
                return
        next_tick = time.monotonic() + 1
        while not self._stop.is_set() or not self._queue.empty():
            if self._overflow.is_set():
                for stream_key in list(self._streams):
                    self._close_stream(stream_key)
                self._mark_incomplete("统计队列已满，部分消息未统计")
                while True:
                    try:
                        dropped = self._queue.get_nowait()
                        if dropped[0] == "file":
                            self._release_capture(dropped[2])
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
                self._close_stream(key)
                self._mark_incomplete("后台统计失败，统计不完整")
                print(f"[TokenSpeed] 统计失败: {exc}", flush=True)
            if time.monotonic() >= next_tick:
                self._publish()
                next_tick = time.monotonic() + 1
                if time.monotonic() - self._diagnostic_at >= 10:
                    if self._diagnostics:
                        # 代理脚本热加载后，旧版主程序也会保存 ProxyFlow 前缀。
                        print("[ProxyFlow] [TokenSpeed] 诊断: " + json.dumps(dict(self._diagnostics), ensure_ascii=False), flush=True)
                        self._diagnostics.clear()
                    self._diagnostic_at = time.monotonic()
        for stream_key in list(self._streams):
            self._close_stream(stream_key)
        deadline = time.monotonic() + 3
        while self._ledger and self._ledger.dirty and time.monotonic() < deadline:
            if not self._publish(speed=0):
                break
        self._publish(speed=0)
        if self._ledger:
            self._ledger.close()
            self._ledger = None

    def _publish(self, speed=None):
        if self._pending_report is None:
            self._sequence += 1
            self._pending_report = {"speed": self._speed() if speed is None else speed,
                                    "session": self._session, "sequence": self._sequence,
                                    "records": self._ledger.snapshot() if self._ledger else [],
                                    "incomplete": sorted(self._incomplete)}
        result = self._report(self._pending_report)
        if result is False:
            return False
        if self._ledger:
            self._ledger.acknowledge(self._pending_report["records"])
        self._pending_report = None
        return True
