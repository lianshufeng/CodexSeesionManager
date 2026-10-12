import math
import time


class AuthCooldownService:
    """账号限流状态仅保存在内存，使用单调时钟倒计时。"""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._states = {}

    @staticmethod
    def key(row):
        return row.account_id or row.refresh_token

    def hit(self, key):
        now = self._clock()
        until, last, count = self._states.get(key, (now, float("-inf"), 0))
        count = count + 1 if now - last <= 600 else 1
        self._states[key] = (now + min(600, max(0, until - now) + min(count * 60, 600)), now, count)
        return self.remaining(key)

    def remaining(self, key):
        return max(0, math.ceil(self._states.get(key, (0, 0, 0))[0] - self._clock()))
