"""Load and validate config.yaml into typed settings.

config.yaml drives structure (which channels run, the default backend, the
allowlists) by convention. Secrets are never stored here — only the *names* of
the environment variables that hold them, which are read at runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    """One channel's settings: whether it runs, its token env var, allowlist."""

    enabled: bool
    token_env: str
    allowed_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    """Browser-profile settings for the `shot` backend."""

    profiles_dir: str
    default_profile: str


@dataclass(frozen=True, slots=True)
class WebConfig:
    """Optional web dashboard settings (off by default; loopback + Basic)."""

    enabled: bool
    bind_host: str
    port: int
    username: str
    token_env: str  # name of the env var holding the Basic-auth password


@dataclass(frozen=True, slots=True)
class Config:
    """Top-level james configuration."""

    default_backend: str
    working_dir: str
    max_concurrency: int
    mcp_config_path: str
    session_store_path: str
    browser: BrowserConfig
    web: WebConfig
    telegram: ChannelConfig
    discord: ChannelConfig
    http_port: int | None


def _channel(raw: Mapping[str, Any], id_key: str) -> ChannelConfig:
    """Parse one channel block, reading its allowlist from ``id_key``."""
    ids = raw.get(id_key) or []
    return ChannelConfig(
        enabled=bool(raw.get("enabled", False)),
        token_env=str(raw.get("token_env", "")),
        allowed_ids=frozenset(int(i) for i in ids),
    )


def load_config(path: str | Path) -> Config:
    """Read config.yaml from ``path`` and return a validated Config."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    channels = data.get("channels", {}) or {}
    server = data.get("server", {}) or {}
    browser = data.get("browser", {}) or {}
    web = data.get("web", {}) or {}
    mcp = data.get("mcp", {}) or {}
    sessions = data.get("sessions", {}) or {}
    port = server.get("http_port")
    max_concurrency = int(data.get("max_concurrency", 4))
    if max_concurrency < 1:
        raise ValueError(f"max_concurrency must be >= 1, got {max_concurrency}")
    return Config(
        default_backend=str(data.get("default_backend", "claude")),
        working_dir=str(data.get("working_dir", ".")),
        max_concurrency=max_concurrency,
        mcp_config_path=str(mcp.get("config_path", "")),
        session_store_path=str(
            sessions.get("store_path", ".james-sessions.json")
        ),
        browser=BrowserConfig(
            profiles_dir=str(browser.get("profiles_dir", ".browser-profiles")),
            default_profile=str(browser.get("default_profile", "default")),
        ),
        web=WebConfig(
            enabled=bool(web.get("enabled", False)),
            bind_host=str(web.get("bind_host", "127.0.0.1")),
            port=int(web.get("port", 8765)),
            username=str(web.get("username", "james")),
            token_env=str(web.get("token_env", "JAMES_WEB_PASSWORD")),
        ),
        telegram=_channel(
            channels.get("telegram", {}) or {}, "allowed_chat_ids"
        ),
        discord=_channel(
            channels.get("discord", {}) or {}, "allowed_channel_ids"
        ),
        http_port=int(port) if port else None,
    )
