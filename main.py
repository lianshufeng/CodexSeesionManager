import ctypes
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

_MB_YESNO = 0x00000004
_MB_OK = 0x00000000
_MB_ICONWARNING = 0x00000030
_MB_DEFBUTTON2 = 0x00000100
_IDYES = 6


def _get_resource_path(*parts: str) -> str:
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = Path(__file__).resolve().parent
    return str(base_dir.joinpath(*parts))


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _show_admin_prompt() -> bool:
    try:
        result = ctypes.windll.user32.MessageBoxW(
            None,
            "当前不是管理员身份运行。\n\n点击“是”将以管理员身份重新启动，点击“否”将直接退出。",
            "需要管理员权限",
            _MB_YESNO | _MB_ICONWARNING | _MB_DEFBUTTON2,
        )
        return result == _IDYES
    except (AttributeError, OSError):
        return False


def _restart_as_admin() -> bool:
    executable = sys.executable
    if getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        script_path = os.path.abspath(sys.argv[0])
        params = subprocess.list2cmdline([script_path, *sys.argv[1:]])
    try:
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
        return result > 32
    except (AttributeError, OSError):
        return False


def _install_exception_hooks(root) -> None:
    def log_exception(prefix: str, exc_type, exc_value, exc_traceback) -> None:
        if exc_type is KeyboardInterrupt:
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        print(f"[Crash] {prefix}\n{detail}", flush=True)

    def handle_thread_exception(args: threading.ExceptHookArgs) -> None:
        log_exception(f"线程异常: {args.thread.name}", args.exc_type, args.exc_value, args.exc_traceback)

    def handle_tk_exception(exc_type, exc_value, exc_traceback) -> None:
        log_exception("Tk 回调异常", exc_type, exc_value, exc_traceback)

    sys.excepthook = lambda exc_type, exc_value, exc_traceback: log_exception(
        "未捕获异常",
        exc_type,
        exc_value,
        exc_traceback,
    )
    threading.excepthook = handle_thread_exception
    root.report_callback_exception = handle_tk_exception


def main() -> None:
    from app.utils.path_utils import app_root
    from app.utils.single_instance import SingleInstance

    single_instance = SingleInstance(app_root())
    if single_instance.notify_existing(timeout=0.2):
        return
    if not single_instance.acquire():
        if not single_instance.notify_existing():
            ctypes.windll.user32.MessageBoxW(
                None,
                "程序已在当前目录运行。",
                "启动提示",
                _MB_OK | _MB_ICONWARNING,
            )
        return

    if not _is_admin():
        if _show_admin_prompt():
            single_instance.close()
            if _restart_as_admin():
                return
            ctypes.windll.user32.MessageBoxW(
                None,
                "无法以管理员身份重新启动程序。",
                "启动失败",
                _MB_OK | _MB_ICONWARNING,
            )
        else:
            single_instance.close()
        sys.exit(1)

    try:
        from app.services.log_service import get_log_service

        log_service = get_log_service()
        log_service.install()

        from app.ui.proxy_window import ProxyWindow
        import tkinter as tk

        root = tk.Tk()
        _install_exception_hooks(root)
        icon_path = _get_resource_path("icon", "icon.ico")
        if os.path.exists(icon_path):
            try:
                root.iconbitmap(icon_path)
            except tk.TclError:
                pass
        window = ProxyWindow(root)
        single_instance.start_restore_server(window.request_restore_window)
        root.mainloop()
    finally:
        single_instance.close()


if __name__ == "__main__":
    main()
