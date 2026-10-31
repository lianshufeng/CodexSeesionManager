from collections import deque


RESPONSE_TERMINAL_EVENTS = frozenset({
    "response.completed", "response.done", "response.failed", "response.incomplete",
    "response.cancelled", "response.canceled", "response.error", "error",
})


class ResponseActivityTracker:
    """只保存响应标识，区分 WebSocket 预热、推理和终止事件。"""

    def __init__(self) -> None:
        self._pending: list[dict] = []
        self._finished: deque[str] = deque(maxlen=128)
        self._events: deque[tuple[bool, str]] = deque(maxlen=128)
        self._overflow = False
        self._interrupts: set[str] = set()
        self._uncertain = False

    @property
    def busy(self) -> bool:
        return bool(self._pending or self._interrupts or self._overflow or self._uncertain)

    @property
    def count(self) -> int:
        # 异常流水线超限时保持保守计数，断线后由租约释放兜底。
        return 1024 if self._overflow else sum(item["active"] for item in self._pending)

    def observe(self, payload, from_client: bool) -> int:
        if not isinstance(payload, dict):
            return self.count
        tag = payload.get("type")
        if not isinstance(tag, str):
            return self.count
        if from_client and tag == "response.interrupt":
            response_id = payload.get("response_id")
            if isinstance(response_id, str):
                self._interrupts.add(response_id)
            else:
                self._uncertain = True
            return self.count
        if not from_client and tag in {"response.interrupt.accepted", "response.interrupt.failed"}:
            response_id = payload.get("response_id")
            if isinstance(response_id, str):
                self._interrupts.discard(response_id)
            if tag == "response.interrupt.failed":
                self._uncertain = True
            return self.count
        if (from_client and tag != "response.create") or (
                not from_client and tag not in RESPONSE_TERMINAL_EVENTS
                and tag not in {"response.created", "response.in_progress"}):
            return self.count
        event_id = payload.get("event_id")
        if isinstance(event_id, str) and event_id:
            identity = (from_client, event_id)
            if identity in self._events:
                return self.count
            self._events.append(identity)
        if from_client:
            if len(self._pending) >= 1024:
                self._overflow = True
            else:
                lane = payload.get("stream_id")
                self._pending.append({"id": "", "active": payload.get("generate") is not False,
                                      "lane": lane if isinstance(lane, str) else ""})
            return self.count
        response = payload.get("response")
        response_id = payload.get("response_id") or (response.get("id") if isinstance(response, dict) else "")
        response_id = response_id if isinstance(response_id, str) else ""
        if response_id and response_id in self._finished:
            return self.count
        item = next((item for item in self._pending if response_id and item["id"] == response_id), None)
        if item is None:
            lane = payload.get("stream_id")
            item = next((item for item in self._pending if not item["id"]
                         and (not isinstance(lane, str) or item["lane"] == lane)), None)
        if item is None and not response_id and self._pending:
            lane = payload.get("stream_id")
            item = next((item for item in self._pending if not isinstance(lane, str) or item["lane"] == lane), None)
        if item is None:
            return self.count
        if tag in {"response.created", "response.in_progress"}:
            if response_id:
                item["id"] = response_id
        else:
            if response_id or item["id"]:
                self._finished.append(response_id or item["id"])
            self._pending.remove(item)
        return self.count
