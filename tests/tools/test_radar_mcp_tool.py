"""Tests for the Radar MCP bridge tools (availability, gating, invocation)."""

from __future__ import annotations

from typing import Any

import pytest

import app.tools.RadarMCPTool as radar_tools
from app.tools.RadarMCPTool import (
    _radar_available,
    diagnose_radar_resource,
    get_radar_dashboard,
    invoke_radar_tool,
)


def test_radar_available_gating() -> None:
    assert _radar_available({"radar": {"connection_verified": True}}) is True
    assert _radar_available({"radar": {"connection_verified": False}}) is False
    assert _radar_available({}) is False


def test_tool_reports_unavailable_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RADAR_MCP_URL", raising=False)
    payload = get_radar_dashboard()
    assert payload["available"] is False
    assert "not configured" in str(payload["error"])


def test_invoke_radar_tool_requires_tool_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_MCP_URL", "http://radar:9280")
    payload = invoke_radar_tool(tool_name="  ")
    assert payload["available"] is False
    assert "tool_name is required" in str(payload["error"])


def test_targeted_tool_invokes_mapped_mcp_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_MCP_URL", "http://radar:9280")
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(_config: Any, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append((name, args or {}))
        return {"is_error": False, "text": '{"ok": true}', "tool": name, "arguments": args or {}}

    monkeypatch.setattr(radar_tools, "invoke_radar_mcp_tool", fake_invoke)

    payload = get_radar_dashboard(namespace="payments")
    assert payload["available"] is True
    assert calls == [("get_dashboard", {"namespace": "payments"})]


def test_targeted_tool_prunes_empty_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_MCP_URL", "http://radar:9280")
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_invoke(_config: Any, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append((name, args or {}))
        return {"is_error": False, "text": "{}", "tool": name, "arguments": args or {}}

    monkeypatch.setattr(radar_tools, "invoke_radar_mcp_tool", fake_invoke)

    # container/tail_lines omitted — must not be forwarded as None/empty
    diagnose_radar_resource(kind="Deployment", namespace="payments", name="api")
    assert calls == [
        ("diagnose", {"kind": "Deployment", "namespace": "payments", "name": "api"}),
    ]


def test_error_surfaces_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_MCP_URL", "http://radar:9280")

    def boom(_config: Any, _name: str, _args: dict[str, Any] | None = None) -> dict[str, Any]:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(radar_tools, "invoke_radar_mcp_tool", boom)

    payload = get_radar_dashboard()
    assert payload["available"] is False
    assert "connection refused" in str(payload["error"])
