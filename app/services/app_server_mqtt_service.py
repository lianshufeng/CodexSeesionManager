from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import queue
import re
import tempfile
import threading
import time
import uuid
from collections import Counter, OrderedDict, deque
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import psutil

from app.services.mosquitto_broker import MosquittoBroker
from app.services.app_server_pipe_writer import AppServerPipeWriter
from app.services.file_transfer import FileTransfers, CAPABILITIES
from app.utils.path_utils import app_root


@dataclass(frozen=True)
class MqttSettings:
    port: int = 1883
    username: str = 'admin'
    password: str = 'admin@2026'
    host: str = '0.0.0.0'

    def validate(self) -> None:
        if ipaddress.ip_address(self.host).version != 4:
            raise ValueError('监听地址需要为 IPv4 地址')
        if not 1 <= self.port <= 65535:
            raise ValueError('端口必须在 1 到 65535 之间')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', self.username):
            raise ValueError('账号仅支持字母、数字、点、下划线和连字符')
        if not self.password or len(self.password.encode('utf-8')) > 256 or '\0' in self.password:
            raise ValueError('密码不能为空或超过 256 字节')


class MqttSettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_root() / 'data' / 'app_server_mqtt.json'

    def load(self) -> MqttSettings:
        if not self.path.exists():
            return MqttSettings()
        import win32crypt

        data = json.loads(self.path.read_text(encoding='utf-8'))
        password = win32crypt.CryptUnprotectData(bytes.fromhex(data['password']), None, None, None, 1)[1]
        settings = MqttSettings(data['port'], data['username'], password.decode('utf-8'), data.get('host', '0.0.0.0'))
        settings.validate()
        return settings

    def save(self, settings: MqttSettings) -> None:
        import win32crypt

        settings.validate()
        encrypted = win32crypt.CryptProtectData(settings.password.encode(), None, None, None, None, 1)
        payload = {'port': settings.port, 'username': settings.username, 'password': encrypted.hex(), 'host': settings.host}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.path)


@dataclass(frozen=True)
class AppServerTarget:
    pid: int
    created: float


def discover_app_servers() -> list[AppServerTarget]:
    return [AppServerTarget(info['pid'], info['create_time'])
            for process in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time'])
            if (info := process.info)['name'] and info['name'].lower() == 'codex.exe'
            and 'app-server' in (info['cmdline'] or ()) and info['create_time'] is not None]


class _ProbeCleanupError(RuntimeError):
    pass


class AppServerMqttService:
    RETRY_DELAYS = (0, 1, 2, 4, 8, 10)
    PROBE_TIMEOUT = 5
    PUBLISH_TIMEOUT = 5
    OBSERVED_QUEUE_BYTES = 16 * 1024 * 1024
    CAPTURE_QUEUE_CHUNKS = 4096
    CAPTURE_QUEUE_BYTES = 32 * 1024 * 1024

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.status = '未启动'
        self.error = ''
        self.count = 0
        self._reply_expiry = {}
        self.dropped = 0
        self.target: AppServerTarget | None = None
        self.broker_running = False
        self.attached = False
        self.writer_ready = False
        self._last_attach_succeeded = False
        self._parent_probe = None
        self._events: deque[tuple[str, str, str, str]] = deque(maxlen=200)
        self._events_lock = threading.Lock()

    def recent_events(self) -> tuple[tuple[str, str, str, str], ...]:
        with self._events_lock:
            return tuple(self._events)

    def _record(self, event: str, detail: str = '', thread_id: str = '') -> None:
        with self._events_lock:
            self._events.append((datetime.now().strftime('%H:%M:%S'), thread_id, event, detail))

    @contextmanager
    def _stage(self, label, target, attempt):
        started = time.monotonic()
        result = '完成'
        try:
            yield
        except BaseException as exc:
            result = f'失败 · {type(exc).__name__}'
            raise
        finally:
            self._record(label, f'PID {target.pid} · 尝试 {attempt} · {result} · {time.monotonic() - started:.3f} 秒')

    @asynccontextmanager
    async def _probe_deadline(self, label, timeout=None):
        timeout = self.PROBE_TIMEOUT if timeout is None else timeout
        try:
            async with asyncio.timeout(timeout):
                yield
        except TimeoutError as exc:
            raise TimeoutError(f'{label}超时（{timeout} 秒）') from exc

    async def _wait_probe_detach(self, session, target):
        task = asyncio.create_task(session.detach())
        try:
            while not self._stop.is_set():
                done, _ = await asyncio.wait((task,), timeout=2)
                if done:
                    try:
                        task.result()
                        return
                    except Exception as exc:
                        if session.is_detached:
                            return
                        self._record('探针卸载重试', f'PID {target.pid} · {type(exc).__name__}')
                        if await asyncio.to_thread(self._stop.wait, 10):
                            return
                        task = asyncio.create_task(session.detach())
                if session.is_detached:
                    return
                if self.status != '等待旧探针卸载':
                    self.status = '等待旧探针卸载'
                    self._record(self.status, f'PID {target.pid} · 等待旧探针释放')
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def _record_protocol_event(self, message: dict) -> None:
        method = message.get('method')
        params = message.get('params')
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        thread_id = params.get('threadId')
        thread_id = thread_id if isinstance(thread_id, str) else ''
        turn = params.get('turn')
        turn = turn if isinstance(turn, dict) else {}
        turn_id = turn.get('id') or params.get('turnId')
        turn_detail = f'轮次 {turn_id}' if isinstance(turn_id, str) else ''
        if method == 'turn/started':
            self._record('轮次开始', turn_detail, thread_id)
        elif method == 'turn/completed':
            status = turn.get('status')
            error = turn.get('error')
            detail = self._detail(turn_detail, error.get('message') if isinstance(error, dict) else '')
            self._record('轮次完成' if status == 'completed' else
                         '轮次失败' if status == 'failed' else
                         '轮次中断' if status == 'interrupted' else '轮次结束', detail, thread_id)
        elif method == 'thread/tokenUsage/updated':
            usage = params.get('tokenUsage')
            if isinstance(usage, dict):
                fields = (('inputTokens', '输入'), ('cachedInputTokens', '缓存'), ('outputTokens', '输出'),
                          ('reasoningOutputTokens', '推理输出'), ('totalTokens', '合计'))
                parts = []
                for key, label in (('last', '本次'), ('total', '累计')):
                    counts = usage.get(key)
                    if isinstance(counts, dict):
                        summary = '，'.join(f'{name} {counts[field]:,}' for field, name in fields
                                            if type(counts.get(field)) is int)
                        if summary:
                            parts.append(f'{label} {summary}')
                if parts:
                    self._record('Token 用量', self._detail(*parts), thread_id)
        elif method == 'account/rateLimits/updated':
            limits = params.get('rateLimits')
            if isinstance(limits, dict):
                parts = []
                for key in ('primary', 'secondary'):
                    window = limits.get(key)
                    if isinstance(window, dict) and type(window.get('usedPercent')) in (int, float):
                        minutes = window.get('windowDurationMins')
                        label = (f'{minutes // 60} 小时' if minutes % 60 == 0 else f'{minutes} 分钟'
                                 ) if type(minutes) is int and minutes > 0 else ('主窗口' if key == 'primary' else '次窗口')
                        parts.append(f'{label}已用 {window["usedPercent"]:g}%')
                if parts:
                    self._record('额度更新', self._detail(*parts))
        elif method == 'turn/plan/updated':
            plan = params.get('plan')
            if isinstance(plan, list):
                steps = [step for step in plan if isinstance(step, dict)]
                done = sum(step.get('status') == 'completed' for step in steps)
                current = next((step.get('step') for step in steps if step.get('status') == 'inProgress'), None)
                self._record('计划更新', self._detail(f'已完成 {done}/{len(steps)}', current or params.get('explanation')), thread_id)
        elif method in {'item/started', 'item/completed'}:
            self._record_item(params.get('item'), method == 'item/completed', thread_id)
        elif method in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                        'item/permissions/requestApproval'}:
            self._record('等待审批', self._detail(params.get('command'), params.get('reason')), thread_id)
        elif method == 'item/tool/requestUserInput':
            questions = params.get('questions')
            question = next((item.get('question') for item in questions if isinstance(item, dict)), None
                            ) if isinstance(questions, list) else None
            self._record('等待输入', self._detail(question), thread_id)
        elif method == 'thread/status/changed':
            state = params.get('status')
            if isinstance(state, dict) and isinstance(state.get('type'), str):
                names = {'idle': '会话空闲', 'active': '会话运行中',
                         'notLoaded': '会话未加载', 'systemError': '会话异常'}
                event = names.get(state.get('type'))
                if event:
                    self._record(event, thread_id=thread_id)
        elif method == 'error':
            error = params.get('error')
            self._record('运行错误', self._detail('Codex 将重试' if params.get('willRetry') else '',
                                                 error.get('message') if isinstance(error, dict) else ''), thread_id)

    @staticmethod
    def _detail(*parts: object) -> str:
        return ' · '.join(part for part in parts if isinstance(part, str) and part.strip())

    def _record_item(self, item: object, completed: bool, thread_id: str) -> None:
        if not isinstance(item, dict):
            return
        kind = item.get('type')
        status = item.get('status')
        result = '失败' if status in ('failed', 'declined') or item.get('success') is False else '结束'
        if kind == 'commandExecution':
            code = item.get('exitCode')
            result = '失败' if type(code) is int and code != 0 else result
            duration = item.get('durationMs')
            detail = self._detail(f'退出码 {code}' if type(code) is int else '',
                                  f'{duration:,} ms' if type(duration) is int else '',
                                  item.get('command'), item.get('cwd'))
            self._record(f'命令{result}' if completed else '执行命令', detail, thread_id)
        elif kind == 'fileChange' and completed:
            changes = item.get('changes')
            paths = [change['path'] for change in changes if isinstance(change, dict) and isinstance(change.get('path'), str)
                     ] if isinstance(changes, list) else []
            self._record(f'文件变更{result}', self._detail(f'{len(paths)} 个文件', *paths[:5]), thread_id)
        elif kind in ('mcpToolCall', 'dynamicToolCall') and completed:
            self._record(f'工具调用{result}', self._detail(item.get('server'), item.get('tool')), thread_id)
        elif kind in ('agentMessage', 'plan') and completed and item.get('text'):
            self._record('助手消息' if kind == 'agentMessage' else '计划摘要', self._detail(item.get('text')), thread_id)

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, settings: MqttSettings, target: AppServerTarget) -> None:
        if self.busy:
            raise RuntimeError('监听已经启动或正在停止')
        settings.validate()
        self._stop.clear()
        self.status, self.error = '正在启动', ''
        self.target = target
        self._record('正在启动', f'PID {target.pid} · MQTT 端口 {settings.port}')
        self.count = self.dropped = 0
        self._thread = threading.Thread(target=self._worker, args=(settings, target), daemon=True)
        self._thread.start()

    def stop(self, *, wait: bool = False) -> None:
        self._stop.set()
        if self.busy:
            self.status = '正在停止'
        if wait and self._thread is not None:
            self._thread.join(timeout=5)

    def _worker(self, settings: MqttSettings, target: AppServerTarget) -> None:
        try:
            asyncio.run(self._run(settings, target))
        except Exception as exc:
            self.error = f'{type(exc).__name__}：{exc}'
            if self.status != '目标进程已退出':
                self.status = ('已断开' if self.status in ('已附加', '正在重新附加', '等待旧探针卸载', '已断开')
                               else '启动失败')
            self._record(self.status, self.error)
        else:
            self.error = ''
            self.status = '已停止'
            self._record(self.status)
        finally:
            self.attached = self.broker_running = self.writer_ready = False

    @staticmethod
    def _same_process(target: AppServerTarget) -> bool:
        try:
            return psutil.Process(target.pid).create_time() == target.created
        except psutil.NoSuchProcess:
            return False

    async def _run(self, settings: MqttSettings, target: AppServerTarget) -> None:
        seen = OrderedDict()
        try:
            parent = psutil.Process(target.pid).parent() if self._same_process(target) else None
            parent_identity = AppServerTarget(parent.pid, parent.create_time()) if parent is not None else None
        except psutil.NoSuchProcess:
            parent_identity = None
        self._last_attach_succeeded = False
        failures = 0
        attempt = 0
        with tempfile.TemporaryDirectory(prefix='codex-mqtt-') as directory:
            broker = MosquittoBroker(Path(directory), settings.port, settings.username, settings.password, settings.host)
            with self._stage('MQTT 端口就绪', target, 1):
                await broker.start()
            self.broker_running = True
            try:
                while not self._stop.is_set():
                    if not self._same_process(target):
                        if parent_identity is None or not self._same_process(parent_identity):
                            self.status = '目标进程已退出'
                            raise RuntimeError('原桌面客户端已退出，请重新选择 App Server')
                        if self.status != '等待新 App Server':
                            self.status = '等待新 App Server'
                            self._record(self.status, f'原 PID {target.pid} · 父 PID {parent_identity.pid}')
                        candidates = []
                        for candidate in discover_app_servers():
                            try:
                                if (self._same_process(candidate)
                                        and psutil.Process(candidate.pid).ppid() == parent_identity.pid):
                                    candidates.append(candidate)
                            except psutil.NoSuchProcess:
                                continue
                        if len(candidates) != 1 or not self._same_process(parent_identity):
                            await asyncio.to_thread(self._stop.wait, 1)
                            continue
                        target = self.target = candidates[0]
                        failures = 0
                        self._record('发现新 App Server', f'PID {target.pid} · 父 PID {parent_identity.pid}')
                    attempt += 1
                    try:
                        await self._attach_once(settings, target, broker, seen, attempt)
                        return
                    except Exception as exc:
                        if self._stop.is_set():
                            return
                        if self._last_attach_succeeded:
                            failures = 0
                            self._last_attach_succeeded = False
                        if isinstance(exc, _ProbeCleanupError) or not broker.running:
                            raise
                        delay = self.RETRY_DELAYS[min(failures, len(self.RETRY_DELAYS) - 1)]
                        failures += 1
                        self.status = '正在重新附加'
                        self.error = str(exc)
                        when = f'{delay} 秒后' if delay else '立即'
                        self._record(self.status, f'PID {target.pid} · {when}第 {failures} 次尝试 · {exc}')
                        if delay and await asyncio.to_thread(self._stop.wait, delay):
                            return
            finally:
                await asyncio.wait_for(broker.shutdown(), 3)
                self.broker_running = False

    async def _attach_once(self, settings: MqttSettings, target: AppServerTarget, broker, seen, attempt) -> None:
        from frida import aio as frida
        from amqtt.client import MQTTClient

        prefix = f'codex/{target.pid}'
        client = None
        self.dropped = self._queue_peak = 0
        self._publish_peak = 0.0
        chunks = queue.Queue(maxsize=self.CAPTURE_QUEUE_CHUNKS)
        capture_stats = {'bytes': 0, 'peak': 0, 'wait': 0.0}
        capture_ready = asyncio.Event()
        sequence = Counter()
        detached = threading.Event()
        process_exited = threading.Event()
        probe_errors = queue.SimpleQueue()

        def on_detached(reason, _crash) -> None:
            if reason == 'process-terminated':
                process_exited.set()
            if not cleaning and not self._stop.is_set():
                self._record('App Server 探针断开', f'PID {target.pid} · 尝试 {attempt} · {reason}')
            detached.set()

        def on_message(message, data) -> None:
            if message['type'] == 'error':
                probe_errors.put(message.get('description', '探针异常'))
                capture_ready.set()
                return
            if message['type'] != 'send':
                return
            meta = message['payload']
            if meta.get('kind') == 'parentBytes' and writer is not None and writer.ready:
                return
            stream = meta.get('direction', 'parent')
            seq = sequence[stream]
            sequence[stream] += 1
            try:
                if capture_stats['bytes'] + len(data) > self.CAPTURE_QUEUE_BYTES:
                    raise queue.Full
                chunks.put_nowait((meta, data, time.monotonic(), stream, seq))
                capture_stats['bytes'] += len(data)
                capture_stats['peak'] = max(capture_stats['peak'], capture_stats['bytes'])
                self._queue_peak = max(self._queue_peak, chunks.qsize())
            except queue.Full:
                self.dropped += 1
            capture_ready.set()

        session = script = parent_session = writer = None
        ready = cleaning = False

        async def disconnect():
            nonlocal client
            if client is None:
                return
            try:
                with self._stage('MQTT 桥接断开', target, attempt):
                    await asyncio.wait_for(client.disconnect(), 2)
            except Exception as exc:
                self._record('MQTT 客户端清理失败', type(exc).__name__)
            finally:
                client = None

        async def connect():
            nonlocal client
            await disconnect()
            client = MQTTClient()
            username = quote(settings.username, safe='')
            password = quote(settings.password, safe='')
            host = settings.host if settings.host != '0.0.0.0' else '127.0.0.1'
            try:
                with self._stage('MQTT 桥接连接', target, attempt):
                    await asyncio.wait_for(client.connect(f'mqtt://{username}:{password}@{host}:{settings.port}'), 5)
            except Exception:
                raise RuntimeError('MQTT 桥接客户端连接失败') from None
            with self._stage('MQTT 桥接订阅', target, attempt):
                grants = await asyncio.wait_for(client.subscribe([(f'{prefix}/command', 1), (f'{prefix}/files/request/+', 1)]), 5)
                if grants != [1, 1]:
                    raise RuntimeError('订阅 MQTT 命令主题失败')
            return client

        try:
            if self._stop.is_set():
                return
            if not self._same_process(target):
                raise RuntimeError('App Server 已退出或已重启，请刷新后重试')
            await connect()
            if self._stop.is_set():
                return
            if not self._same_process(target):
                raise RuntimeError('App Server 已退出或已重启，请刷新后重试')
            parent = psutil.Process(target.pid).parent()
            if parent is None:
                raise RuntimeError('找不到 App Server 父客户端')
            writer = AppServerPipeWriter(parent.pid, parent.create_time())
            with self._stage('父进程探针注入', target, attempt):
                async with self._probe_deadline('父进程探针注入'):
                    parent_session = await frida.attach(parent.pid)
                    parent_script = await parent_session.create_script(
                        Path(__file__).with_name('app_server_parent_probe.js').read_text(encoding='utf-8'))
                    parent_script.on('message', on_message)
                    await parent_script.load()
            self._parent_probe = parent_script
            with self._stage('App Server 探针注入', target, attempt):
                async with self._probe_deadline('App Server 探针注入'):
                    session = await frida.attach(target.pid)
                    session.on('detached', on_detached)
                    if not self._same_process(target):
                        raise RuntimeError('App Server 已退出或已重启，请刷新后重试')
                    source = Path(__file__).with_name('app_server_probe.js').read_text(encoding='utf-8')
                    script = await session.create_script(source)
                    script.on('message', on_message)
                    await script.load()
                    ready = True
                    try:
                        await script.exports.ready()
                    except frida.RPCException as exc:
                        raise RuntimeError(probe_errors.get_nowait() if not probe_errors.empty() else str(exc)) from exc
            await self._bridge(client, writer, prefix, chunks, detached, process_exited, probe_errors, broker, seen,
                               target, attempt, connect, capture_stats, capture_ready, sequence)
        except Exception as exc:
            if ready and not self._stop.is_set():
                self._record('桥接异常', f'PID {target.pid} · 尝试 {attempt} · {type(exc).__name__} · {exc}')
            raise
        finally:
            cleaning = True
            self.attached = self.writer_ready = False
            if process_exited.is_set() and not self._stop.is_set():
                self.status = '目标进程已退出'
            if ready or self.dropped:
                self._record('捕获统计', f'队列峰值 {self._queue_peak}/{self.CAPTURE_QUEUE_CHUNKS} · 丢弃 {self.dropped} 块'
                            f' · 字节峰值 {capture_stats["peak"]} · 最长排队 {capture_stats["wait"] * 1000:.1f} ms'
                           f' · MQTT 发布最长 {self._publish_peak * 1000:.1f} ms')
            try:
                try:
                    if ready and not detached.is_set():
                        try:
                            with self._stage('App Server 探针停止捕获', target, attempt):
                                async with self._probe_deadline('App Server 探针停止捕获', 2):
                                    await script.exports.dispose()
                        except Exception as exc:
                            self._record('探针停止捕获失败', f'PID {target.pid} · {type(exc).__name__}')
                        try:
                            if writer is not None and writer.ready:
                                # Release a pre-existing read callback before unloading Frida.
                                wake = {'id': f'csm-mqtt:{uuid.uuid4().hex}', 'method': 'thread/list', 'params': {'limit': 1}}
                                await asyncio.to_thread(writer.write, (json.dumps(wake) + '\n').encode())
                        except Exception as exc:
                            self._record('管道唤醒失败', f'PID {target.pid} · {type(exc).__name__}')
                finally:
                    try:
                        if session is not None and not detached.is_set():
                            with self._stage('App Server 探针卸载', target, attempt):
                                await self._wait_probe_detach(session, target)
                    finally:
                        try:
                            if parent_session is not None:
                                with self._stage('父进程探针卸载', target, attempt):
                                    await self._wait_probe_detach(parent_session, target)
                        finally:
                            if writer is not None:
                                writer.close()
            except Exception as exc:
                raise _ProbeCleanupError(f'探针清理失败：{exc}') from exc
            finally:
                self._parent_probe = None
                await disconnect()

    async def _bridge(self, client, writer, prefix, chunks, detached, process_exited, probe_errors, broker, seen,
                      target, attempt, reconnect, capture_stats, capture_ready, sequence) -> None:
        loop = asyncio.get_running_loop()
        run_id = uuid.uuid4().hex
        files = FileTransfers()
        file_tasks = set()
        observed_queue = deque()
        replies = deque()
        outgoing = asyncio.Event()
        observed_bytes = observed_peak = observed_dropped = reply_bytes = 0
        status_dirty = False

        async def transfer_file(connection, topic, payload):
            request_id = topic.rsplit('/', 1)[-1]
            try:
                response = await asyncio.to_thread(self._file_as_target, files, payload)
                await asyncio.wait_for(connection.publish(f'{prefix}/files/reply/{request_id}', response, qos=1, retain=False),
                                       self.PUBLISH_TIMEOUT)
            except Exception as exc:
                self._record('文件传输失败', type(exc).__name__)

        async def publish_payload(topic, payload, *, retain=False, qos=0):
            started = loop.time()
            try:
                async with asyncio.timeout(self.PUBLISH_TIMEOUT):
                    await client.publish(f'{prefix}/{topic}', payload, qos=qos, retain=retain)
            finally:
                self._publish_peak = max(self._publish_peak, loop.time() - started)

        async def publish(topic, message, *, qos):
            nonlocal reply_bytes
            payload = json.dumps(message, ensure_ascii=False).encode()
            if len(replies) >= 1024 or (replies and reply_bytes + len(payload) > self.OBSERVED_QUEUE_BYTES):
                self._record('MQTT 回复积压超限', f'PID {target.pid} · {topic} · 可用原请求 ID 查询已缓存的结果')
                return
            replies.append((topic, payload, qos))
            reply_bytes += len(payload)
            outgoing.set()

        async def send_outgoing():
            nonlocal observed_bytes, observed_dropped, reply_bytes, status_dirty
            while True:
                await outgoing.wait()
                if status_dirty:
                    status_dirty = False
                    with self._stage('发布 MQTT 写端状态', target, attempt):
                        await publish_status(True, writer.ready)
                elif replies:
                    topic, payload, qos = replies[0]
                    await publish_payload(topic, payload, qos=qos)
                    replies.popleft()
                    reply_bytes -= len(payload)
                elif observed_dropped:
                    dropped = observed_dropped
                    await publish_payload('observed/gap', json.dumps({'runId': run_id, 'dropped': dropped}).encode(), qos=1)
                    observed_dropped -= dropped
                elif observed_queue:
                    topic, payload = observed_queue.popleft()
                    observed_bytes -= len(payload)
                    try:
                        await publish_payload(topic, payload)
                    except (Exception, asyncio.CancelledError):
                        observed_dropped += 1
                        raise
                else:
                    outgoing.clear()

        def enqueue_observed(topic, message):
            nonlocal observed_bytes, observed_peak, observed_dropped
            payload = json.dumps(message, ensure_ascii=False).encode()
            if len(observed_queue) >= 4096 or observed_bytes + len(payload) > self.OBSERVED_QUEUE_BYTES:
                if not observed_dropped:
                    self._record('MQTT 观察消息积压超限', f'PID {target.pid} · 开始丢弃观察消息')
                observed_dropped += 1
                outgoing.set()
                return
            observed_queue.append((topic, payload))
            observed_bytes += len(payload)
            observed_peak = max(observed_peak, observed_bytes)
            outgoing.set()

        async def publish_status(attached, writer_ready):
            payload = {'attached': attached, 'writerReady': writer_ready, 'protocolVersion': 1,
                       'runId': run_id, 'maxRequestBytes': None, 'fileTransfer': CAPABILITIES, 'methods': '*'}
            await publish_payload('status', json.dumps(payload, ensure_ascii=False).encode(), retain=True, qos=1)
            locator = json.dumps({'pid': target.pid, 'attached': attached, 'writerReady': writer_ready,
                                  'runId': run_id}).encode()
            async with asyncio.timeout(self.PUBLISH_TIMEOUT):
                await client.publish('codex/status', locator, qos=1, retain=True)

        async def mqtt_ready(reconnect_needed):
            nonlocal client
            if reconnect_needed:
                client = await reconnect()
            with self._stage('发布 MQTT 附加状态', target, attempt):
                await publish_status(True, writer.ready)

        async def recover_mqtt():
            failures = 0
            while not self._stop.is_set():
                delay = self.RETRY_DELAYS[min(failures, len(self.RETRY_DELAYS) - 1)]
                await asyncio.sleep(delay)
                try:
                    await mqtt_ready(True)
                    return
                except Exception as exc:
                    failures += 1
                    self._record('MQTT 重连失败', f'PID {target.pid} · {type(exc).__name__}')

        buffers = {'input': b'', 'output': b''}
        requests = OrderedDict()
        parent_buffers = {}
        parent_requests = OrderedDict()
        input_requests = OrderedDict()
        pending = {}
        expected = Counter()
        resync = set()
        carry = None
        command = confirmation = None

        def report_capture_gap(stream, seq):
            nonlocal observed_dropped
            missing = seq - expected[stream]
            expected[stream] = seq
            self._record('捕获缺口', f'PID {target.pid} · {stream} · 丢弃 {missing} 块')
            parent_buffers.clear()
            parent_requests.clear()
            input_requests.clear()
            if stream != 'parent':
                buffers[stream] = b''
                resync.add(stream)
                requests.clear()
                if stream == 'output':
                    for request_id in pending.values():
                        seen[request_id] = {'id': request_id, 'stage': 'delivery_unknown',
                                            'error': '捕获缺口可能丢失响应，请查询执行状态'}
            observed_dropped += 1
            outgoing.set()

        def take_chunk():
            meta, data, queued_at, stream, seq = chunks.get_nowait()
            capture_stats['bytes'] -= len(data)
            capture_stats['wait'] = max(capture_stats['wait'], time.monotonic() - queued_at)
            return meta, data, stream, seq

        def take_batch():
            nonlocal carry
            meta, data, stream, seq = carry if carry is not None else take_chunk()
            carry = None
            parts = [data]
            size = len(data)
            if stream != 'parent':
                for _ in range(7):
                    try:
                        item = take_chunk()
                    except queue.Empty:
                        break
                    other_meta, part, other_stream, other_seq = item
                    if other_stream != stream or other_seq != seq + len(parts) or size + len(part) > 65536:
                        carry = item
                        break
                    parts.append(part)
                    size += len(part)
            return meta, b''.join(parts), stream, seq, len(parts)

        receive = sender = None
        connecting = asyncio.create_task(mqtt_ready(False))
        try:
            while not self._stop.is_set():
                received = None
                if not broker.running:
                    raise RuntimeError('Mosquitto 已退出，请重新启动服务')
                if detached.is_set():
                    if process_exited.is_set():
                        self.status = '目标进程已退出'
                    raise RuntimeError('App Server 探针已断开')
                if not probe_errors.empty():
                    raise RuntimeError(probe_errors.get_nowait())
                if command is not None and command.done():
                    command.result()
                    command = None
                if confirmation is not None and confirmation.done():
                    confirmation.result()
                    confirmation = None
                    if writer.ready:
                        status_dirty = True
                        outgoing.set()
                if connecting is not None and connecting.done():
                    try:
                        connecting.result()
                    except Exception:
                        self.status = 'MQTT 正在重连'
                        connecting = asyncio.create_task(recover_mqtt())
                    else:
                        connecting = None
                        event = 'MQTT 已重连' if self.status == 'MQTT 正在重连' else '已附加'
                        self.attached = True
                        self.status, self.error = '已附加', ''
                        self._last_attach_succeeded = True
                        self._record(event, f'PID {target.pid}')
                        sender = asyncio.create_task(send_outgoing())
                        receive = asyncio.create_task(client.deliver_message())
                if receive is not None:
                    try:
                        if not client.session.transitions.is_connected():
                            raise ConnectionError('MQTT 桥接客户端已断开')
                        if sender.done():
                            sender.result()
                        if receive.done():
                            if receive.cancelled():
                                raise ConnectionError('MQTT 桥接客户端已断开')
                            packet_received = receive.result()
                            if packet_received is None:
                                raise ConnectionError('MQTT 桥接客户端已断开')
                            if command is None:
                                received = packet_received
                    except Exception as exc:
                        self.status = 'MQTT 正在重连'
                        self._record(self.status, f'PID {target.pid} · {type(exc).__name__}')
                        sender.cancel()
                        receive.cancel()
                        await asyncio.gather(sender, receive, return_exceptions=True)
                        sender = receive = None
                        connecting = asyncio.create_task(recover_mqtt())
                for _ in range(64):
                    if detached.is_set():
                        break
                    try:
                        meta, data, stream, seq, span = take_batch()
                    except queue.Empty:
                        break
                    if seq != expected[stream]:
                        report_capture_gap(stream, seq)
                    expected[stream] = seq + span
                    if meta['kind'] == 'parentBytes':
                        if not writer.ready:
                            handle = meta['handle']
                            stream = parent_buffers.get(handle, b'') + data
                            lines = stream.split(b'\n')
                            parent_buffers[handle] = lines.pop()
                            if len(parent_buffers) > 32:
                                parent_buffers.clear()
                            for line in lines:
                                fingerprint = self._request_fingerprint(line)
                                if fingerprint:
                                    parent_requests[fingerprint] = handle
                                    if len(parent_requests) > 128:
                                        parent_requests.popitem(last=False)
                            if confirmation is None:
                                confirmation = asyncio.create_task(self._confirm_writer(writer, parent_requests, input_requests))
                        continue
                    if meta['kind'] != 'bytes':
                        continue
                    direction = meta['direction']
                    if direction in resync:
                        _, boundary, data = data.partition(b'\n')
                        if not boundary:
                            continue
                        resync.remove(direction)
                    lines = (buffers[direction] + data).split(b'\n')
                    buffers[direction] = lines.pop()
                    for line in lines:
                        if detached.is_set():
                            break
                        try:
                            message = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if not isinstance(message, dict):
                            continue
                        if direction == 'input' and not writer.ready:
                            fingerprint = self._request_fingerprint(line)
                            if fingerprint:
                                input_requests[fingerprint] = True
                                if len(input_requests) > 128:
                                    input_requests.popitem(last=False)
                                if confirmation is None:
                                    confirmation = asyncio.create_task(self._confirm_writer(writer, parent_requests, input_requests))
                        request_id = message.get('id')
                        if isinstance(request_id, str) and request_id.startswith('csm-mqtt:'):
                            external_id = pending.pop(request_id, None) if direction == 'output' else None
                            if external_id is not None:
                                message['id'] = external_id
                                reply = message
                                seen[external_id] = reply
                                self._reply_expiry[external_id] = loop.time() + 60
                                await publish(f'reply/{quote(str(external_id), safe="")}', reply, qos=1)
                            continue
                        if direction == 'output':
                            self._record_protocol_event(message)
                        observed = self._observed(message, direction, requests)
                        if observed is not None:
                            enqueue_observed(f'observed/{direction}', observed)
                        self.count += 1
                if received is not None and not (
                        self._stop.is_set() or detached.is_set() or not probe_errors.empty()):
                    packet = received.publish_packet
                    receive = asyncio.create_task(client.deliver_message())
                    if packet and not packet.retain_flag:
                        if received.topic.startswith(f'{prefix}/files/request/'):
                            request_id = received.topic.rsplit('/', 1)[-1]
                            if re.fullmatch(r'[A-Za-z0-9-]{1,64}', request_id) and len(packet.payload.data) <= 540672:
                                task = asyncio.create_task(transfer_file(client, received.topic, packet.payload.data))
                                file_tasks.add(task)
                                task.add_done_callback(file_tasks.discard)
                        else:
                            command = asyncio.create_task(self._command(packet.payload.data, writer, publish, pending, seen, requests))
                if chunks.empty() and carry is None:
                    for stream, seq in sequence.items():
                        if seq > expected[stream]:
                            report_capture_gap(stream, seq)
                    capture_ready.clear()
                    try:
                        await asyncio.wait_for(capture_ready.wait(), 0.025)
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0)
        finally:
            mqtt_tasks = [task for task in (sender, receive, connecting) if task is not None]
            for task in mqtt_tasks:
                task.cancel()
            await asyncio.gather(*mqtt_tasks, return_exceptions=True)
            await asyncio.gather(*(task for task in (command, confirmation) if task is not None), return_exceptions=True)
            self._record('MQTT 观察发送统计', f'积压峰值 {observed_peak} 字节 · 结束时未发完 {observed_bytes} 字节'
                         f' · 待报告缺口 {observed_dropped} 条')
            if file_tasks:
                await asyncio.gather(*file_tasks, return_exceptions=True)
            for request_id in pending.values():
                seen[request_id] = {'id': request_id, 'stage': 'delivery_unknown',
                                    'error': '连接中断前尚未收到响应，请查询执行状态'}
            try:
                await asyncio.wait_for(publish_status(False, False), 1)
            except Exception as exc:
                self._record('MQTT 状态更新失败', str(exc))
            else:
                self._record('已发布 MQTT 断开状态', f'PID {target.pid} · 尝试 {attempt}')

    def _file_as_target(self, files, payload):
        import win32api
        import win32con
        import win32security

        process = win32api.OpenProcess(0x1000, False, self.target.pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        try:
            token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY | win32con.TOKEN_DUPLICATE)
            try:
                impersonation = win32security.DuplicateToken(token, win32security.SecurityImpersonation)
                try:
                    win32security.ImpersonateLoggedOnUser(impersonation)
                    try:
                        return files.handle(payload)
                    finally:
                        win32security.RevertToSelf()
                finally:
                    impersonation.Close()
            finally:
                token.Close()
        finally:
            process.Close()

    async def _confirm_writer(self, writer, parent_requests, input_requests) -> None:
        matches = [parent_requests[key] for key in input_requests if key in parent_requests]
        for fingerprint in input_requests:
            if fingerprint in parent_requests and matches.count(parent_requests[fingerprint]) >= 2:
                writer.confirm(parent_requests[fingerprint])
                if self._parent_probe is not None:
                    async with self._probe_deadline('父进程探针停止捕获', 2):
                        await self._parent_probe.exports.dispose()
                self.writer_ready = True
                self._record('写端已确认', 'MQTT 命令可以主动投递')
                parent_requests.clear()
                input_requests.clear()
                return

    @staticmethod
    def _request_fingerprint(line: bytes) -> str | None:
        try:
            message = json.loads(line)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(message, dict) or 'id' not in message or not isinstance(message.get('method'), str):
            return None
        return hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=True).encode()).hexdigest()

    async def _command(self, data, writer, publish, pending, seen, requests):
        try:
            command = json.loads(data)
        except (ValueError, UnicodeError):
            return pending
        if not isinstance(command, dict):
            return pending
        if 'id' not in command:
            if not writer.ready or command.get('method') in ('initialize', 'initialized'):
                return pending
            await asyncio.to_thread(writer.write, (json.dumps(command, ensure_ascii=False) + '\n').encode())
            return pending
        request_id = command.get('id')
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return pending
        for key, expiry in list(self._reply_expiry.items()):
            if expiry <= asyncio.get_running_loop().time():
                self._reply_expiry.pop(key, None)
                seen.pop(key, None)
        if request_id in seen:
            await publish(f'reply/{quote(str(request_id), safe="")}', seen[request_id], qos=1)
            return pending
        method = command.get('method')
        params = command.get('params', {})
        error = None if writer.ready else '写端尚未确认，请在桌面切换一次会话'
        if method in ('initialize', 'initialized'):
            error = '桌面连接已经初始化，请直接发送业务请求'
        if not error:
            rpc_id = f'csm-mqtt:{uuid.uuid4().hex}'
            message = {**command, 'id': rpc_id}
            server_id = params.get('requestId') if isinstance(params, dict) else None
            if method == 'rpc/respond':
                if (not isinstance(params, dict) or isinstance(server_id, bool) or not isinstance(server_id, (str, int))
                        or ('result' in params) == ('error' in params)):
                    await publish(f'reply/{quote(str(request_id), safe="")}', {'id': request_id, 'error': '反向请求回答格式无效'}, qos=1)
                    return pending
                if ('output', server_id) not in requests:
                    await publish(f'reply/{quote(str(request_id), safe="")}', {'id': request_id, 'error': '反向请求已结束'}, qos=1)
                    return pending
                message = {'id': server_id, **{key: params[key] for key in ('result', 'error') if key in params}}
            elif 'method' not in command:
                if ('result' in command) == ('error' in command) or ('output', request_id) not in requests:
                    await publish(f'reply/{quote(str(request_id), safe="")}', {'id': request_id, 'error': '反向请求已结束'}, qos=1)
                    return pending
                message = command
            payload = json.dumps(message, ensure_ascii=False) + '\n'
            seen[request_id] = {'id': request_id, 'stage': 'delivery_unknown', 'error': '投递处理中，请勿重复执行'}
            self._reply_expiry[request_id] = asyncio.get_running_loop().time() + 3600
            expect_reply = method != 'rpc/respond' and 'method' in command
            if expect_reply:
                pending[rpc_id] = request_id
            try:
                await asyncio.to_thread(writer.write, payload.encode())
            except Exception as exc:
                if expect_reply and pending.pop(rpc_id, None) is None:
                    return pending
                reply = {'id': request_id, 'error': str(exc), 'stage': 'delivery_unknown'}
                seen[request_id] = reply
                await publish(f'reply/{quote(str(request_id), safe="")}', reply, qos=1)
                return pending
            else:
                if method == 'rpc/respond' or 'method' not in command:
                    requests.pop(('output', server_id if method else request_id), None)
                    reply = {'id': request_id, 'result': {'written': True}}
                    seen[request_id] = reply
                    self._reply_expiry[request_id] = asyncio.get_running_loop().time() + 60
                    await publish(f'reply/{quote(str(request_id), safe="")}', reply, qos=1)
                    return pending
                return pending
        await publish(f'reply/{quote(str(request_id), safe="")}', {'id': request_id, 'error': error}, qos=1)
        return pending

    @classmethod
    def _observed(cls, message, direction, requests):
        request_id = message.get('id')
        method = message.get('method')
        if isinstance(request_id, (str, int)) and not isinstance(request_id, bool):
            if isinstance(method, str):
                requests[(direction, request_id)] = method
            else:
                opposite = 'output' if direction == 'input' else 'input'
                method = requests.pop((opposite, request_id), None)
        if method == 'serverRequest/resolved':
            requests.pop(('output', (message.get('params') or {}).get('requestId')), None)
        return message
