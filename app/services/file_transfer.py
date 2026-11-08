"""Bounded MQTT file transport shared by the Linux and Windows bridges."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import threading
import time

CHUNK_BYTES = 512 * 1024
CAPABILITIES = {'version': 1, 'chunkBytes': CHUNK_BYTES, 'maxFileBytes': None}
TRANSFER_ID = re.compile(r'[a-f0-9-]{36}')


def frame(metadata: dict, data: bytes = b'') -> bytes:
    header = json.dumps(metadata, ensure_ascii=False).encode('utf-8')
    return struct.pack('>I', len(header)) + header + data


def unframe(payload: bytes) -> tuple[dict, bytes]:
    if len(payload) < 4 or len(payload) > CHUNK_BYTES + 32768:
        raise ValueError('文件传输数据包大小无效')
    size = struct.unpack('>I', payload[:4])[0]
    if size > 32764 or size + 4 > len(payload):
        raise ValueError('文件传输头无效')
    metadata = json.loads(payload[4:4 + size])
    if not isinstance(metadata, dict):
        raise ValueError('文件传输参数无效')
    return metadata, payload[4 + size:]


def checked_path(root: str, relative: str = '') -> Path:
    base = Path(root)
    if not base.is_absolute() or '\x00' in root:
        raise ValueError('项目目录无效')
    parts = relative.split('/') if relative else []
    if any(not part or part in ('.', '..') or re.search(r'[\\<>:"|?*\x00-\x1f]', part)
           or part.endswith(('.', ' ')) or part.lower().startswith('.web-')
           or re.match(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', part, re.I) for part in parts):
        raise ValueError('文件路径越界或包含特殊名称')
    result = base.joinpath(*parts)
    for current in [*reversed(result.parents), result]:
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError('禁止访问符号链接或目录联接')
    return result


def integer(value, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or (value < minimum or (maximum is not None and value > maximum)):
        raise ValueError('文件大小或偏移量无效')
    return value


class FileTransfers:
    def __init__(self):
        self.lock = threading.Lock()

    def handle(self, payload: bytes) -> bytes:
        try:
            metadata, data = unframe(payload)
            with self.lock:
                result, output = self.execute(metadata, data)
            return frame({'result': result}, output)
        except Exception as error:
            return frame({'error': str(error)[:500]})

    def manifest_path(self, root, transfer_id):
        if not isinstance(transfer_id, str) or not TRANSFER_ID.fullmatch(transfer_id):
            raise ValueError('上传标识无效')
        directory = checked_path(root, 'attachments')
        return directory / f'.web-upload-{transfer_id}.json'

    def save(self, path, metadata):
        temporary = path.with_suffix('.tmp')
        if path.is_symlink() or temporary.is_symlink():
            raise ValueError('上传状态路径无效')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(metadata, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def load(self, root, transfer_id):
        manifest = self.manifest_path(root, transfer_id)
        if manifest.is_symlink():
            raise ValueError('上传状态路径无效')
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
        if time.time() - metadata['created'] > 86400:
            raise ValueError('上传已过期，请重新上传')
        target = checked_path(root, metadata['relative'])
        part = target.parent / f'.web-upload-{transfer_id}.part'
        if part.is_symlink():
            raise ValueError('临时上传路径无效')
        if not metadata['complete'] and not part.exists() and target.is_file() and metadata['offset'] == metadata['size']:
            digest = hashlib.sha256()
            with target.open('rb') as stream:
                for block in metadata['hashes']:
                    chunk = stream.read(block['size'])
                    if hashlib.sha256(chunk).hexdigest() != block['sha256']:
                        raise ValueError('文件恢复校验失败')
                    digest.update(chunk)
                if stream.read(1):
                    raise ValueError('文件恢复大小不一致')
            metadata.update(complete=True, sha256=digest.hexdigest())
            self.save(manifest, metadata)
        return manifest, metadata, target, part

    def cleanup(self, directory):
        # Only transport-owned, expired files in a visited attachment directory.
        for manifest in directory.glob('.web-upload-*.json'):
            if manifest.is_symlink() or time.time() - manifest.stat().st_mtime <= 86400:
                continue
            part = manifest.with_suffix('.part')
            if part.is_symlink():
                continue
            part.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)

    def receipt(self, metadata):
        return {key: metadata[key] for key in ('relative', 'name', 'size', 'image', 'offset', 'complete', 'sha256')}

    def start_receipt(self, metadata):
        return self.receipt(metadata)

    def execute(self, request, data):
        operation = request.get('op')
        root = request.get('root')
        if not isinstance(root, str):
            raise ValueError('项目目录无效')
        if operation == 'begin':
            size = integer(request.get('size'))
            relative = request.get('relative', '')
            transfer_id = request.get('transferId')
            manifest = self.manifest_path(root, transfer_id)
            target = checked_path(root, relative)
            if not relative.startswith('attachments/') or target.parent != manifest.parent:
                raise ValueError('附件目录无效')
            if manifest.exists():
                _, metadata, _, _ = self.load(root, transfer_id)
                if any(metadata[key] != request.get(key) for key in ('relative', 'size', 'name', 'image')):
                    raise ValueError('续传文件与原文件不一致')
                if metadata['complete'] and not target.is_file():
                    raise ValueError('附件已删除，请重新上传')
                return self.start_receipt(metadata), b''
            self.cleanup(manifest.parent)
            if target.exists():
                raise ValueError('目标文件已经存在')
            part = target.parent / f'.web-upload-{transfer_id}.part'
            with part.open('xb'):
                pass
            metadata = {key: request.get(key) for key in ('relative', 'size', 'name', 'image')}
            metadata.update(offset=0, hashes=[], created=time.time(), complete=False, sha256=None)
            self.save(manifest, metadata)
            return self.start_receipt(metadata), b''
        if operation in ('status', 'write', 'finish', 'abort'):
            manifest, metadata, target, part = self.load(root, request.get('transferId'))
            if operation == 'abort':
                part.unlink(missing_ok=True)
                manifest.unlink(missing_ok=True)
                return {'aborted': True}, b''
            if metadata['complete']:
                if not target.is_file() or target.stat().st_size != metadata['size']:
                    raise ValueError('附件已删除或发生变化')
                if operation == 'write':
                    offset = integer(request.get('offset'), maximum=metadata['size'])
                    with target.open('rb') as stream:
                        stream.seek(offset)
                        if stream.read(len(data)) != data:
                            raise ValueError('续传文件与原文件不一致')
                return self.receipt(metadata), b''
            # Recover a process crash between writing data and atomically saving progress.
            with part.open('r+b') as stream:
                if part.stat().st_size < metadata['offset']:
                    raise ValueError('临时文件不完整，请重新上传')
                stream.truncate(metadata['offset'])
                if operation == 'write':
                    offset = integer(request.get('offset'))
                    if offset % CHUNK_BYTES or not data or len(data) != min(CHUNK_BYTES, metadata['size'] - offset):
                        raise ValueError('文件块大小无效')
                    digest = hashlib.sha256(data).hexdigest()
                    if digest != request.get('sha256'):
                        raise ValueError('文件块校验失败')
                    if offset < metadata['offset']:
                        stream.seek(offset)
                        if stream.read(len(data)) != data:
                            raise ValueError('重复文件块内容不一致')
                    elif offset == metadata['offset']:
                        stream.seek(offset)
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                        metadata['hashes'].append({'offset': offset, 'size': len(data), 'sha256': digest})
                        metadata['offset'] += len(data)
                        self.save(manifest, metadata)
                    else:
                        raise ValueError('文件块偏移不连续，请查询续传进度')
                if operation == 'finish':
                    if metadata['offset'] != metadata['size']:
                        raise ValueError('上传尚未完成')
                    digest = hashlib.sha256()
                    for block in metadata['hashes']:
                        stream.seek(block['offset'])
                        chunk = stream.read(block['size'])
                        if hashlib.sha256(chunk).hexdigest() != block['sha256']:
                            raise ValueError('文件校验失败')
                        digest.update(chunk)
                    stream.seek(0)
                    header = stream.read(16)
                    if metadata['image']:
                        suffix = target.suffix.lower()
                        valid = ((suffix == '.png' and header.startswith(b'\x89PNG\r\n\x1a\n'))
                                 or (suffix in ('.jpg', '.jpeg') and header.startswith(b'\xff\xd8\xff'))
                                 or (suffix == '.gif' and header[:6] in (b'GIF87a', b'GIF89a'))
                                 or (suffix == '.webp' and header[:4] == b'RIFF' and header[8:12] == b'WEBP'))
                        if not valid:
                            raise ValueError('图片内容与格式不匹配')
                    metadata['sha256'] = digest.hexdigest()
                    if request.get('sha256') and request['sha256'] != metadata['sha256']:
                        raise ValueError('整文件校验失败')
            if operation == 'finish':
                checked_path(root, metadata['relative'])
                if target.exists():
                    raise ValueError('目标文件已经存在')
                os.replace(part, target)
                metadata['complete'] = True
                self.save(manifest, metadata)
            return self.receipt(metadata), b''
        if operation in ('open', 'read'):
            target = checked_path(root, request.get('relative', ''))
            if not target.is_file():
                raise ValueError('请选择文件')
            stat = target.stat()
            size = integer(stat.st_size)
            version = f'{stat.st_size}-{stat.st_mtime_ns}-{stat.st_ino}'
            if operation == 'open':
                return {'size': size, 'version': version}, b''
            if request.get('version') != version:
                raise ValueError('文件在下载期间发生变化，请重新下载')
            offset = integer(request.get('offset'), maximum=size)
            length = integer(request.get('length'), maximum=CHUNK_BYTES)
            with target.open('rb') as stream:
                stream.seek(offset)
                output = stream.read(min(length, size - offset))
            after = target.stat()
            if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('文件在下载期间发生变化，请重新下载')
            return {'offset': offset, 'sha256': hashlib.sha256(output).hexdigest()}, output
        raise ValueError('文件传输操作无效')


if __name__ == '__main__':
    engine = FileTransfers()
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line)
            response = engine.handle(base64.b64decode(request['payload']))
            print(json.dumps({'id': request['id'], 'payload': base64.b64encode(response).decode('ascii')}), flush=True)
        except Exception:
            print(json.dumps({'id': request.get('id'), 'payload': base64.b64encode(frame({'error': '文件服务请求失败'})).decode('ascii')}), flush=True)
