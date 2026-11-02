from __future__ import annotations

import re
import time
from threading import Event
from typing import Callable
from urllib.parse import urlsplit

from app.models import LoginCredentials


class BrowserAuthService:
    """每次启动独立的非持久化会话；只在受信任登录域名内填写凭据。"""

    def run(
        self,
        authorize_url: str,
        credentials: LoginCredentials,
        done: Event,
        status: Callable[[str], None],
        proxy_url: str = "",
        timeout_seconds: float = 300,
    ) -> None:
        try:
            from playwright.sync_api import sync_playwright
            import pyotp
        except ImportError as exc:
            raise RuntimeError("缺少自动登录依赖，请安装 requirement.txt 中的依赖。") from exc
        credentials.validate()
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(
                    channel="chrome", headless=False,
                    **({"proxy": {"server": proxy_url}} if proxy_url else {}),
                )
            except Exception:
                raise RuntimeError("无法启动自动登录 Chrome，请确认已安装 Chrome 及 Playwright 运行组件。") from None
            try:
                context = browser.new_context()
                context.set_default_timeout(1500)
                page = context.new_page()
                status("正在打开登录页")
                page.goto(authorize_url, wait_until="domcontentloaded", timeout=60000)
                self.drive_page(page, credentials, done, status, pyotp, timeout_seconds)
            finally:
                browser.close()

    def drive_page(
        self, page, credentials: LoginCredentials, done: Event,
        status: Callable[[str], None], pyotp, timeout_seconds: float,
        allowed_origin: str = "https://auth.openai.com",
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        submitted: set[str] = set()
        last_status = ""
        from playwright.sync_api import Error
        while not done.is_set():
            if page.is_closed():
                raise RuntimeError("登录窗口已关闭。")
            if time.monotonic() >= deadline:
                raise RuntimeError("登录超时，请重新更新授权。")
            parsed = urlsplit(page.url)
            trusted = f"{parsed.scheme}://{parsed.netloc}" == allowed_origin
            message = "等待人工完成验证或授权确认"
            if trusted:
                try:
                    stage, field = self._detect_field(page)
                except Error:
                    page.wait_for_timeout(250)
                    continue
                if stage and stage not in submitted:
                    value = ""
                    if stage == "email":
                        value, message = credentials.email, "正在输入登录邮箱"
                    elif stage == "password":
                        value, message = credentials.password, "正在输入登录密码"
                    elif credentials.totp_secret:
                        # 避免在验证码即将过期时提交。
                        if 30 - time.time() % 30 < 5:
                            page.wait_for_timeout(250)
                            continue
                        value = pyotp.TOTP(credentials.totp_secret).now()
                        message = "正在输入验证器动态码"
                    else:
                        message = "未配置 2FA 密钥，等待手动输入验证码"
                    if value:
                        status(message)
                        field.fill(value)
                        submitted.add(stage)
                        self._submit(page, field)
                        page.wait_for_timeout(250)
                        continue
            if message != last_status:
                status(message)
                last_status = message
            page.wait_for_timeout(250)

    def _detect_field(self, page):
        fields = (
            ("totp", 'input[autocomplete="one-time-code"], input[name="totp"], input[name="code"]'),
            ("password", 'input[type="password"]'),
            ("email", 'input[type="email"], input[autocomplete="username"], input[name="username"]'),
        )
        # 邮件/短信验证码也可能叫 code，只有明确的验证器页面才自动填 TOTP。
        text = page.locator("body").inner_text(timeout=1500)
        authenticator = bool(re.search(r"authenticator|authentication app|验证器|身份验证应用|身份验证器", text, re.I))
        for stage, selector in fields:
            if stage == "totp" and not authenticator:
                continue
            inputs = page.locator(selector)
            for index in range(inputs.count()):
                field = inputs.nth(index)
                if field.is_visible() and field.is_editable():
                    return stage, field
        return "", None

    def _submit(self, page, field) -> None:
        form = field.locator("xpath=ancestor::form[1]")
        buttons = form.locator('button[type="submit"], input[type="submit"]') if form.count() else page.locator('button[type="submit"]')
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if button.is_visible() and button.is_enabled():
                button.click()
                return
        buttons = page.get_by_role("button", name=re.compile(r"^(continue|log in|sign in|verify|继续|登录|验证|确认)$", re.I))
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if button.is_visible() and button.is_enabled():
                button.click()
                return
        field.press("Enter")
