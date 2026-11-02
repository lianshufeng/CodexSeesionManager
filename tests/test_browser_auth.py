import json
import socket
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit, urlencode
from urllib.request import build_opener, ProxyHandler

import pyotp
from playwright.sync_api import sync_playwright

from app.models import LoginCredentials
from app.services.auth_sync_service import AuthSyncService
from app.services.auth_login_service import AuthLoginService
from app.services.browser_auth_service import BrowserAuthService
from app.services.cloud_sync_service import CloudSyncService, CloudSyncConfig


class BrowserAuthAcceptance(unittest.TestCase):
    def test_oauth_callback_saves_tokens_and_finishes_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sync = AuthSyncService(source_path=root / "source.json", target_dir=root / "auth")
            service = AuthLoginService(sync)
            finished = Event()
            outcomes = []
            def finish(result, error):
                outcomes.append((result, error))
                finished.set()
            service.set_result_callback(finish)
            service._request_tokens = lambda *_: {}
            service._build_auth_data = lambda _: {"tokens": {
                "account_id": "test", "refresh_token": "new-refresh", "access_token": "new-access",
            }}
            def simulate_browser(authorize_url, *args):
                query = parse_qs(urlsplit(authorize_url).query)
                callback = query["redirect_uri"][0] + "?" + urlencode({
                    "state": query["state"][0], "code": "test-code",
                })
                with build_opener(ProxyHandler({})).open(callback, timeout=5) as response:
                    self.assertEqual(response.status, 200)
            with patch.object(BrowserAuthService, "run", side_effect=simulate_browser):
                service.start_login(credentials=LoginCredentials("test@example.com", "test-password"), expected_account_id="test", callback_port=0)
                self.assertTrue(finished.wait(10))
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0][1], "")
            self.assertEqual(sync.list_auth_rows()[0].refresh_token, "new-refresh")
            self.assertIsNone(service._callback_server)

    def test_cloud_upload_excludes_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "auth").mkdir()
            path = root / "auth" / "test.json"
            data = {"tokens": {}, "_codex_session_manager": {
                "note": "测试备注", "login_credentials": {"password": "test-password"},
            }}
            path.write_text(json.dumps(data), encoding="utf-8")
            service = CloudSyncService(CloudSyncConfig("", "bucket", "", ""), root)
            client = Mock()
            service._create_client = lambda: client
            service._next_version_prefix = lambda _: "20261101_1/"
            service._build_version = lambda *_: None
            service.sync_auth()
            uploaded = json.loads(client.put_object.call_args.kwargs["Body"])
            self.assertNotIn("login_credentials", uploaded["_codex_session_manager"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), data)

    def test_missing_totp_waits_for_user_and_untrusted_origin_is_not_filled(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page()
                page.route("https://login.test/**", lambda route: route.fulfill(
                    body='<html><body>Authenticator code<form><input autocomplete="one-time-code"></form></body></html>', content_type="text/html",
                ))
                page.goto("https://login.test/0")
                done = Event()
                messages = []
                def status(message):
                    messages.append(message)
                    done.set()
                service = BrowserAuthService()
                service.drive_page(page, LoginCredentials("test@example.com", "password"), done, status, pyotp, 5, "https://login.test")
                self.assertIn("未配置 2FA 密钥", messages[0])
                self.assertEqual(page.locator("input").input_value(), "")
                done.clear()
                service.drive_page(page, LoginCredentials("test@example.com", "password", "JBSWY3DPEHPK3PXP"), done, status, pyotp, 5)
                self.assertEqual(page.locator("input").input_value(), "")
            finally:
                browser.close()

    def test_credentials_and_callback_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sync = AuthSyncService(source_path=root / "source.json", target_dir=root / "auth")
            sync.save_logged_in_auth_file({"tokens": {
                "account_id": "test", "access_token": "access", "refresh_token": "refresh",
            }})
            self.assertTrue(sync.set_login_credentials("refresh", "test@example.com", "test-password", "JBSWY3DPEHPK3PXP")[0])
            self.assertEqual(sync.get_login_credentials("refresh")["totp_secret"], "JBSWY3DPEHPK3PXP")
            sync.save_logged_in_auth_file({"tokens": {
                "account_id": "test", "access_token": "new-access", "refresh_token": "new-refresh",
            }})
            self.assertEqual(sync.get_login_credentials("new-refresh")["password"], "test-password")
            self.assertTrue(sync.activate_auth_file("new-refresh")[0])
            self.assertNotIn("_codex_session_manager", json.loads(sync.source_path.read_text(encoding="utf-8")))
            login = AuthLoginService(sync)
            try:
                login.start_callback_listener(callback_port=0)
                port = login._callback_port
            finally:
                login.stop_callback_listener()
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))

    def test_browser_email_password_totp_without_listening_port(self):
        credentials = LoginCredentials("test@example.com", "test-password", "JBSWY3DPEHPK3PXP")
        done = Event()
        values = []
        statuses = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()

                def respond(route):
                    stage = int(route.request.url.rsplit("/", 1)[-1])
                    if route.request.method == "POST":
                        from urllib.parse import parse_qs
                        values.append(parse_qs(route.request.post_data)["value"][0])
                    if stage == 3:
                        done.set()
                        route.fulfill(body="<html><body>成功</body></html>", content_type="text/html")
                        return
                    types = ("email", "password", "text")
                    attrs = ' autocomplete="one-time-code"' if stage == 2 else ""
                    heading = "Authenticator code" if stage == 2 else "Login"
                    route.fulfill(body=f'<html><body><h1>{heading}</h1><form method="post" action="/{stage + 1}"><input name="value" type="{types[stage]}"{attrs}><button type="submit">Continue</button></form></body></html>', content_type="text/html")

                context.route("https://login.test/**", respond)
                page.goto("https://login.test/0")
                BrowserAuthService().drive_page(page, credentials, done, statuses.append, pyotp, 20, "https://login.test")
                self.assertEqual(values[:2], [credentials.email, credentials.password])
                self.assertEqual(len(values), 3)
                self.assertTrue(pyotp.TOTP(credentials.totp_secret).verify(values[2], valid_window=1))
                self.assertEqual(context.cookies(), [])
                context.close()
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
