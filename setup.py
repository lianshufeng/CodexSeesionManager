import sys
import os
from pathlib import Path

from cx_Freeze import setup, Executable

project_root = Path(__file__).resolve().parent
site_packages_dir = Path(sys.prefix) / "Lib" / "site-packages"
from app.services.response_token_speed_service import prepare_tokenizer_cache

tokenizer_cache = project_root / "app/services/tokenizer_cache"
prepare_tokenizer_cache(tokenizer_cache)
include_files = [
    (str(tokenizer_cache), "lib/app/services/tokenizer_cache"),
    ("app/services/response_token_speed_service.py", "response_token_speed_service.py"),
    ("app/services/response_content_counter.py", "response_content_counter.py"),
    ("app/services/response_payload_reader.py", "response_payload_reader.py"),
    ("app/services/response_usage_ledger.py", "response_usage_ledger.py"),
    ("app/services/proxy_logger_addon.py", "proxy_logger_addon.py"),
    ("icon/icon.ico", "icon/icon.ico"),
    ("icon/tray_icon.ico", "icon/tray_icon.ico"),
]
mitmproxy_dir = project_root / ".mitmproxy"
if mitmproxy_dir.exists():
    include_files.append((str(mitmproxy_dir), ".mitmproxy"))

mitmproxy_windows_dir = site_packages_dir / "mitmproxy_windows"
if mitmproxy_windows_dir.exists():
    for filename in ("windows-redirector.exe", "WinDivert.dll", "WinDivert.lib", "WinDivert64.sys"):
        file_path = mitmproxy_windows_dir / filename
        if file_path.exists():
            include_files.append((str(file_path), f"lib/mitmproxy_windows/{filename}"))

setup(
    name="codex_session",
    version="1.0",
    description="Codex Session Manager",
    executables=[
        Executable("mitmdump_entry.py", target_name="mitmdump.exe", base="console"),
        Executable("main.py", target_name="codex_session.exe", base="gui", icon="icon/icon.ico"),
    ],
    options={
        "build_exe": {
            "build_exe": os.environ.get("BUILD_EXE_DIR", str(project_root / "build" / "exe.win-amd64-3.11")),
            "include_files": include_files,
            "include_msvcr": True,
            "packages": ["mitmproxy", "mitmproxy_rs", "mitmproxy_windows", "psutil", "websocket", "pystray", "PIL", "app", "tiktoken", "tiktoken_ext"],
            "excludes": []  # 不需要的库可以排除掉
        }
    }
)
