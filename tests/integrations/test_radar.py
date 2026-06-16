"""Tests for the Radar MCP bridge integration (config, headers, validation)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

import app.integrations.radar as radar_module
from app.integrations.radar import (
    build_radar_config,
    radar_config_from_env,
    validate_radar_config,
)


def test_mcp_url_appends_mcp_suffix_when_missing() -> None:
    assert build_radar_config({"url": "http://radar:9280"}).mcp_url == "http://radar:9280/mcp"
    # already-suffixed urls are left intact (trailing slash normalized away)
    assert build_radar_config({"url": "http://radar:9280/mcp/"}).mcp_url == "http://radar:9280/mcp"


def test_request_headers_proxy_mode_sets_forwarded_identity() -> None:
    config = build_radar_config(
        {
            "url": "http://radar:9280",
            "auth_mode": "proxy",
            "forwarded_user": "opensre",
            "forwarded_groups": "opensre-readonly",
        }
    )
    headers = config.request_headers
    assert headers["X-Forwarded-User"] == "opensre"
    assert headers["X-Forwarded-Groups"] == "opensre-readonly"


def test_request_headers_none_mode_is_empty() -> None:
    assert build_radar_config({"url": "http://radar:9280"}).request_headers == {}


def test_proxy_mode_requires_forwarded_user() -> None:
    with pytest.raises(ValidationError):
        build_radar_config({"url": "http://radar:9280", "auth_mode": "proxy"})


def test_config_from_env_returns_none_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RADAR_MCP_URL", raising=False)
    assert radar_config_from_env() is None


def test_config_from_env_builds_proxy_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_MCP_URL", "http://radar:9280")
    monkeypatch.setenv("RADAR_AUTH_MODE", "proxy")
    monkeypatch.setenv("RADAR_FORWARDED_USER", "opensre")
    config = radar_config_from_env()
    assert config is not None
    assert config.auth_mode == "proxy"
    assert config.request_headers["X-Forwarded-User"] == "opensre"


def test_validate_config_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_list_tools(_config: Any) -> list[dict[str, Any]]:
        return [
            {"name": "get_dashboard", "description": "", "input_schema": {}},
            {"name": "issues", "description": "", "input_schema": {}},
        ]

    monkeypatch.setattr(radar_module, "list_radar_tools", fake_list_tools)
    result = validate_radar_config(build_radar_config({"url": "http://radar:9280"}))
    assert result.ok is True
    assert result.tool_names == ("get_dashboard", "issues")
    assert "discovered 2 tool(s)" in result.detail


def test_validate_config_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_list_tools(_config: Any) -> list[dict[str, Any]]:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(radar_module, "list_radar_tools", fake_list_tools)
    result = validate_radar_config(build_radar_config({"url": "http://radar:9280"}))
    assert result.ok is False
    assert "validation failed" in result.detail
