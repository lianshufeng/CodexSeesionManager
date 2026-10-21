from __future__ import annotations

import ast
import base64
import hashlib
from dataclasses import dataclass
import os
import locale
import re
import shutil
import socket
import subprocess
import sys
import time
from threading import Thread
from pathlib import Path

import psutil


@dataclass
class ProxyConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    upstream_proxy: str = "127.0.0.1:1088"
    use_upstream_proxy: bool = True


class ProxyService:
    def __init__(self, config: ProxyConfig | None = None) -> None:
        self.config = config or ProxyConfig()
        self.process: subprocess.Popen[str] | None = None
        self.certificate_dir().mkdir(parents=True, exist_ok=True)

    def _log(self, message: str) -> None:
        print(f"[ProxyService] {message}", flush=True)

    def _decode_output(self, output: bytes | str | None) -> str:
        if output is None:
            return ""
        if isinstance(output, str):
            return output
        preferred_encoding = locale.getpreferredencoding(False) or "utf-8"
        for encoding in (preferred_encoding, "utf-8", "mbcs", "cp936"):
            try:
                return output.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return output.decode("utf-8", errors="replace")

    def _app_root(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parents[2]

    def certificate_dir(self) -> Path:
        return self._app_root() / ".mitmproxy"

    def certificate_files(self) -> dict[str, Path]:
        cert_dir = self.certificate_dir()
        return {
            "cer": cert_dir / "mitmproxy-ca-cert.cer",
            "pem": cert_dir / "mitmproxy-ca-cert.pem",
            "p12": cert_dir / "mitmproxy-ca-cert.p12",
            "ca_pem": cert_dir / "mitmproxy-ca.pem",
            "ca_p12": cert_dir / "mitmproxy-ca.p12",
        }

    def ensure_launch_permissions(self, exe_path: Path) -> tuple[bool, str]:
        if not exe_path.exists():
            return False, f"找不到文件 {exe_path}"
        if not exe_path.is_file():
            return False, f"不是可执行文件 {exe_path}"
        ok, message = self._grant_launch_permissions(exe_path)
        if not ok:
            return False, message
        return True, ""

    def _grant_launch_permissions(self, exe_path: Path) -> tuple[bool, str]:
        user = os.environ.get("USERNAME", "")
        if not user:
            return False, "无法识别当前用户，无法设置执行权限。"

        target_path = exe_path.parent
        if not target_path.exists():
            return False, f"找不到目标目录 {target_path}"
        self._log(f"设置权限目标: {target_path}")
        result = subprocess.run(
            ["icacls", str(target_path), "/grant", f"{user}:(RX)"],
            capture_output=True,
        )
        output = self._decode_output(result.stdout).strip() or self._decode_output(result.stderr).strip()
        self._log(f"icacls output: {output}")
        return True, ""

    def find_available_port(self, start_port: int | None = None, max_tries: int = 100) -> int:
        port = start_port or self.config.port
        for _ in range(max_tries):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind((self.config.host, port))
                except OSError:
                    port += 1
                    continue
                return port
        raise RuntimeError("未找到可用端口")

    def install_certificate(self) -> tuple[bool, str]:
        cert_path = self.certificate_files()["cer"]
        if not cert_path.exists():
            return False, "未找到 mitmproxy 证书，先启动一次代理生成证书。"
        command = [
            "certutil",
            "-user",
            "-addstore",
            "Root",
            str(cert_path),
        ]
        result = subprocess.run(command, capture_output=True)
        if result.returncode != 0:
            message = self._decode_output(result.stderr).strip() or self._decode_output(result.stdout).strip() or "证书安装失败"
            return False, message
        self._log(f"证书安装成功: {cert_path}")
        return True, "证书已安装到当前用户的受信任根证书存储。"

    def certificate_thumbprint(self) -> str:
        cert_path = self.certificate_files()["cer"]
        if not cert_path.exists():
            return ""
        data = cert_path.read_bytes()
        if b"-----BEGIN CERTIFICATE-----" in data:
            text = data.decode("utf-8", errors="ignore")
            body = "".join(
                line.strip()
                for line in text.splitlines()
                if line and "CERTIFICATE" not in line
            )
            try:
                data = base64.b64decode(body.encode("ascii"), validate=False)
            except (ValueError, UnicodeEncodeError):
                return ""
        return hashlib.sha1(data).hexdigest().upper()

    def is_certificate_installed(self) -> bool:
        thumbprint = self.certificate_thumbprint()
        if not thumbprint:
            return False
        thumbprint = thumbprint.replace("'", "''")
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "$cert = Get-ChildItem -Path Cert:\\CurrentUser\\Root "
                    + "| Where-Object { $_.Thumbprint -eq '"
                    + thumbprint
                    + "' } "
                    + "| Select-Object -First 1; "
                    + "if ($null -ne $cert) { exit 0 } else { exit 1 }"
                ),
            ],
            capture_output=True,
        )
        return result.returncode == 0
    def _resolve_mitmdump_command(self) -> list[str]:
        candidates: list[Path] = []
        self._log(
            f"运行环境: frozen={getattr(sys, 'frozen', False)} argv0={sys.argv[0]} executable={sys.executable}"
        )
        candidates.append(Path(sys.argv[0]).resolve().with_name("mitmdump.exe"))
        if getattr(sys, "frozen", False):
            candidates.append(Path(__file__).resolve().parents[3] / "mitmdump.exe")
        candidates.append(Path(sys.executable).with_name("mitmdump.exe"))
        if not getattr(sys, "frozen", False):
            candidates.append(Path(sys.prefix) / "Scripts" / "mitmdump.exe")
        for script in candidates:
            if script.exists():
                return [str(script)]
        located = shutil.which("mitmdump") or shutil.which("mitmdump.exe")
        if located:
            return [located]
        raise RuntimeError("未找到 mitmdump.exe，请先安装 mitmproxy")

    def _proxy_logger_script(self) -> Path:
        candidate = Path(sys.argv[0]).resolve().with_name("proxy_logger_addon.py")
        if candidate.exists():
            return candidate
        return Path(__file__).with_name("proxy_logger_addon.py")

    def _process_environment(self) -> dict[str, str]:
        return os.environ.copy()

    def _extra_arguments(self) -> list[str]:
        return []

    def _normalize_upstream_proxy(self, upstream: str) -> str:
        value = upstream.strip()
        if value.startswith("http://"):
            value = value.removeprefix("http://")
        elif value.startswith("https://"):
            value = value.removeprefix("https://")
        return value

    def _is_listen_port_available(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((self.config.host, self.config.port))
            except OSError:
                return False
            return True

    def _pump_process_output(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            print(line, end="", flush=True)
    def run(self) -> tuple[bool, str]:
        self._log(f"准备启动代理: {self.config.host}:{self.config.port}")
        if not self._is_listen_port_available():
            message = f"监听端口 {self.config.port} 已被占用，请先关闭占用该端口的程序后重试"
            self._log(f"代理启动前检查失败: {message}")
            return False, message
        self.certificate_dir().mkdir(parents=True, exist_ok=True)
        addon_script = self._proxy_logger_script()
        upstream = self._normalize_upstream_proxy(self.config.upstream_proxy)
        cert_state = {name: path.exists() for name, path in self.certificate_files().items()}
        self._log(f"证书状态: {cert_state}")
        argv = self._resolve_mitmdump_command() + [
            "-s",
            str(addon_script),
            "--set",
            f"confdir={self.certificate_dir()}",
            "--listen-host",
            self.config.host,
            "--listen-port",
            str(self.config.port),
        ]
        argv.extend(self._extra_arguments())
        if self.config.use_upstream_proxy and upstream:
            argv[1:1] = ["--mode", f"upstream:http://{upstream}"]
            self._log(f"二级代理: http://{upstream}")
        else:
            self._log("二级代理: 直连")
        self._log(f"日志插件: {addon_script}")
        self._log("启动命令: " + " ".join(argv))
        env = self._process_environment()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        Thread(target=self._pump_process_output, args=(self.process,), daemon=True).start()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self.process.poll() is not None:
                return_code = self.process.returncode
                self.process = None
                message = f"代理启动失败，退出码 {return_code}"
                self._log(f"代理提前退出: {message}")
                return False, message
            time.sleep(0.1)
        self._log("代理启动成功")
        return True, ""

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.stop_process_tree(self.process.pid)
        else:
            self._stop_matching_processes()
        self.process = None

    def stop_process_tree(self, pid: int) -> None:
        try:
            parent = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            self._stop_matching_processes()
            return

        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        _, alive = psutil.wait_procs(children, timeout=3)
        for child in alive:
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        try:
            parent.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        try:
            parent.wait(timeout=3)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            try:
                parent.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def _stop_matching_processes(self) -> None:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if "mitmdump" not in cmdline and "mitmproxy.tools.main" not in cmdline:
                continue
            try:
                children = proc.children(recursive=True)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                children = []
            for child in children:
                try:
                    child.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            _, alive = psutil.wait_procs(children, timeout=3)
            for child in alive:
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            try:
                proc.wait(timeout=3)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                try:
                    proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
