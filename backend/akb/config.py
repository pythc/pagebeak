import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AKB_", env_file=ROOT / ".env", extra="ignore")
    database_url: str = Field(default="", repr=False)
    worker_database_url: str = Field(default="", repr=False)
    migration_database_url: str = Field(default="", repr=False)
    storage_dir: Path = ROOT / "data/objects"
    api_port: int = 5876
    web_origin: str = "http://localhost:5877"
    cookie_secure: bool = False
    upload_limit: int = 2 * 1024**3
    default_quota_bytes: int = 10 * 1024**3
    upload_chunk_bytes: int = 8 * 1024**2
    disk_reserve_bytes: int = 256 * 1024**2
    lease_seconds: int = 120
    cloudreve_credentials: Path | None = Field(default=None, repr=False)
    native_cloudreve_url: str = "http://127.0.0.1:5878"
    native_origin: str = "http://localhost:5878"
    native_sync_enabled: bool = False
    # Production API containers use this narrow host service, never a Docker socket.
    preview_socket: Path | None = None
    native_secrets_dir: Path = ROOT / "secrets/native-sessions"
    workbuddy_package_dir: Path = ROOT / "work/workbuddy-private-packages"
    knowledge_engine: Literal["legacy", "tencent", "weknora"] = "tencent"
    weknora_url: str = "http://127.0.0.1:5881"
    weknora_secrets_dir: Path = ROOT / "secrets/weknora"
    agent_task_lease_seconds: int = Field(default=900, ge=60, le=3600)
    memory_core_url: str = "http://127.0.0.1:8420"
    memory_wiki_url: str = "http://127.0.0.1:8421"
    memory_service_key_file: Path = ROOT / "secrets/tencent-memory.key"
    # Disabled until a real HTTPS origin is configured. Never infer it from Host.
    assistant_public_origin: str = ""
    assistant_author_email: str = ""
    passwordless_enabled: bool = False
    provisioner_file: Path | None = Field(default=None, repr=False)
    managed_spaces_dir: Path = ROOT / "secrets/managed-spaces"
    managed_group_id: int = Field(default=2, ge=2)
    auto_space_limit: int = Field(default=500, ge=1, le=100000)
    # Staged cutover: existing browser spaces must be bound before retiring device entry.
    identity_required: bool = False
    identity_providers_file: Path | None = Field(default=None, repr=False)
    # Reverse-domain native app schemes work by default (RFC 8252); short schemes
    # require an operator allowlist. These two retain existing installations.
    assistant_redirect_schemes: list[str] = ["workbuddy", "codebuddy"]

    @field_validator("assistant_redirect_schemes")
    @classmethod
    def safe_native_schemes(cls, values):
        if len(values) > 30 or any(not re.fullmatch(r'[a-z][a-z0-9+.-]{1,79}', value)
                or value in {'javascript', 'data', 'file', 'http', 'https', 'ftp', 'vbscript', 'blob'} for value in values):
            raise ValueError('原生应用回调协议配置无效')
        return values

    @field_validator("cloudreve_credentials", mode="before")
    @classmethod
    def empty_storage_broker_is_disabled(cls, value):
        # An explicit empty environment override must disable the legacy broker,
        # rather than resolve to the application's current working directory.
        return None if value == "" else value

    @field_validator("identity_providers_file", mode="before")
    @classmethod
    def empty_identity_configuration_is_disabled(cls, value):
        return None if value == "" else value

    def object_root(self) -> Path:
        path = self.storage_dir
        return path.resolve() if path.is_absolute() else (ROOT / path).resolve()
