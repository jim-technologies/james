"""Unit tests for config.yaml loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_config


def test_load_config_parses_browser_and_concurrency(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "default_backend: codex\n"
        "max_concurrency: 7\n"
        "browser:\n"
        "  profiles_dir: /srv/profiles\n"
        "  default_profile: main\n"
        "mcp:\n"
        "  config_path: mcp/servers.mcp.json\n"
    )
    cfg = load_config(path)
    assert cfg.default_backend == "codex"
    assert cfg.max_concurrency == 7
    assert cfg.browser.profiles_dir == "/srv/profiles"
    assert cfg.browser.default_profile == "main"
    assert cfg.mcp_config_path == "mcp/servers.mcp.json"


def test_load_config_rejects_zero_concurrency(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("max_concurrency: 0\n")
    with pytest.raises(ValueError, match="max_concurrency"):
        load_config(path)


def test_load_config_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("default_backend: claude\n")
    cfg = load_config(path)
    assert cfg.max_concurrency == 4
    assert cfg.browser.profiles_dir == ".browser-profiles"
    assert cfg.browser.default_profile == "default"
    assert cfg.mcp_config_path == ""
    assert cfg.session_store_path == ".james-sessions.json"
    assert cfg.telegram.enabled is False
    assert cfg.http_port is None
    # web dashboard is off by default and loopback-bound
    assert cfg.web.enabled is False
    assert cfg.web.bind_host == "127.0.0.1"
    assert cfg.web.port == 8765
    assert cfg.web.token_env == "JAMES_WEB_PASSWORD"


def test_committed_config_yaml_parses():
    # Guard the shipped config.yaml itself: every key the code reads must
    # parse (content is deploy-specific, so assert shape, not values).
    cfg = load_config(Path(__file__).resolve().parents[2] / "config.yaml")
    assert cfg.default_backend
    assert cfg.max_concurrency >= 1
    assert cfg.session_store_path
    assert cfg.browser.profiles_dir and cfg.browser.default_profile
    assert cfg.web.port > 0 and cfg.web.token_env


def test_load_config_parses_web_block(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "web:\n"
        "  enabled: true\n"
        "  bind_host: 0.0.0.0\n"
        "  port: 9000\n"
        "  username: ops\n"
        "  token_env: WEB_PW\n"
    )
    cfg = load_config(path)
    assert cfg.web.enabled is True
    assert cfg.web.bind_host == "0.0.0.0"
    assert cfg.web.port == 9000
    assert cfg.web.username == "ops"
    assert cfg.web.token_env == "WEB_PW"
