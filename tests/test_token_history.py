import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from app.services.token_history_service import TokenHistoryService
from app.ui.token_history_window import plot_segments
from app.ui.proxy_window import ProxyWindow


class HistoryTests(unittest.TestCase):
    def test_pending_read_exit_flush_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            now = int(time.time())
            service = TokenHistoryService(path)
            try:
                service.record(12.3, now)
                service.record(20, now)
                service.record(0, now + 1)
                self.assertEqual(service.read_async(now, now+1).result(timeout=5), [(now, 20), (now+1, 0)])
                self.assertEqual(service.recent(now), [(now, 20), (now+1, 0)])
            finally:
                service.stop()
            self.assertFalse(service._thread.is_alive())
            reopened = TokenHistoryService(path)
            try:
                self.assertEqual(reopened.read_async(now, now+1).result(timeout=5), [(now, 20), (now+1, 0)])
            finally:
                reopened.stop()

    def test_retention_and_batch_flush(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            now = int(time.time())
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE samples(timestamp INTEGER PRIMARY KEY, speed REAL NOT NULL)")
                connection.execute("INSERT INTO samples VALUES (?, 1)", (now-8*86400,))
                connection.commit()
            service = TokenHistoryService(path, flush_seconds=.1)
            try:
                service.record(25, now)
                deadline = time.monotonic()+5
                while time.monotonic()<deadline:
                    with closing(sqlite3.connect(path)) as connection:
                        rows = connection.execute("SELECT * FROM samples").fetchall()
                    if rows == [(now, 25)]:
                        break
                    time.sleep(.05)
                self.assertEqual(rows, [(now, 25)])
            finally:
                service.stop()

    def test_storage_failure_and_stopped_reads_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            service = TokenHistoryService(Path(directory))
            try:
                with self.assertRaises(Exception):
                    service.read_async(0, int(time.time())).result(timeout=5)
            finally:
                service.stop()
            self.assertTrue(service.error)
            with self.assertRaises(RuntimeError):
                service.read_async(0, 10).result(timeout=1)

    def test_compression_keeps_peak_and_gaps(self):
        samples = [(i, 100 if i == 50 else 0) for i in range(100)] + [(200, 2), (201, 3)]
        segments = plot_segments(samples, 0, 300, 10)
        self.assertEqual(len(segments), 2)
        self.assertIn((50, 100), segments[0])
        self.assertLess(len(segments[0]), 40)

    def test_idle_is_recorded_and_popup_is_reused(self):
        window = ProxyWindow.__new__(ProxyWindow)
        window._closing = False
        window.root = Mock()
        window._token_speed_updated_at = 0
        window._token_speed_value = 25
        window._token_speed_var = Mock()
        window._refresh_tray_icon_tooltip = Mock()
        window._token_history_service = Mock()
        window._tick_token_speed()
        window._token_history_service.record.assert_called_once_with(0)
        window._token_history_window = Mock()
        window._show_token_history()
        window._token_history_window.show.assert_called_once()


if __name__ == "__main__":
    unittest.main()
