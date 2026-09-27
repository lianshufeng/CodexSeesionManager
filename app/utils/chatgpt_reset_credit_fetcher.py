from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.request import ProxyHandler, Request, build_opener

from app.models import ResetCredit, ResetCreditInfo


_CHINA_TIMEZONE = timezone(timedelta(hours=8))


class ChatGPTResetCreditFetcher:
    endpoint = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"

    def fetch(self, access_token: str, account_id: str = "", proxy_url: str = "") -> ResetCreditInfo:
        return self.parse(self._request(access_token, account_id, proxy_url))

    def consume(self, access_token: str, account_id: str, proxy_url: str,
                credit_id: str, redeem_request_id: str) -> str:
        if not credit_id or not redeem_request_id:
            raise ValueError("缺少已确认的重置卡")
        payload = self._request(access_token, account_id, proxy_url, {
            "credit_id": credit_id,
            "redeem_request_id": redeem_request_id,
        })
        code = payload.get("code") if isinstance(payload, dict) else None
        if code not in {"reset", "nothing_to_reset", "no_credit", "already_redeemed"}:
            raise ValueError("重置卡使用结果未知，请刷新后查看")
        return code

    def _request(self, access_token: str, account_id: str, proxy_url: str,
                 consume_payload: dict[str, str] | None = None) -> object:
        if not access_token:
            raise ValueError("访问令牌为空")
        headers = {
            "authorization": f"Bearer {access_token}",
            "accept": "application/json",
            "user-agent": "codex-tui/0.121.0 (Windows; x86_64)",
        }
        if account_id:
            headers["chatgpt-account-id"] = account_id
        proxy_url = proxy_url.strip()
        if proxy_url and "://" not in proxy_url:
            proxy_url = f"http://{proxy_url}"
        # 使用请求独立的代理设置，避免后台线程修改进程环境变量。
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
        opener = build_opener(ProxyHandler(proxies))
        data = None
        endpoint = self.endpoint
        if consume_payload is not None:
            endpoint += "/consume"
            headers["content-type"] = "application/json"
            data = json.dumps(consume_payload).encode("utf-8")
        request = Request(endpoint, headers=headers, data=data, method="POST" if data else "GET")
        with opener.open(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload

    @staticmethod
    def parse(payload: object) -> ResetCreditInfo:
        if not isinstance(payload, dict):
            raise ValueError("重置卡响应格式异常")
        count = payload.get("available_count")
        credits = payload.get("credits")
        if type(count) is not int or count < 0 or not isinstance(credits, list):
            raise ValueError("重置卡响应缺少数量或列表")
        expiry_times: list[tuple[datetime, str]] = []
        available_credits: list[ResetCredit] = []
        for credit in credits:
            if not isinstance(credit, dict):
                raise ValueError("重置卡数据格式异常")
            if credit.get("status") != "available":
                continue
            credit_id = credit.get("id")
            if not isinstance(credit_id, str) or not credit_id:
                raise ValueError("重置卡缺少标识")
            try:
                granted_at = datetime.fromisoformat(credit["granted_at"].replace("Z", "+00:00"))
                if granted_at.tzinfo is None:
                    raise ValueError("发放时间缺少时区")
            except (KeyError, AttributeError, TypeError, ValueError):
                raise ValueError("重置卡发放时间格式异常") from None
            expires_at = credit.get("expires_at")
            if expires_at is None:
                expiry_times.append((datetime.max.replace(tzinfo=timezone.utc), "无过期时间"))
                available_credits.append(ResetCredit(credit_id, granted_at.timestamp()))
                continue
            try:
                instant = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    raise ValueError("过期时间缺少时区")
                label = instant.astimezone(_CHINA_TIMEZONE).strftime("%Y-%m-%d %H:%M")
                expiry_times.append((instant, label))
                available_credits.append(ResetCredit(credit_id, granted_at.timestamp(), instant.timestamp()))
            except (AttributeError, ValueError, OverflowError):
                raise ValueError("重置卡过期时间格式异常") from None
        expiry_times.sort()
        timestamps = tuple(instant.timestamp() for instant, label in expiry_times if label != "无过期时间")
        return ResetCreditInfo(count, tuple(label for _, label in expiry_times), timestamps, tuple(available_credits))
