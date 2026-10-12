def is_rate_limit(payload):
    """只检查结构化错误字段，避免将正常生成内容识别为限流。"""
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    response = payload.get("response")
    if error is None and isinstance(response, dict):
        error = response.get("error")
    if error is None and payload.get("type") == "error":
        error = payload
    if error is None and payload.get("type") in {"response.failed", "response.error"}:
        error = payload
    if isinstance(error, str):
        error = {"message": error}
    if not isinstance(error, dict):
        return False
    if str(error.get("status", error.get("status_code", ""))) == "429":
        return True
    code = str(error.get("code") or error.get("type") or "").lower()
    message = str(error.get("message") or "").lower()
    return code in {"rate_limit_exceeded", "rate_limit_error", "usage_limit_reached", "insufficient_quota", "model_capacity_exceeded", "model_at_capacity", "model_overloaded", "overloaded_error", "server_overloaded", "server_is_overloaded"} or any(
        text in message for text in ("selected model is at capacity", "model is at capacity", "model is overloaded", "model is currently overloaded", "rate limit exceeded", "rate limit reached", "too many requests")
    )
