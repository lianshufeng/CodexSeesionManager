from __future__ import annotations

import logging
import math
import sqlite3
import time
from collections import deque
from concurrent.futures import Future
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread


class TokenHistoryService:
    """单独线程读写 SQLite；UI 仅提交采样、读取近期内存。"""

    def __init__(self, path: Path, flush_seconds: float = 10, retention_days: int = 7) -> None:
        self.path = path
        self._flush_seconds = flush_seconds
        self._retention_seconds = retention_days * 86400
        self._queue: Queue = Queue(maxsize=8192)
        self._recent: deque[tuple[int, float]] = deque(maxlen=3600)
        self._lock = Lock()
        self._stop = Event()
        self._thread = Thread(target=self._run, name="token-history", daemon=True)
        self.error = ""
        self._thread.start()

    def record(self, speed: float, timestamp: int | None = None) -> None:
        if self._stop.is_set() or not math.isfinite(speed):
            return
        sample = (int(time.time()) if timestamp is None else timestamp, max(0, speed))
        with self._lock:
            if self._recent and self._recent[-1][0] == sample[0]:
                self._recent[-1] = sample
            else:
                self._recent.append(sample)
        try:
            self._queue.put_nowait(("record", sample))
        except Full:
            self._set_error("历史记录队列已满，部分记录未保存")

    def recent(self, since: int) -> list[tuple[int, float]]:
        with self._lock:
            return [sample for sample in self._recent if sample[0] >= since]

    def read_async(self, since: int, until: int) -> Future:
        result = Future()
        if self._stop.is_set():
            result.set_exception(RuntimeError("历史服务已停止"))
            return result
        try:
            self._queue.put_nowait(("read", (since, until, result)))
        except Full:
            result.set_exception(RuntimeError("历史服务繁忙，请稍后重试"))
        return result

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            self._set_error("历史记录尚未完成写入")

    def _set_error(self, message: str) -> None:
        if message != self.error:
            logging.getLogger(__name__).warning(message)
        self.error = message

    def _run(self) -> None:
        connection = None
        pending: dict[int, float] = {}
        next_flush = time.monotonic() + self._flush_seconds
        next_cleanup = 0.0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=2)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("CREATE TABLE IF NOT EXISTS samples (timestamp INTEGER PRIMARY KEY, speed REAL NOT NULL)")
            connection.commit()
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    kind, value = self._queue.get(timeout=min(1, max(0, next_flush - time.monotonic())))
                except Empty:
                    kind, value = "", None
                if kind == "record":
                    pending[value[0]] = value[1]
                elif kind == "read":
                    since, until, result = value
                    try:
                        rows = dict(connection.execute("SELECT timestamp, speed FROM samples WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp", (since, until)))
                        rows.update((stamp, speed) for stamp, speed in pending.items() if since <= stamp <= until)
                        result.set_result(sorted(rows.items()))
                    except Exception as exc:
                        result.set_exception(exc)
                now = time.monotonic()
                if now >= next_flush:
                    self._flush(connection, pending)
                    next_flush = now + self._flush_seconds
                if now >= next_cleanup:
                    try:
                        with connection:
                            connection.execute("DELETE FROM samples WHERE timestamp < ?", (int(time.time()) - self._retention_seconds,))
                    except sqlite3.Error as exc:
                        self._set_error(f"清理速度历史失败: {exc}")
                    next_cleanup = now + 3600
            self._flush(connection, pending)
        except Exception as exc:
            self._set_error(f"速度历史服务失败: {exc}")
            self._stop.set()
        finally:
            if connection is not None:
                connection.close()
            while True:
                try:
                    kind, value = self._queue.get_nowait()
                except Empty:
                    break
                if kind == "read":
                    value[2].set_exception(RuntimeError(self.error or "历史服务已停止"))

    def _flush(self, connection, pending: dict[int, float]) -> None:
        if not pending:
            return
        try:
            with connection:
                connection.executemany("INSERT OR REPLACE INTO samples(timestamp, speed) VALUES (?, ?)", pending.items())
            pending.clear()
            self.error = ""
        except sqlite3.Error as exc:
            self._set_error(f"保存速度历史失败: {exc}")
            # 写入失败后只保留有界缓冲，下轮重试。
            while len(pending) > 8192:
                pending.pop(next(iter(pending)))
