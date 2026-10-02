from __future__ import annotations

import atexit
import io
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable


class _NullStream(io.TextIOBase):
    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("stream has no file descriptor")

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "strict"


class _TeeStream(io.TextIOBase):
    def __init__(self, manager: "LogService", original: io.TextIOBase | None, name: str) -> None:
        self._manager = manager
        self._original = original
        self._name = name

    def write(self, text: str) -> int:
        if self._original is not None:
            try:
                self._original.write(text)
            except UnicodeEncodeError:
                encoding = getattr(self._original, "encoding", "utf-8") or "utf-8"
                buffer = getattr(self._original, "buffer", None)
                if buffer is not None:
                    buffer.write(text.encode(encoding, errors="replace"))
        self._manager._write(self._name, text)
        return len(text)

    def flush(self) -> None:
        if self._original is not None:
            self._original.flush()

    def isatty(self) -> bool:
        if self._original is None:
            return False
        return bool(getattr(self._original, "isatty", lambda: False)())

    def fileno(self) -> int:
        if self._original is None:
            raise OSError("stream has no file descriptor")
        return self._original.fileno()

    @property
    def encoding(self) -> str:
        if self._original is None:
            return "utf-8"
        return getattr(self._original, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        if self._original is None:
            return "strict"
        return getattr(self._original, "errors", "strict")


class LogService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._installed = False
        self._stdout_original: io.TextIOBase | None = None
        self._stderr_original: io.TextIOBase | None = None
        self._stdout_proxy: _TeeStream | None = None
        self._stderr_proxy: _TeeStream | None = None
        self._ui_callback: Callable[[str], None] | None = None
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._last_line = ""
        self._log_root = self._resolve_log_root()

    def _resolve_log_root(self) -> Path:
        argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
        if argv0 is not None:
            return argv0.resolve().parent / "logs"
        return Path(__file__).resolve().parents[2] / "logs"

    def install(self) -> None:
        with self._lock:
            if self._installed:
                return
            self._stdout_original = sys.stdout if sys.stdout is not None else sys.__stdout__
            self._stderr_original = sys.stderr if sys.stderr is not None else sys.__stderr__
            stdout_target: io.TextIOBase | None = self._stdout_original or _NullStream()
            stderr_target: io.TextIOBase | None = self._stderr_original or _NullStream()
            self._stdout_proxy = _TeeStream(self, stdout_target, "stdout")
            self._stderr_proxy = _TeeStream(self, stderr_target, "stderr")
            sys.stdout = self._stdout_proxy
            sys.stderr = self._stderr_proxy
            self._log_root.mkdir(parents=True, exist_ok=True)
            self._installed = True
            atexit.register(self.restore)

    def restore(self) -> None:
        with self._lock:
            if not self._installed:
                return
            if self._stdout_original is not None:
                sys.stdout = self._stdout_original
            if self._stderr_original is not None:
                sys.stderr = self._stderr_original
            self._installed = False

    def set_ui_callback(self, callback: Callable[[str], None] | None) -> None:
        with self._lock:
            self._ui_callback = callback
            last_line = self._last_line
        if callback is not None and last_line:
            callback(last_line)

    def _write(self, stream_name: str, text: str) -> None:
        if not text:
            return

        with self._lock:
            buffer = self._stdout_buffer if stream_name == "stdout" else self._stderr_buffer
            buffer += text
            parts = buffer.splitlines(keepends=True)
            pending = ""
            for part in parts:
                if part.endswith("\n") or part.endswith("\r"):
                    line = part.rstrip("\r\n")
                    if line:
                        self._last_line = line
                        self._write_line_to_disk(stream_name, line)
                        self._emit_ui_callback(line)
                else:
                    pending = part
            if stream_name == "stdout":
                self._stdout_buffer = pending
            else:
                self._stderr_buffer = pending

    def _write_line_to_disk(self, stream_name: str, line: str) -> None:
        if not self._should_persist_line(stream_name, line):
            return

        now = datetime.now()
        month_dir = self._log_root / now.strftime("%Y%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        log_path = month_dir / f"{now.strftime('%Y%m%d')}.log"
        with log_path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(line + "\n")

    def _should_persist_line(self, stream_name: str, line: str) -> bool:
        if stream_name == "stderr":
            return True
        if line.startswith(("[ProxyService]", "[AuthUsage]", "[AuthSync]", "[AutoLoad]", "[ProxyWindow]", "[TokenSpeed]")):
            return True
        if line.startswith("[ProxyFlow]"):
            noisy_markers = ("请求头", "请求体", "响应头", "响应体", "流量:")
            return not any(marker in line for marker in noisy_markers)
        keywords = (
            "启动",
            "停止",
            "刷新",
            "失败",
            "错误",
            "异常",
            "警告",
            "切换",
            "删除",
            "安装证书",
            "请求失败",
            "额度刷新失败",
            "代理插件退出",
        )
        return any(keyword in line for keyword in keywords)

    def _emit_ui_callback(self, line: str) -> None:
        callback = self._ui_callback
        if callback is None:
            return
        try:
            callback(line)
        except Exception:
            pass


_log_service = LogService()


def get_log_service() -> LogService:
    return _log_service
