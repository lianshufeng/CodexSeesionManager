from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.utils.path_utils import app_root


@dataclass
class AppConfig:
    port: int = 8080
    upstream_proxy: str = "127.0.0.1:1088"
    use_upstream_proxy: bool = True
    auto_load: bool = True
    quota_warmup: bool = False
    lock_model_enabled: bool = True
    load_model: str = "gpt-5.5"
    cloud_s3_address: str = ""
    cloud_bucket_name: str = ""
    cloud_account: str = ""
    cloud_password: str = ""


class AppConfigService:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or app_root() / "config.json"

    def exists(self) -> bool:
        return self.config_path.exists()

    def load(self) -> AppConfig | None:
        if not self.config_path.exists():
            return None
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            port = int(data.get("port") or 8080)
        except (TypeError, ValueError):
            port = 8080
        upstream_proxy = str(data.get("upstream_proxy") or "127.0.0.1:1088")
        use_upstream_proxy = bool(data.get("use_upstream_proxy", True))
        auto_load = bool(data.get("auto_load", True))
        quota_warmup = bool(data.get("quota_warmup", False))
        load_model = str(data.get("load_model")).strip() if "load_model" in data else "gpt-5.5"
        if "lock_model_enabled" in data:
            lock_model_enabled = bool(data.get("lock_model_enabled", True))
        else:
            lock_model_enabled = bool(load_model)
        cloud_storage = data.get("cloud_storage")
        if not isinstance(cloud_storage, dict):
            cloud_storage = {}
        return AppConfig(
            port=port,
            upstream_proxy=upstream_proxy,
            use_upstream_proxy=use_upstream_proxy,
            auto_load=auto_load,
            quota_warmup=quota_warmup,
            lock_model_enabled=lock_model_enabled,
            load_model=load_model,
            cloud_s3_address=str(cloud_storage.get("s3_address") or ""),
            cloud_bucket_name=str(cloud_storage.get("bucket_name") or ""),
            cloud_account=str(cloud_storage.get("account") or ""),
            cloud_password=str(cloud_storage.get("password") or ""),
        )

    def save(self, config: AppConfig) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "port": config.port,
            "upstream_proxy": config.upstream_proxy,
            "use_upstream_proxy": config.use_upstream_proxy,
            "auto_load": config.auto_load,
            "quota_warmup": config.quota_warmup,
            "lock_model_enabled": config.lock_model_enabled,
            "load_model": config.load_model,
            "cloud_storage": {
                "s3_address": config.cloud_s3_address,
                "bucket_name": config.cloud_bucket_name,
                "account": config.cloud_account,
                "password": config.cloud_password,
            },
        }
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
