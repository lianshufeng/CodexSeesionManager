import json
import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock, patch

from app.services.auth_sync_service import AuthSyncService
from app.services.cloud_sync_service import CloudSyncConfig, CloudSyncService


class CloudAuthSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "codex" / "auth.json"
        self.version = self.root / "download"
        self.staged = self.version / "auth"
        self.staged.mkdir(parents=True)
        self.service = AuthSyncService(self.source, self.root / "manager" / "auth")
        self.cloud = CloudSyncService(CloudSyncConfig("", "", "", ""), self.root / "manager")
        self.local = self.auth("local", "2026-10-18T00:00:00Z")
        self.remote = self.auth("remote", "2026-10-19T00:00:00Z")
        self.write(self.source, self.local)
        self.write(self.staged / "account.json", self.remote)

    @staticmethod
    def auth(token, timestamp, account="account"):
        return {
            "tokens": {"account_id": account, "refresh_token": token, "access_token": "access-" + token},
            "last_refresh": timestamp,
            "_codex_session_manager": {"note": "云端备注", "load_strategy": "priority"},
        }

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def read(path):
        return json.loads(path.read_text(encoding="utf-8"))

    def apply(self, callback=None):
        return self.service.apply_cloud_version(self.version, callback or self.cloud._apply_pulled_version)

    def assert_current(self, token):
        self.service.sync_now()
        rows = self.service.list_auth_rows()
        self.assertEqual([row.refresh_token for row in rows if row.current], [token])
        self.assertEqual(self.read(self.source)["tokens"]["refresh_token"], token)
        self.assertEqual(len(rows), 1)

    def test_newer_cloud_updates_current_and_survives_background_sync(self):
        self.service.sync_now()
        self.service.increment_traffic_by_access_token("access-local")
        callback = Mock()
        self.service.set_change_callback(callback)
        self.assertIn("较新的授权", self.apply())
        self.assertNotIn("_codex_session_manager", self.read(self.source))
        self.assert_current("remote")
        row = self.service.list_auth_rows()[0]
        self.assertEqual(row.note, "云端备注")
        self.assertEqual(row.traffic, 1)
        callback.assert_called()

    def test_older_cloud_keeps_current(self):
        self.remote["last_refresh"] = "2026-10-17T00:00:00Z"
        self.write(self.staged / "account.json", self.remote)
        self.apply()
        self.assert_current("local")

    def test_equal_or_invalid_times_keep_current(self):
        for timestamp in (self.local["last_refresh"], "", "invalid"):
            with self.subTest(timestamp=timestamp):
                self.write(self.staged / "account.json", self.auth("remote", timestamp))
                self.assertIn("冲突", self.apply())
                self.assert_current("local")

    def test_invalid_local_time_keeps_current(self):
        self.local["last_refresh"] = "invalid"
        self.write(self.source, self.local)
        self.apply()
        self.assert_current("local")

    def test_missing_cloud_account_preserves_current_without_switching(self):
        self.write(self.staged / "account.json", self.auth("other", "2026-10-19T00:00:00Z", "other"))
        (self.staged / "account.json").rename(self.staged / "other.json")
        self.assertIn("没有当前账号", self.apply())
        self.service.sync_now()
        rows = self.service.list_auth_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.account_id for row in rows if row.current], ["account"])

    def test_same_tokens_leave_current_file_untouched(self):
        self.write(self.staged / "account.json", self.local)
        original = self.source.read_bytes()
        self.apply()
        self.assertEqual(self.source.read_bytes(), original)
        self.assert_current("local")

    def test_missing_current_does_not_select_cloud_account(self):
        self.source.unlink()
        self.apply()
        self.assertFalse(self.source.exists())
        self.assertFalse(any(row.current for row in self.service.list_auth_rows()))

    def test_duplicate_account_aborts_before_applying(self):
        self.write(self.staged / "duplicate.json", self.remote)
        callback = Mock()
        with self.assertRaisesRegex(RuntimeError, "多个"):
            self.apply(callback)
        callback.assert_not_called()
        self.assertEqual(self.read(self.source), self.local)

    def test_background_sync_waits_until_application_finishes(self):
        started, finished = Event(), Event()

        def sync():
            started.set()
            self.service.sync_now()
            finished.set()

        worker = Thread(target=sync, daemon=True)

        def apply(version):
            worker.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.05))
            self.cloud._apply_pulled_version(version)

        try:
            self.apply(apply)
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assert_current("remote")

    def test_external_login_during_application_is_preserved(self):
        latest = self.auth("latest", "2026-10-19T01:00:00Z")

        def apply(version):
            self.cloud._apply_pulled_version(version)
            self.write(self.source, latest)

        self.assertIn("最新本地授权", self.apply(apply))
        self.assert_current("latest")

    def test_atomic_write_failure_keeps_current_and_releases_lock(self):
        with patch.object(Path, "replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.read(self.source), self.local)
        self.assertEqual(list(self.source.parent.glob("*.tmp")), [])
        self.service.sync_now()
        self.assert_current("local")

    def test_download_uses_latest_login_and_coordinates_application(self):
        client = Mock()
        prefix = "20261019_1/"

        def download(_bucket, _key, destination):
            self.write(Path(destination), self.remote)
            self.write(self.source, self.auth("login", "2026-10-19T02:00:00Z"))

        client.download_file.side_effect = download
        with patch.object(self.cloud, "_create_client", return_value=client), patch.object(
            self.cloud, "_list_object_keys", return_value=[prefix + "auth/account.json"]
        ):
            count = self.cloud.pull_version(
                prefix,
                apply_version=lambda version: self.service.apply_cloud_version(version, self.cloud._apply_pulled_version),
            )
        self.assertEqual(count, 1)
        self.assert_current("login")

    def test_ui_refreshes_local_state_when_version_listing_fails(self):
        from app.ui.proxy_window import CloudSyncAction, ProxyWindow

        window = ProxyWindow.__new__(ProxyWindow)
        window.auth_sync_service = self.service
        window.auth_usage_service = Mock()
        window._post_ui = lambda callback: callback()
        window._set_cloud_sync_buttons_state = Mock()
        window._render_cloud_sync_versions = Mock()
        window._reload_config_from_disk = Mock()
        window.refresh_auth_files = Mock()
        window._recompute_auto_load_target = Mock()
        window._cloud_storage_window = None
        cloud = Mock()
        cloud.pull_version.side_effect = lambda _prefix, apply_version: apply_version(self.version)
        cloud._apply_pulled_version = self.cloud._apply_pulled_version
        cloud.list_versions.side_effect = RuntimeError("network unavailable")
        with patch("app.ui.proxy_window.messagebox.showinfo") as showinfo, patch(
            "app.ui.proxy_window.messagebox.showerror"
        ) as showerror:
            window._cloud_sync_worker(cloud, CloudSyncAction.PULL, Mock(prefix="20261019_1/"))
        showerror.assert_not_called()
        self.assertIn("版本列表刷新失败", showinfo.call_args.args[1])
        window.refresh_auth_files.assert_called_once()
        window._recompute_auto_load_target.assert_called_once()
        window.auth_usage_service.request_refresh.assert_called_once()
        self.assertFalse(window._cloud_sync_running)
        self.assert_current("remote")


if __name__ == "__main__":
    unittest.main()
