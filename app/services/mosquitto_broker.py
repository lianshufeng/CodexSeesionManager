from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shutil
import socket
import subprocess
from pathlib import Path

from app.utils.path_utils import app_root


def find_mosquitto() -> Path | None:
    candidates = [
        app_root() / 'mosquitto' / 'mosquitto.exe',
        Path(os.environ.get('LOCALAPPDATA', '')) / 'CodexSessionManager' / 'mosquitto' / 'mosquitto.exe',
        Path(os.environ.get('ProgramFiles', '')) / 'mosquitto' / 'mosquitto.exe',
    ]
    if executable := shutil.which('mosquitto'):
        candidates.append(Path(executable))
    return next((path for path in candidates if path.is_file()), None)


class MosquittoBroker:
    def __init__(self, directory: Path, port: int, username: str, password: str, host: str = '0.0.0.0') -> None:
        self.directory = directory
        self.port = port
        self.host = host
        self.process: subprocess.Popen | None = None
        # Mosquitto password file format 7 is PBKDF2-HMAC-SHA512.
        iterations = 101000
        salt = os.urandom(12)
        digest = hashlib.pbkdf2_hmac('sha512', password.encode('utf-8'), salt, iterations)
        salt64 = base64.b64encode(salt).decode('ascii')
        digest64 = base64.b64encode(digest).decode('ascii')
        (directory / 'users').write_text(f'{username}:$7${iterations}${salt64}${digest64}\n', encoding='utf-8')
        (directory / 'mosquitto.conf').write_text(
            f'listener {port} {host}\nallow_anonymous false\npassword_file users\n'
            'persistence false\nconnection_messages false\nlog_dest stderr\n', encoding='utf-8',
        )

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    async def start(self) -> None:
        executable = find_mosquitto()
        if executable is None:
            raise RuntimeError('找不到 Mosquitto，请安装 Eclipse Mosquitto 或将其放入程序目录下的 mosquitto 文件夹')
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind((self.host, self.port))
            except OSError as exc:
                raise RuntimeError(f'MQTT 端口 {self.port} 无法监听：{exc}') from exc
        log_path = self.directory / 'broker.log'
        with log_path.open('wb') as log:
            self.process = subprocess.Popen(
                [str(executable.resolve()), '-c', 'mosquitto.conf'], cwd=self.directory,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        try:
            for _ in range(100):
                if self.process.poll() is not None:
                    detail = log_path.read_bytes()[-2048:].decode('utf-8', errors='replace').strip()
                    raise RuntimeError(f'Mosquitto 启动失败：{detail or self.process.returncode}')
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection('127.0.0.1' if self.host == '0.0.0.0' else self.host, self.port), .2)
                except (OSError, TimeoutError):
                    pass
                else:
                    writer.close()
                    await writer.wait_closed()
                    return
                await asyncio.sleep(.05)
            raise RuntimeError('Mosquitto 启动超时')
        except BaseException:
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 2)
