from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.utils.path_utils import app_root


_VERSION_PREFIX_PATTERN = re.compile(r"^(\d{8})_(\d+)/$")


@dataclass
class CloudSyncConfig:
    s3_address: str
    bucket_name: str
    account: str
    password: str


@dataclass
class CloudSyncVersion:
    name: str
    prefix: str
    object_count: int
    size: int
    last_modified: str


@dataclass
class CloudSyncFile:
    key: str
    relative_path: str
    size: int
    last_modified: str


class CloudSyncService:
    def __init__(self, config: CloudSyncConfig, root_dir: Path | None = None) -> None:
        self.config = config
        self.root_dir = root_dir or app_root()
        self.auth_dir = self.root_dir / "auth"

    def list_versions(self) -> list[CloudSyncVersion]:
        client = self._create_client()
        prefixes = self._list_version_prefixes(client)
        versions = [self._build_version(client, prefix) for prefix in prefixes]
        return sorted(versions, key=lambda item: self._version_sort_key(item.prefix), reverse=True)

    def sync_auth(self) -> CloudSyncVersion:
        if not self.auth_dir.exists() or not self.auth_dir.is_dir():
            raise RuntimeError(f"找不到 auth 目录: {self.auth_dir}")

        files = [path for path in self.auth_dir.rglob("*") if path.is_file()]
        config_path = self.root_dir / "config.json"
        if not files and not config_path.exists():
            raise RuntimeError("auth 目录为空，且找不到 config.json，没有可同步的文件。")

        client = self._create_client()
        prefix = self._next_version_prefix(client)
        for path in files:
            relative_path = path.relative_to(self.auth_dir).as_posix()
            key = f"{prefix}auth/{relative_path}"
            client.upload_file(str(path), self.config.bucket_name, key)
        if config_path.exists() and config_path.is_file():
            client.upload_file(str(config_path), self.config.bucket_name, f"{prefix}config.json")
        return self._build_version(client, prefix)

    def delete_version(self, prefix: str) -> None:
        self._validate_version_prefix(prefix)
        client = self._create_client()
        keys = self._list_object_keys(client, prefix)
        for index in range(0, len(keys), 1000):
            batch = keys[index : index + 1000]
            if not batch:
                continue
            client.delete_objects(
                Bucket=self.config.bucket_name,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )

    def list_version_files(self, prefix: str) -> list[CloudSyncFile]:
        self._validate_version_prefix(prefix)
        client = self._create_client()
        files: list[CloudSyncFile] = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bucket_name, Prefix=prefix):
            for item in page.get("Contents", []):
                key = str(item.get("Key") or "")
                if not key or key.endswith("/"):
                    continue
                modified = item.get("LastModified")
                files.append(
                    CloudSyncFile(
                        key=key,
                        relative_path=key.removeprefix(prefix),
                        size=int(item.get("Size") or 0),
                        last_modified=modified.strftime("%Y-%m-%d %H:%M:%S") if modified is not None else "",
                    )
                )
        return sorted(files, key=lambda item: item.relative_path)

    def download_file_to(self, key: str, target_path: Path) -> None:
        if not key:
            raise RuntimeError("云端文件不能为空。")
        client = self._create_client()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(self.config.bucket_name, key, str(target_path))
        if not target_path.exists():
            raise RuntimeError(f"文件下载校验失败: {target_path}")

    def delete_file(self, key: str) -> None:
        if not key:
            raise RuntimeError("云端文件不能为空。")
        client = self._create_client()
        client.delete_object(Bucket=self.config.bucket_name, Key=key)

    def delete_prefix(self, prefix: str) -> None:
        if not prefix:
            raise RuntimeError("云端目录不能为空。")
        client = self._create_client()
        keys = self._list_object_keys(client, prefix)
        for index in range(0, len(keys), 1000):
            batch = keys[index : index + 1000]
            if not batch:
                continue
            client.delete_objects(
                Bucket=self.config.bucket_name,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )

    def pull_version(self, prefix: str, apply_version: Callable[[Path], None] | None = None) -> int:
        self._validate_version_prefix(prefix)
        client = self._create_client()
        keys = self._list_object_keys(client, prefix)
        if not keys:
            raise RuntimeError("该版本没有可拉取的文件。")

        downloaded_count = 0
        with tempfile.TemporaryDirectory(prefix="codex_cloud_sync_") as temp_dir:
            temp_root = Path(temp_dir)
            version_root = temp_root / prefix.rstrip("/")
            downloaded_paths: list[Path] = []
            for key in keys:
                relative_key = key.removeprefix(prefix)
                if not relative_key or relative_key.endswith("/"):
                    continue
                target_path = version_root / Path(relative_key)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(self.config.bucket_name, key, str(target_path))
                downloaded_paths.append(target_path)

            if not version_root.exists():
                raise RuntimeError("拉取完成后未找到版本目录。")
            missing_paths = [path for path in downloaded_paths if not path.exists()]
            if missing_paths:
                raise RuntimeError(f"拉取文件校验失败: {missing_paths[0]}")
            downloaded_count = len(downloaded_paths)
            (apply_version or self._apply_pulled_version)(version_root)
        return downloaded_count

    def _create_client(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError("缺少 boto3，请先执行 pip install -r requirement.txt。") from exc

        endpoint_url = self._normalize_endpoint_url(self.config.s3_address)
        if not endpoint_url:
            raise RuntimeError("请先填写 (s3)地址。")
        if not self.config.bucket_name.strip():
            raise RuntimeError("请先填写桶名。")
        if not self.config.account.strip():
            raise RuntimeError("请先填写账号。")
        if not self.config.password:
            raise RuntimeError("请先填写密码。")

        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=self.config.account.strip(),
            aws_secret_access_key=self.config.password,
            region_name="us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def _normalize_endpoint_url(self, endpoint_url: str) -> str:
        value = endpoint_url.strip()
        if not value:
            return ""
        if "://" not in value:
            return f"https://{value}"
        return value

    def _list_version_prefixes(self, client) -> list[str]:
        prefixes: set[str] = set()
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bucket_name, Delimiter="/"):
            for item in page.get("CommonPrefixes", []):
                prefix = str(item.get("Prefix") or "")
                if _VERSION_PREFIX_PATTERN.match(prefix):
                    prefixes.add(prefix)
            for item in page.get("Contents", []):
                key = str(item.get("Key") or "")
                first_part = key.split("/", 1)[0]
                prefix = f"{first_part}/"
                if _VERSION_PREFIX_PATTERN.match(prefix):
                    prefixes.add(prefix)
        return list(prefixes)

    def _build_version(self, client, prefix: str) -> CloudSyncVersion:
        keys = []
        size = 0
        last_modified = ""
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bucket_name, Prefix=prefix):
            for item in page.get("Contents", []):
                keys.append(str(item.get("Key") or ""))
                size += int(item.get("Size") or 0)
                modified = item.get("LastModified")
                if modified is not None:
                    modified_text = modified.strftime("%Y-%m-%d %H:%M:%S")
                    if modified_text > last_modified:
                        last_modified = modified_text
        return CloudSyncVersion(
            name=prefix.rstrip("/"),
            prefix=prefix,
            object_count=len(keys),
            size=size,
            last_modified=last_modified,
        )

    def _next_version_prefix(self, client) -> str:
        today = datetime.now().strftime("%Y%m%d")
        max_version = 0
        for prefix in self._list_version_prefixes(client):
            match = _VERSION_PREFIX_PATTERN.match(prefix)
            if match is None or match.group(1) != today:
                continue
            max_version = max(max_version, int(match.group(2)))
        return f"{today}_{max_version + 1}/"

    def _list_object_keys(self, client, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bucket_name, Prefix=prefix):
            for item in page.get("Contents", []):
                key = str(item.get("Key") or "")
                if key:
                    keys.append(key)
        return keys

    def _apply_pulled_version(self, version_root: Path) -> None:
        for item in version_root.iterdir():
            target_path = self.root_dir / item.name
            if item.name == "auth" and item.is_dir():
                self._clear_directory(target_path)
                self._copy_directory_contents(item, target_path)
            elif item.is_dir():
                if target_path.exists():
                    shutil.rmtree(target_path)
                shutil.copytree(item, target_path)
            else:
                shutil.copy2(item, target_path)

    def _clear_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        for child in path.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    def _copy_directory_contents(self, source: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            destination = target / child.name
            if child.is_dir():
                shutil.copytree(child, destination)
            else:
                shutil.copy2(child, destination)

    def _validate_version_prefix(self, prefix: str) -> None:
        if _VERSION_PREFIX_PATTERN.match(prefix) is None:
            raise RuntimeError(f"无效的云同步版本: {prefix}")

    def _version_sort_key(self, prefix: str) -> tuple[str, int]:
        match = _VERSION_PREFIX_PATTERN.match(prefix)
        if match is None:
            return "", 0
        return match.group(1), int(match.group(2))
