from __future__ import annotations

import json
import sqlite3
import time
import weakref
from tempfile import TemporaryDirectory
from pathlib import Path


class ResponseUsageLedger:
    """后台线程的磁盘去重账本；不靠淘汰已完成 ID 控制内存。"""

    def __init__(self, path=None):
        self.directory = TemporaryDirectory(prefix="codex-token-ledger-") if path is None else None
        path = Path(self.directory.name) / "ledger.sqlite3" if path is None else Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self._cleanup = weakref.finalize(self, self._close_resources, self.connection, self.directory)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE TABLE IF NOT EXISTS parts (response TEXT, part TEXT, total INTEGER, done INTEGER, tail TEXT, tail_count INTEGER, PRIMARY KEY(response, part))")
        self.connection.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, timestamp INTEGER, estimate INTEGER, usage TEXT)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS finished (id TEXT PRIMARY KEY)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS dirty (id TEXT PRIMARY KEY)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS covered (id TEXT PRIMARY KEY)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS issues (reason TEXT PRIMARY KEY, timestamp INTEGER)")
        expired = int(time.time()) - 7 * 86400
        for table, column in (("parts", "response"), ("finished", "id"), ("dirty", "id"), ("covered", "id")):
            self.connection.execute(f"DELETE FROM {table} WHERE {column} IN (SELECT id FROM records WHERE timestamp < ?)", (expired,))
        self.connection.execute("DELETE FROM records WHERE timestamp < ?", (expired,))
        self.connection.execute("DELETE FROM issues WHERE timestamp < ?", (expired,))
        self.connection.commit()

    @property
    def dirty(self):
        return self.connection.execute("SELECT 1 FROM dirty LIMIT 1").fetchone() is not None

    def mark_dirty(self, identity):
        self.connection.execute("INSERT OR IGNORE INTO dirty VALUES (?)", (str(identity),))

    def mark_issue(self, reason):
        self.connection.execute("INSERT OR REPLACE INTO issues VALUES (?, ?)", (reason, int(time.time())))
        self.connection.commit()

    def issues(self):
        return {row[0] for row in self.connection.execute("SELECT reason FROM issues")}

    def cover_estimate(self, identity, now):
        stamp = int(time.time() - time.monotonic() + now)
        self.connection.execute("INSERT OR IGNORE INTO covered VALUES (?)", (str(identity),))
        self.connection.execute("INSERT OR IGNORE INTO records VALUES (?, ?, 0, NULL)", (str(identity), stamp))
        self.connection.execute("UPDATE records SET estimate=0 WHERE id=?", (str(identity),))
        self.mark_dirty(identity)
        self.connection.commit()

    def finished(self, response_id):
        return self.connection.execute("SELECT 1 FROM finished WHERE id=?", (str(response_id),)).fetchone() is not None

    def finish(self, response_id):
        self.connection.execute("INSERT OR IGNORE INTO finished VALUES (?)", (str(response_id),))
        self.connection.commit()

    def rename(self, previous, current):
        previous, current = str(previous), str(current)
        old = self.connection.execute("SELECT timestamp, estimate, usage FROM records WHERE id=?", (previous,)).fetchone()
        if old is None:
            return
        exists = self.connection.execute("SELECT 1 FROM records WHERE id=?", (current,)).fetchone()
        if not exists:
            self.connection.execute("INSERT INTO records VALUES (?, ?, ?, ?)", (current, *old))
            self.connection.execute("UPDATE parts SET response=? WHERE response=?", (current, previous))
        self.connection.execute("UPDATE records SET estimate=0, usage=NULL WHERE id=?", (previous,))
        self.mark_dirty(previous)
        self.mark_dirty(current)

    def part(self, response_id, key):
        row = self.connection.execute("SELECT total, done, tail, tail_count FROM parts WHERE response=? AND part=?",
                                      (str(response_id), json.dumps(key))).fetchone()
        return row or (0, False, "", 0)

    def set_part(self, response_id, key, total, done, now, tail="", tail_count=0):
        old = self.part(response_id, key)[0]
        self.connection.execute("INSERT OR REPLACE INTO parts VALUES (?, ?, ?, ?, ?, ?)",
                                (str(response_id), json.dumps(key), total, int(done), tail, tail_count))
        stamp = int(time.time() - time.monotonic() + now)
        self.connection.execute("INSERT OR IGNORE INTO records VALUES (?, ?, 0, NULL)", (str(response_id), stamp))
        self.connection.execute("UPDATE records SET estimate=estimate+? WHERE id=? AND id NOT IN (SELECT id FROM covered)", (total - old, str(response_id)))
        self.mark_dirty(response_id)
        return total - old

    def usage(self, response_id, usage, now):
        if not isinstance(usage, dict) or not isinstance(response_id, str) or len(response_id) > 512:
            return False
        def number(*keys, source=usage):
            for key in keys:
                value = source.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
            return None
        inputs = number("input_tokens", "prompt_tokens", "inputTokens")
        outputs = number("output_tokens", "completion_tokens", "outputTokens")
        if inputs is None or outputs is None:
            return False
        input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
        input_details = input_details if isinstance(input_details, dict) else {}
        output_details = output_details if isinstance(output_details, dict) else {}
        normalized = {"input": inputs, "output": outputs,
                      "cached_input": number("cached_input_tokens", "cachedInputTokens") or number("cached_tokens", source=input_details) or 0,
                      "cache_write_input": number("cache_write_input_tokens", "cacheWriteInputTokens") or number("cache_write_tokens", source=input_details) or 0,
                      "reasoning_output": number("reasoning_output_tokens", "reasoningOutputTokens") or number("reasoning_tokens", source=output_details) or 0,
                      "total": number("total_tokens", "totalTokens")}
        if normalized["total"] is None:
            normalized["total"] = inputs + outputs
        stamp = int(time.time() - time.monotonic() + now)
        self.connection.execute("INSERT OR IGNORE INTO records VALUES (?, ?, 0, NULL)", (str(response_id), stamp))
        self.connection.execute("UPDATE records SET usage=? WHERE id=?", (json.dumps(normalized), str(response_id)))
        self.mark_dirty(response_id)
        # 完成响应的官方用量立即落盘，代理进程被终止后仍可重送。
        self.connection.commit()
        return True

    def snapshot(self, limit=64):
        ids = [row[0] for row in self.connection.execute("SELECT id FROM dirty ORDER BY rowid LIMIT ?", (limit,))]
        records = []
        for identity in ids:
            row = self.connection.execute("SELECT timestamp, estimate, usage FROM records WHERE id=?", (identity,)).fetchone()
            records.append({"id": identity, "timestamp": row[0], "estimate": max(0, row[1]),
                            "usage": json.loads(row[2]) if row[2] else None})
        self.connection.commit()
        return records

    def acknowledge(self, records):
        for record in records:
            row = self.connection.execute("SELECT timestamp, estimate, usage FROM records WHERE id=?", (record["id"],)).fetchone()
            current_usage = json.loads(row[2]) if row and row[2] else None
            if row and (row[0], max(0, row[1]), current_usage) == (record["timestamp"], record["estimate"], record["usage"]):
                self.connection.execute("DELETE FROM dirty WHERE id=?", (record["id"],))
        self.connection.commit()

    def close(self):
        self._cleanup()

    @staticmethod
    def _close_resources(connection, directory):
        connection.close()
        if directory is not None:
            directory.cleanup()
