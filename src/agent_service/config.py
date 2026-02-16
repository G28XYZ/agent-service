from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


DEFAULT_CONFIG_YAML = """openwebui:
  base_url: "https://openwebui.local"
  verify_tls: false
  endpoints:
    signin: "/api/v1/auths/signin"
    session_check: "/api/v1/auths/"
    models: "/api/models"
    chat_list: "/api/v1/chats/"
    chat_create: "/api/v1/chats/new"
    chat_delete: "/api/v1/chats/{chat_id}"
    chat_completion: "/api/chat/completions"
  credentials:
    username: ""
    password: ""

agent:
  default_model: ""
  project_chat_autobind: true
  project_path: ""
  prompts:
    system: ""
    fallback_tools: ""
    fallback_repair: ""

http:
  timeout_seconds: 30
  retries: 2
  user_agent: "Mozilla/5.0 (AgentService/0.1)"
  use_env_proxy: false
"""


class ConfigMissingError(RuntimeError):
    pass


class EndpointsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    signin: str = "/api/v1/auths/signin"
    session_check: str = "/api/v1/auths/"
    models: str = "/api/models"
    chat_list: str = "/api/v1/chats/"
    chat_create: str = "/api/v1/chats/new"
    chat_delete: str = "/api/v1/chats/{chat_id}"
    chat_completion: str = "/api/chat/completions"

    @field_validator(
        "signin",
        "session_check",
        "models",
        "chat_list",
        "chat_create",
        "chat_delete",
        "chat_completion",
    )
    @classmethod
    def normalize_endpoint(cls, value: str) -> str:
        if not value:
            raise ValueError("Endpoint path must not be empty")
        return value if value.startswith("/") else f"/{value}"


class CredentialsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    username: str = ""
    password: str = ""


class OpenWebUIConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: str = Field(min_length=1)
    verify_tls: bool = False
    endpoints: EndpointsConfig = Field(default_factory=EndpointsConfig)
    credentials: CredentialsConfig = Field(default_factory=CredentialsConfig)

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("openwebui.base_url must start with http:// or https://")
        return value


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    class PromptsConfig(BaseModel):
        model_config = ConfigDict(extra="ignore")

        system: str = ""
        fallback_tools: str = ""
        fallback_repair: str = ""

        @field_validator("system", "fallback_tools", "fallback_repair")
        @classmethod
        def normalize_prompt_strings(cls, value: str) -> str:
            return value.strip()

    default_model: str = ""
    project_chat_autobind: bool = True
    project_path: str = ""
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)

    @field_validator("default_model", "project_path")
    @classmethod
    def normalize_agent_strings(cls, value: str) -> str:
        return value.strip()


class HttpConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timeout_seconds: int = Field(default=30, ge=1)
    retries: int = Field(default=2, ge=0)
    user_agent: str = "Mozilla/5.0 (AgentService/0.1)"
    use_env_proxy: bool = False


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    openwebui: OpenWebUIConfig
    agent: AgentConfig = Field(default_factory=AgentConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)


def resolve_config_path(project_root: Path | None = None) -> Path:
    override = os.getenv("AGENT_SERVICE_CONFIG")
    if override:
        return Path(override).expanduser().resolve()

    root = project_root or Path.cwd()
    return root / ".agent-service" / "config.yaml"


def ensure_config_exists(path: Path) -> bool:
    if path.exists():
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    return True


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or resolve_config_path()
    if not config_path.exists():
        raise ConfigMissingError(f"Config file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    try:
        return AppConfig.model_validate(raw)
    except Exception as exc:
        raise RuntimeError(f"Invalid config format in {config_path}: {exc}") from exc
