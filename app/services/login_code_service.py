import time

import pyotp


def current_login_code(secret: str) -> tuple[str, int]:
    """按当前时间生成验证码及剩余有效秒数。"""
    timestamp = time.time()
    totp = pyotp.TOTP(secret)
    return totp.at(timestamp), totp.interval - int(timestamp) % totp.interval
