import math
import time
from threading import RLock


class AccountTrafficService:
    """接收不含令牌的累计字节快照，计算应用载荷速率并在空闲时归零。"""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = RLock()
        self._session = ""
        self._sequence = -1
        self._elapsed = 0.0
        self._totals = {}
        self._rates = {}
        self._updated = None

    def record(self, session: str, sequence: int, elapsed: float, totals: dict) -> bool:
        if (not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed <= 0
                or not isinstance(totals, dict) or len(totals) > 10000):
            return False
        for key, pair in totals.items():
            if (not isinstance(key, str) or not isinstance(pair, list) or len(pair) != 2
                    or any(not isinstance(n, int) or n < 0 for n in pair)):
                return False
        with self._lock:
            if session != self._session:
                self.clear()
                self._session = session
            if sequence <= self._sequence:
                return True
            if elapsed <= self._elapsed:
                return False
            period = max(0.1, elapsed - self._elapsed)
            self._rates = {
                key: tuple(max(0, pair[i] - self._totals.get(key, (0, 0))[i]) / period for i in (0, 1))
                for key, pair in totals.items()
            }
            self._totals = {key: tuple(pair) for key, pair in totals.items()}
            self._elapsed, self._sequence, self._updated = elapsed, sequence, self._clock()
            return True

    def snapshot(self) -> dict[str, tuple[float, float]]:
        with self._lock:
            if self._updated is None or self._clock() - self._updated > 2.5:
                return {}
            return dict(self._rates)

    def clear(self) -> None:
        with self._lock:
            self._sequence, self._elapsed, self._updated = -1, 0.0, None
            self._totals, self._rates = {}, {}
