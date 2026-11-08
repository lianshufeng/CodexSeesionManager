from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import queue
import re
import tempfile
import threading
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import psutil

from app.services.mosquitto_broker import MosquittoBroker
from app.services.app_server_pipe_writer import AppServerPipeWriter
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
    METHODS = frozenset({'model/list', 'thread/list', 'thread/read', 'thread/start', 'thread/resume',
                         'thread/turns/list', 'thread/items/list', 'turn/start', 'turn/interrupt'})
    RESPOND_METHODS = frozenset({'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                                 'item/permissions/requestApproval', 'item/tool/requestUserInput'})
    SENSITIVE = re.compile(r'password|secret|authorization|^token$|(?:access|refresh|id|api|bearer)[_-]?(?:token|key)|cookie', re.I)
    CHUNK_LIMIT = 4 * 1024 * 1024
    RESPONSE_TIMEOUT = 15
    RETRY_DELAYS = (1, 2, 4)
    PUBLISH_TIMEOUT = 5

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.status = '未启动'
        self.error = ''
        self.count = 0
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
                self.status = ('已断开' if self.status in ('已附加', '正在重新附加', '已断开')
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
        self._last_attach_succeeded = False
        failures = 0
        has_attached = False
        with tempfile.TemporaryDirectory(prefix='codex-mqtt-') as directory:
            broker = MosquittoBroker(Path(directory), settings.port, settings.username, settings.password, settings.host)
            await broker.start()
            self.broker_running = True
            try:
                while not self._stop.is_set():
                    try:
                        await self._attach_once(settings, target, broker, seen)
                        return
                    except Exception as exc:
                        if self._stop.is_set():
                            return
                        if self._last_attach_succeeded:
                            has_attached = True
                            failures = 0
                            self._last_attach_succeeded = False
                        if self.status == '目标进程已退出' or not self._same_process(target):
                            self.status = '目标进程已退出'
                            raise RuntimeError('App Server 已退出或已重启，请刷新后重试') from exc
                        if not has_attached or isinstance(exc, _ProbeCleanupError) or not broker.running:
                            raise
                        if failures == len(self.RETRY_DELAYS):
                            self.status = '已断开'
                            raise RuntimeError(f'自动重新附加连续 {failures} 次失败，最后一次错误：{exc}') from exc
                        delay = self.RETRY_DELAYS[failures]
                        failures += 1
                        self.status = '正在重新附加'
                        self.error = str(exc)
                        self._record(self.status, f'{delay} 秒后第 {failures}/{len(self.RETRY_DELAYS)} 次尝试 · {exc}')
                        if await asyncio.to_thread(self._stop.wait, delay):
                            return
            finally:
                await asyncio.wait_for(broker.shutdown(), 3)
                self.broker_running = False

    async def _attach_once(self, settings: MqttSettings, target: AppServerTarget, broker, seen) -> None:
        import frida
        from amqtt.client import MQTTClient

        prefix = f'codex/{target.pid}'
        client = MQTTClient()
        self.dropped = self._queue_peak = 0
        self._publish_peak = 0.0
        chunks = queue.Queue(maxsize=512)
        detached = threading.Event()
        process_exited = threading.Event()
        probe_errors = queue.SimpleQueue()

        def on_detached(reason, _crash) -> None:
            if reason == 'process-terminated':
                process_exited.set()
            detached.set()

        def on_message(message, data) -> None:
            if message['type'] == 'error':
                probe_errors.put(message.get('description', '探针异常'))
                return
            if message['type'] != 'send':
                return
            try:
                chunks.put_nowait((message['payload'], data))
                self._queue_peak = max(self._queue_peak, chunks.qsize())
            except queue.Full:
                self.dropped += 1

        session = script = parent_session = writer = None
        ready = connected = False
        try:
            if self._stop.is_set():
                return
            if not self._same_process(target):
                raise RuntimeError('App Server 已退出或已重启，请刷新后重试')
            username = quote(settings.username, safe='')
            password = quote(settings.password, safe='')
            try:
                await asyncio.wait_for(client.connect(
                    f'mqtt://{username}:{password}@{settings.host if settings.host != "0.0.0.0" else "127.0.0.1"}:{settings.port}'), 5)
            except Exception:
                raise RuntimeError('MQTT 桥接客户端连接失败') from None
            connected = True
            grants = await asyncio.wait_for(client.subscribe([(f'{prefix}/command', 1)]), 5)
            if grants != [1]:
                raise RuntimeError('订阅 MQTT 命令主题失败')
            if self._stop.is_set():
                return
            if not self._same_process(target):
                raise RuntimeError('App Server 已退出或已重启，请刷新后重试')
            parent = psutil.Process(target.pid).parent()
            if parent is None:
                raise RuntimeError('找不到 App Server 父客户端')
            writer = AppServerPipeWriter(parent.pid, parent.create_time())
            parent_session = frida.attach(parent.pid)
            parent_script = parent_session.create_script(
                Path(__file__).with_name('app_server_parent_probe.js').read_text(encoding='utf-8'))
            parent_script.on('message', on_message)
            parent_script.load()
            self._parent_probe = parent_script
            session = frida.attach(target.pid)
            session.on('detached', on_detached)
            if not self._same_process(target):
                raise RuntimeError('App Server 已退出或已重启，请刷新后重试')
            source = Path(__file__).with_name('app_server_probe.js').read_text(encoding='utf-8')
            script = session.create_script(source)
            script.on('message', on_message)
            script.load()
            try:
                script.exports_sync.ready()
            except frida.RPCException as exc:
                raise RuntimeError(probe_errors.get_nowait() if not probe_errors.empty() else str(exc)) from exc
            ready = True
            await self._bridge(client, writer, prefix, chunks, detached, process_exited, probe_errors, broker, seen)
        finally:
            self.attached = self.writer_ready = False
            if process_exited.is_set() and not self._stop.is_set():
                self.status = '目标进程已退出'
            if ready or self.dropped:
                self._record('捕获统计', f'队列峰值 {self._queue_peak}/512 · 丢弃 {self.dropped} 块'
                           f' · MQTT 发布最长 {self._publish_peak * 1000:.1f} ms')
            try:
                try:
                    if ready and not detached.is_set():
                        script.exports_sync.dispose()
                        if writer is not None and writer.ready:
                            # Release a pre-existing read callback before unloading Frida.
                            wake = {'id': f'csm-mqtt:{uuid.uuid4().hex}', 'method': 'thread/list', 'params': {'limit': 1}}
                            await asyncio.to_thread(writer.write, (json.dumps(wake) + '\n').encode())
                finally:
                    try:
                        if session is not None and not detached.is_set():
                            cancellation = frida.Cancellable()
                            timer = threading.Timer(2, cancellation.cancel)
                            timer.start()
                            try:
                                with cancellation:
                                    session.detach()
                            except frida.OperationCancelledError:
                                raise RuntimeError('旧探针未确认卸载，待读取返回后回收') from None
                            finally:
                                timer.cancel()
                    finally:
                        try:
                            if parent_session is not None:
                                parent_session.detach()
                        finally:
                            if writer is not None:
                                writer.close()
            except Exception as exc:
                raise _ProbeCleanupError(f'探针清理失败：{exc}') from exc
            finally:
                self._parent_probe = None
                if connected:
                    try:
                        await asyncio.wait_for(client.disconnect(), 2)
                    except Exception as exc:
                        self._record('MQTT 客户端清理失败', str(exc))

    async def _bridge(self, client, writer, prefix, chunks, detached, process_exited, probe_errors, broker, seen) -> None:
        loop = asyncio.get_running_loop()

        async def publish(topic, message, *, retain=False, qos=0):
            started = loop.time()
            try:
                async with asyncio.timeout(self.PUBLISH_TIMEOUT):
                    await client.publish(f'{prefix}/{topic}', json.dumps(message, ensure_ascii=False).encode(),
                                         qos=qos, retain=retain)
            finally:
                self._publish_peak = max(self._publish_peak, loop.time() - started)

        buffers = {'input': b'', 'output': b''}
        skip_line = {'input': True, 'output': True}
        requests = OrderedDict()
        parent_buffers = {}
        parent_requests = OrderedDict()
        input_requests = OrderedDict()
        pending = None
        routes = {}
        receive = None
        try:
            while not self._stop.is_set():
                if not broker.running:
                    raise RuntimeError('Mosquitto 已退出，请重新启动服务')
                if detached.is_set():
                    if process_exited.is_set():
                        self.status = '目标进程已退出'
                    raise RuntimeError('App Server 探针已断开')
                if not probe_errors.empty():
                    raise RuntimeError(probe_errors.get_nowait())
                if self.dropped:
                    raise RuntimeError('捕获队列溢出，消息边界已失效')
                if receive is None:
                    await publish('status', {'attached': True, 'writerReady': False}, retain=True)
                    if detached.is_set() or self.dropped or not probe_errors.empty():
                        continue
                    if self._stop.is_set():
                        return
                    self.attached = True
                    self.status, self.error = '已附加', ''
                    self._last_attach_succeeded = True
                    self._record('已附加', f'PID {prefix.rsplit("/", 1)[-1]} · 请切换一次会话以确认写端')
                    receive = asyncio.create_task(client.deliver_message())
                for _ in range(64):
                    try:
                        meta, data = chunks.get_nowait()
                    except queue.Empty:
                        break
                    if meta['kind'] == 'parentBytes':
                        if not writer.ready:
                            handle = meta['handle']
                            stream = parent_buffers.get(handle, b'') + data
                            lines = stream.split(b'\n')
                            parent_buffers[handle] = lines.pop()[-self.CHUNK_LIMIT:]
                            if len(parent_buffers) > 32:
                                parent_buffers.clear()
                            for line in lines:
                                fingerprint = self._request_fingerprint(line)
                                if fingerprint:
                                    parent_requests[fingerprint] = handle
                                    if len(parent_requests) > 128:
                                        parent_requests.popitem(last=False)
                            self._confirm_writer(writer, parent_requests, input_requests)
                            if writer.ready:
                                await publish('status', {'attached': True, 'writerReady': True}, retain=True)
                        continue
                    if meta['kind'] != 'bytes':
                        continue
                    direction = meta['direction']
                    lines = (buffers[direction] + data).split(b'\n')
                    buffers[direction] = lines.pop()
                    if len(buffers[direction]) > self.CHUNK_LIMIT:
                        buffers[direction] = b''
                        skip_line[direction] = True
                    for line in lines:
                        if len(line) > self.CHUNK_LIMIT:
                            continue
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
                                self._confirm_writer(writer, parent_requests, input_requests)
                                if writer.ready:
                                    await publish('status', {'attached': True, 'writerReady': True}, retain=True)
                        uncertain = skip_line[direction]
                        skip_line[direction] = False
                        request_id = message.get('id')
                        if isinstance(request_id, str) and request_id.startswith('csm-mqtt:'):
                            external_id = routes.pop(request_id, None) if direction == 'output' else None
                            if external_id is not None:
                                message['id'] = external_id
                                reply = self._redact(message)
                                seen[external_id] = reply
                                await publish(f'reply/{external_id}', reply, qos=1)
                                if pending and request_id == pending[0]:
                                    pending = None
                            continue
                        if direction == 'output':
                            self._record_protocol_event(message)
                        message = self._observed(message, direction, requests, uncertain)
                        await publish(f'observed/{direction}', message)
                        self.count += 1
                if pending and loop.time() >= pending[2]:
                    stage, error = 'response_timeout', '命令已写入，等待响应超时，请查询状态后再决定是否重试'
                    self._record('MQTT 命令超时', error)
                    reply = {'id': pending[1], 'error': error, 'stage': stage}
                    seen[pending[1]] = reply
                    await publish(f'reply/{pending[1]}', reply, qos=1)
                    pending = None
                if receive.done() and not (self._stop.is_set() or self.dropped or detached.is_set()
                                           or not probe_errors.empty()):
                    received = receive.result()
                    if received is None:
                        raise RuntimeError('MQTT 桥接客户端已断开')
                    packet = received.publish_packet
                    receive = asyncio.create_task(client.deliver_message())
                    if packet and not packet.retain_flag:
                        pending = await self._command(packet.payload.data, writer, publish, pending, seen, requests)
                        if pending is not None:
                            routes[pending[0]] = pending[1]
                # Yield to MQTT tasks between batches without throttling a capture backlog.
                await asyncio.sleep(0.025 if chunks.empty() else 0)
        finally:
            if pending and seen[pending[1]].get('stage') == 'delivery_unknown':
                request_id = pending[1]
                seen[request_id] = {'id': request_id, 'stage': 'delivery_unknown',
                                    'error': '命令已写入，断开前未收到响应，请查询状态'}
            if receive is not None:
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
            try:
                await asyncio.wait_for(publish('status', {'attached': False, 'writerReady': False}, retain=True), 1)
            except Exception as exc:
                self._record('MQTT 状态更新失败', str(exc))

    def _confirm_writer(self, writer, parent_requests, input_requests) -> None:
        matches = [parent_requests[key] for key in input_requests if key in parent_requests]
        for fingerprint in input_requests:
            if fingerprint in parent_requests and matches.count(parent_requests[fingerprint]) >= 2:
                writer.confirm(parent_requests[fingerprint])
                if self._parent_probe is not None:
                    self._parent_probe.exports_sync.dispose()
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
        if len(data) > 4096:
            return pending
        try:
            command = json.loads(data)
        except (ValueError, UnicodeError):
            return pending
        if not isinstance(command, dict):
            return pending
        request_id = command.get('id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id):
            return pending
        if request_id in seen:
            await publish(f'reply/{request_id}', seen[request_id], qos=1)
            return pending
        method = command.get('method')
        params = command.get('params', {})
        if not isinstance(method, str) or method not in self.METHODS | {'rpc/respond'} or not isinstance(params, dict):
            error = '不支持的方法'
        elif pending:
            error = '已有命令等待响应'
        elif not writer.ready:
            error = '写端尚未确认，请在 Codex 中切换一次会话'
        elif len(seen) >= 128:
            error = '已达到本次服务运行的命令数量上限'
        else:
            rpc_id = f'csm-mqtt:{uuid.uuid4().hex}'
            message = {'id': rpc_id, 'method': method, 'params': params}
            server_id = params.get('requestId')
            if method == 'rpc/respond':
                if (not isinstance(server_id, (str, int)) or isinstance(server_id, bool)
                        or requests.get(('output', server_id)) not in self.RESPOND_METHODS
                        or not isinstance(params.get('result'), dict)):
                    await publish(f'reply/{request_id}', {'id': request_id, 'error': '审批或提问已失效，或回答格式错误'}, qos=1)
                    return pending
                message = {'id': server_id, 'result': params['result']}
            payload = json.dumps(message) + '\n'
            if len(payload.encode()) > 4096:
                await publish(f'reply/{request_id}', {'id': request_id, 'error': '编码后的命令超过 4 KiB'}, qos=1)
                return pending
            seen[request_id] = {'id': request_id, 'stage': 'delivery_unknown', 'error': '投递处理中，请勿重复执行'}
            try:
                await asyncio.to_thread(writer.write, payload.encode())
            except Exception as exc:
                reply = {'id': request_id, 'error': str(exc), 'stage': 'delivery_unknown'}
                seen[request_id] = reply
                await publish(f'reply/{request_id}', reply, qos=1)
                return pending
            else:
                if method == 'rpc/respond':
                    requests.pop(('output', server_id), None)
                    reply = {'id': request_id, 'result': {'written': True}}
                    seen[request_id] = reply
                    await publish(f'reply/{request_id}', reply, qos=1)
                    return pending
                return rpc_id, request_id, asyncio.get_running_loop().time() + self.RESPONSE_TIMEOUT
        await publish(f'reply/{request_id}', {'id': request_id, 'error': error}, qos=1)
        return pending

    @classmethod
    def _redact(cls, value):
        if isinstance(value, dict):
            return {key: '[已隐藏]' if cls.SENSITIVE.search(key) else cls._redact(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        return value

    @classmethod
    def _observed(cls, message, direction, requests, uncertain):
        method = message.get('method')
        request_id = message.get('id')
        if isinstance(request_id, (str, int)):
            opposite = 'output' if direction == 'input' else 'input'
            if isinstance(method, str):
                requests[(direction, request_id)] = method
                if len(requests) > 2048:
                    requests.popitem(last=False)
            else:
                method = requests.pop((opposite, request_id), None)
                uncertain = uncertain or method is None
        if uncertain or (isinstance(method, str) and re.search(r'auth|login|attestation|credential|oauth|token(?!Usage)', method, re.I)):
            return {key: message[key] for key in ('id', 'method') if key in message} | {'payloadOmitted': True}
        return cls._redact(message)
