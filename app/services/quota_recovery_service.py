from __future__ import annotations

from collections import deque


class QuotaRecoveryService:
    """额度不足时触发矫正，在按钮倒计时结束后恢复一次执行。"""

    def __init__(self, correct_traffic, send, log):
        self._correct_traffic = correct_traffic
        self._send = send
        self._log = log
        self.enabled = False
        self.pending: set[str] = set()
        self._handled = deque(maxlen=2048)
        self._continued: set[str] = set()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled:
            self.pending.clear()
            self._continued.clear()

    @staticmethod
    def is_quota_error(error) -> bool:
        if not isinstance(error, dict):
            return False
        code = error.get("codexErrorInfo")
        if code == "usageLimitExceeded" or error.get("code") in {
            "usage_limit_reached", "insufficient_quota", "credit_balance_exhausted",
        }:
            return True
        # 兼容旧客户端缺少错误分类的通知，仅检查错误正文。
        message = str(error.get("message") or "").lower()
        return any(text in message for text in (
            "you've hit your usage limit", "you’ve hit your usage limit", "quota exceeded",
            "usage limit reached", "额度不足", "额度已用完", "已达到使用上限", "已达到用量上限",
        ))

    def observe(self, message: dict, direction: str = "output") -> None:
        if not self.enabled:
            return
        method, params = message.get("method"), message.get("params")
        if not isinstance(params, dict):
            return
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or not thread_id:
            return
        if direction == "input" or method in {"thread/closed", "thread/archived", "thread/deleted"}:
            self.pending.discard(thread_id)
            self._continued.discard(thread_id)
            return
        if method == "turn/started":
            self.pending.discard(thread_id)
            return
        turn = params.get("turn") or {}
        if not isinstance(turn, dict):
            return
        if method == "turn/completed" and turn.get("status") == "completed":
            self.pending.discard(thread_id)
            self._continued.discard(thread_id)
            return
        turn_id = turn.get("id") or params.get("turnId")
        error = turn.get("error") if method == "turn/completed" else params.get("error")
        if method not in {"error", "turn/completed"} or not self.is_quota_error(error):
            return
        if (method == "error" and params.get("willRetry")
                or method == "turn/completed" and turn.get("status") != "failed"):
            return
        if not isinstance(turn_id, str) or not turn_id:
            return
        key = (thread_id, turn_id)
        if key in self._handled or thread_id in self._continued:
            return
        self._handled.append(key)
        self.pending.add(thread_id)
        self._log("额度不足，矫正流量后等待 5 秒恢复执行", thread_id)
        self._correct_traffic()

    def correction_finished(self) -> None:
        if not self.enabled:
            return
        pending = self.pending.copy()
        self.pending.clear()
        for thread_id in pending:
            self._continued.add(thread_id)
            self._send(thread_id)
