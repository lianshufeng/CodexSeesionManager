from __future__ import annotations

from enum import StrEnum

import logging
import json
import math
import sqlite3
import time
from collections import deque
from concurrent.futures import Future
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread


class HistoryTask(StrEnum):
    RECORD = "record"
    REPORT = "report"
    TOTALS = "totals"
    READ = "read"


class TokenHistoryService:
    """单独线程读写 SQLite；UI 仅提交采样、读取近期内存。"""

    def __init__(self, path: Path, flush_seconds: float = 10, retention_days: int = 7, metric: str | None = None) -> None:
        self.path = path
        self._metric = metric
        self._flush_seconds = flush_seconds
        self._retention_seconds = retention_days * 86400
        self._queue: Queue = Queue(maxsize=8192)
        self._recent: deque[tuple[int, float]] = deque(maxlen=3600)
        self._lock = Lock()
        self._stop = Event()
        self._thread = Thread(target=self._run, name="token-history", daemon=True)
        self.error = ""
        self._statistics_incomplete = ""
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
            self._queue.put_nowait((HistoryTask.RECORD, sample))
        except Full:
            self._set_error("历史记录队列已满，部分记录未保存", incomplete=True)

    def recent(self, since: int) -> list[tuple[int, float]]:
        with self._lock:
            return [sample for sample in self._recent if sample[0] >= since]

    def read_async(self, since: int, until: int) -> Future:
        result = Future()
        if self._stop.is_set():
            result.set_exception(RuntimeError("历史服务已停止"))
            return result
        try:
            self._queue.put_nowait((HistoryTask.READ, (since, until, result)))
        except Full:
            result.set_exception(RuntimeError("历史服务繁忙，请稍后重试"))
        return result

    def record_report(self, report: dict) -> Future:
        """记录响应快照；相同 ID 覆盖修正，不依赖 UI 采样和回报重试次数。"""
        return self._request(HistoryTask.REPORT, report)

    def read_totals_async(self, since: int, until: int) -> Future:
        return self._request(HistoryTask.TOTALS, (since, until))

    def _request(self, kind, value):
        result = Future()
        if self._stop.is_set():
            result.set_exception(RuntimeError(self.error or "历史服务已停止"))
            return result
        try:
            self._queue.put_nowait((kind, (value, result)))
        except Full:
            self._set_error("历史记录队列已满，统计不完整", incomplete=True)
            result.set_exception(RuntimeError(self.error))
        return result

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            self._set_error("历史记录尚未完成写入")

    def _set_error(self, message: str, *, incomplete: bool = False) -> None:
        if message != self.error:
            logging.getLogger(__name__).warning(message)
        self.error = message
        if incomplete:
            self._statistics_incomplete = message

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
            connection.execute("CREATE TABLE IF NOT EXISTS responses (id TEXT PRIMARY KEY, timestamp INTEGER, estimate INTEGER, usage TEXT)")
            connection.execute("CREATE INDEX IF NOT EXISTS responses_timestamp ON responses(timestamp)")
            connection.execute("CREATE TABLE IF NOT EXISTS statistics_status (session TEXT PRIMARY KEY, timestamp INTEGER, reasons TEXT)")
            if self._metric is not None:
                connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                previous = connection.execute("SELECT value FROM metadata WHERE key = 'metric'").fetchone()
                if previous != (self._metric,):
                    # 统计口径变化时重置旧采样，避免历史曲线混用两种单位。
                    connection.execute("CREATE TABLE IF NOT EXISTS legacy_samples (timestamp INTEGER PRIMARY KEY, speed REAL NOT NULL)")
                    connection.execute("INSERT OR IGNORE INTO legacy_samples SELECT * FROM samples")
                    connection.execute("DELETE FROM samples")
                    connection.execute("INSERT OR REPLACE INTO metadata VALUES ('metric', ?)", (self._metric,))
            connection.commit()
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    kind, value = self._queue.get(timeout=min(1, max(0, next_flush - time.monotonic())))
                except Empty:
                    kind, value = "", None
                if kind == HistoryTask.RECORD:
                    pending[value[0]] = value[1]
                elif kind == HistoryTask.REPORT:
                    report, result = value
                    try:
                        with connection:
                            for record in report.get("records", []):
                                usage = record.get("usage")
                                connection.execute("INSERT INTO responses VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET estimate=excluded.estimate, usage=COALESCE(excluded.usage, responses.usage)",
                                                   (record["id"], int(record["timestamp"]), max(0, int(record["estimate"])),
                                                    json.dumps(usage) if isinstance(usage, dict) else None))
                            if report.get("incomplete"):
                                session = report.get("session", "unknown")
                                previous = connection.execute("SELECT reasons FROM statistics_status WHERE session=?", (session,)).fetchone()
                                reasons = set(json.loads(previous[0]) if previous else []) | set(report["incomplete"])
                                connection.execute("INSERT INTO statistics_status VALUES (?, ?, ?) ON CONFLICT(session) DO UPDATE SET reasons=excluded.reasons",
                                                   (session, int(time.time()), json.dumps(sorted(reasons), ensure_ascii=False)))
                        result.set_result(True)
                    except Exception as exc:
                        self._set_error(f"保存用量失败，统计不完整: {exc}", incomplete=True)
                        result.set_exception(exc)
                elif kind == HistoryTask.TOTALS:
                    (since, until), result = value
                    try:
                        totals = dict(input=0, output=0, cached_input=0, cache_write_input=0,
                                      reasoning_output=0, total=0, estimate=0, official_responses=0, estimated_responses=0)
                        for estimate, usage_json in connection.execute("SELECT estimate, usage FROM responses WHERE timestamp BETWEEN ? AND ?", (since, until)):
                            if usage_json:
                                usage = json.loads(usage_json)
                                for name in ("input", "output", "cached_input", "cache_write_input", "reasoning_output", "total"):
                                    totals[name] += usage.get(name, 0)
                                totals["official_responses"] += 1
                            elif estimate:
                                totals["estimate"] += estimate
                                totals["estimated_responses"] += 1
                        totals["incomplete"] = [reason for (reasons,) in connection.execute("SELECT reasons FROM statistics_status WHERE timestamp BETWEEN ? AND ?", (since, until)) for reason in json.loads(reasons)]
                        if self._statistics_incomplete:
                            totals["incomplete"].append(self._statistics_incomplete)
                        result.set_result(totals)
                    except Exception as exc:
                        result.set_exception(exc)
                elif kind == HistoryTask.READ:
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
                            connection.execute("DELETE FROM responses WHERE timestamp < ?", (int(time.time()) - self._retention_seconds,))
                            connection.execute("DELETE FROM statistics_status WHERE timestamp < ?", (int(time.time()) - self._retention_seconds,))
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
                if kind == HistoryTask.READ:
                    value[2].set_exception(RuntimeError(self.error or "历史服务已停止"))
                elif kind in (HistoryTask.REPORT, HistoryTask.TOTALS):
                    value[1].set_exception(RuntimeError(self.error or "历史服务已停止"))

    def _flush(self, connection, pending: dict[int, float]) -> None:
        if not pending:
            return
        try:
            with connection:
                connection.executemany("INSERT OR REPLACE INTO samples(timestamp, speed) VALUES (?, ?)", pending.items())
            pending.clear()
            self.error = self._statistics_incomplete
        except sqlite3.Error as exc:
            self._set_error(f"保存速度历史失败: {exc}")
            # 写入失败后只保留有界缓冲，下轮重试。
            while len(pending) > 8192:
                pending.pop(next(iter(pending)))
