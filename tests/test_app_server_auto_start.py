import threading
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.services.app_server_mqtt_service import (
    AppServerMqttService, AppServerTarget, MqttSettings,
)
from app.ui.app_server_mqtt_window import AppServerMqttWindow
from app.ui.proxy_window import ProxyWindow


MODULE = 'app.services.app_server_mqtt_service'


class AutoStartAcceptance(unittest.TestCase):
    """只模拟进程与注入入口，不接触真实客户端或 MQTT 端口。"""

    def setUp(self):
        self.service = AppServerMqttService()
        self.exe = Path('C:/isolated/ChatGPT.exe')
        self.target = AppServerTarget(202, 102)
        self.parent = Mock(pid=101)
        self.parent.create_time.return_value = 101
        self.parent.exe.return_value = str(self.exe)
        self.process = Mock()
        self.process.parents.return_value = [self.parent]
        self.settings = MqttSettings(port=21883)
        self.patches = [
            patch(f'{MODULE}.discover_app_servers', return_value=[self.target]),
            patch(f'{MODULE}.psutil.Process', return_value=self.process),
            patch(f'{MODULE}.MqttSettingsStore.load', return_value=self.settings),
            patch.object(self.service, '_worker'),
        ]
        self.discover, _, self.load, self.worker = [p.start() for p in self.patches]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.service.stop(wait=True)
        for p in reversed(self.patches):
            p.stop()

    def launch(self, timeout=0.1):
        self.service.start_after_launch(self.exe, 101, 100, timeout=timeout, interval=0.002)

    def finish(self):
        self.service._launch_thread.join(timeout=2)
        self.assertFalse(self.service.busy)

    def test_delayed_process_reuses_injection_with_saved_settings(self):
        self.discover.side_effect = [[], [], [self.target]]
        self.launch()
        self.finish()
        self.worker.assert_called_once_with(self.settings, self.target)
        self.assertEqual(self.service.target, self.target)
        self.assertEqual(self.service.settings, self.settings)
        self.assertGreaterEqual(self.discover.call_count, 3)

    def test_old_or_unrelated_process_is_not_injected(self):
        self.parent.pid = 999
        self.parent.exe.return_value = 'C:/other/ChatGPT.exe'
        self.discover.return_value = [AppServerTarget(201, 99), self.target]
        self.launch(timeout=0.01)
        self.finish()
        self.worker.assert_not_called()
        self.assertEqual(self.service.status, '自动注入失败')

    def test_multiple_matching_processes_are_not_guessed(self):
        self.discover.return_value = [self.target, AppServerTarget(203, 103)]
        self.launch(timeout=0.01)
        self.finish()
        self.worker.assert_not_called()
        self.assertIn('唯一', self.service.error)

    def test_stop_cancels_wait_and_prevents_injection(self):
        probing = threading.Event()

        def discover():
            probing.set()
            return []

        self.discover.side_effect = discover
        self.launch(timeout=1)
        self.assertTrue(probing.wait(1))
        self.assertTrue(self.service.busy)
        with self.assertRaises(RuntimeError):
            self.service.start(self.settings, self.target)
        self.service.stop(wait=True)
        self.worker.assert_not_called()
        self.assertEqual(self.service.status, '已停止')

    def test_restart_waits_for_previous_injection_cleanup(self):
        entered = threading.Event()
        release = threading.Event()

        def old_worker():
            entered.set()
            self.service._stop.wait(1)
            release.wait(1)

        self.service._thread = threading.Thread(target=old_worker)
        self.service._thread.start()
        self.assertTrue(entered.wait(1))
        try:
            self.launch(timeout=1)
            self.worker.assert_not_called()
        finally:
            release.set()
        self.finish()
        self.worker.assert_called_once_with(self.settings, self.target)
        self.assertFalse(self.service._stop.is_set())

    def test_new_launch_cancels_previous_discovery(self):
        probing = threading.Event()

        def discover():
            probing.set()
            return []

        self.discover.side_effect = discover
        self.launch(timeout=1)
        old_thread = self.service._launch_thread
        self.assertTrue(probing.wait(1))
        self.discover.side_effect = None
        self.launch()
        self.finish()
        self.assertFalse(old_thread.is_alive())
        self.worker.assert_called_once()

    def test_settings_error_restores_idle_state(self):
        self.load.side_effect = ValueError('配置读取失败')
        self.launch()
        self.finish()
        self.worker.assert_not_called()
        self.assertIn('配置读取失败', self.service.error)

    def test_stopping_restart_keeps_old_cleanup_busy(self):
        entered, release = threading.Event(), threading.Event()

        def worker(*_):
            entered.set()
            release.wait(2)

        self.worker.side_effect = worker
        self.launch(timeout=1)
        self.assertTrue(entered.wait(1))
        old_thread = self.service._launch_thread
        try:
            self.launch(timeout=1)
            self.service.stop()
            self.service._launch_thread.join(1)
            self.assertTrue(self.service.busy)
        finally:
            release.set()
            old_thread.join(1)
        self.assertFalse(self.service.busy)
        self.worker.assert_called_once()


class RemoteWindowAcceptance(unittest.TestCase):
    def test_waiting_attached_and_stopped_share_service_state(self):
        interpreter = tk.Tcl()
        window = AppServerMqttWindow.__new__(AppServerMqttWindow)
        window.service = service = AppServerMqttService()
        window.window = Mock()
        window._refresh_job = None
        window._was_busy = False
        window._displayed_settings = None
        window._last_events = ()
        for name in ('port', 'host', 'username', 'password', 'target', 'connection', 'status'):
            setattr(window, name, tk.StringVar(interpreter, value='未保存'))
        for name in ('refresh_button', 'save_button', 'start_button', 'stop_button', 'targets'):
            setattr(window, name, Mock())
        window.entries = [Mock()]
        window.refresh = Mock()
        service._thread = Mock()
        service._thread.is_alive.return_value = True
        service.settings = MqttSettings(port=21883)
        service.status = '等待 App Server'
        window.update_status()
        self.assertEqual(window.target.get(), '')
        self.assertEqual(window.port.get(), '21883')
        self.assertIn('等待 App Server', window.status.get())
        window.start_button.configure.assert_called_with(state='disabled')
        window.stop_button.configure.assert_called_with(state='normal')
        service.target = AppServerTarget(202, 102)
        service.status = '已附加'
        window.update_status()
        self.assertEqual(window.target.get(), '202')
        service._thread.is_alive.return_value = False
        service.status = '已停止'
        window.update_status()
        window.refresh.assert_called_once()
        window.start_button.configure.assert_called_with(state='normal')
        window.stop_button.configure.assert_called_with(state='disabled')


class DesktopLaunchAcceptance(unittest.TestCase):
    def test_normal_and_packaged_launch_schedule_auto_injection(self):
        for app_id in (None, 'Isolated!App'):
            with self.subTest(app_id=app_id):
                window = ProxyWindow.__new__(ProxyWindow)
                window.service = Mock()
                window.service.process.poll.return_value = None
                window.service.config.port = 8080
                window.service.ensure_launch_permissions.return_value = (True, '')
                window.codex_retry_service = Mock(debugging_enabled=False)
                window.codex_local_config_service = Mock()
                window.codex_local_config_service.set_proxy_environment.return_value = (True, '')
                window._app_server_mqtt = Mock()
                window._is_cli_codex_executable = Mock(return_value=False)
                window._is_relay_active = Mock(return_value=False)
                row = Mock(path='C:/isolated/ChatGPT.exe')
                with patch('app.ui.proxy_window.packaged_app_id', return_value=app_id), \
                        patch('app.ui.proxy_window.Path.exists', return_value=True), \
                        patch('app.ui.proxy_window.psutil.process_iter', return_value=[]), \
                        patch('app.ui.proxy_window.time.time', return_value=100), \
                        patch('app.ui.proxy_window.subprocess.Popen') as popen, \
                        patch('app.ui.proxy_window.activate_packaged_app', return_value=102) as activate:
                    popen.return_value.pid = 101
                    self.assertTrue(window._launch_codex_process(row))
                    window._app_server_mqtt.start_after_launch.assert_called_once_with(
                        Path(row.path), 102 if app_id else 101, 100)
                    if app_id:
                        popen.assert_not_called()
                        activate.assert_called_once()
                    else:
                        popen.assert_called_once()
                        activate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
