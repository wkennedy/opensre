"""End-to-end: OpenSRE's Radar MCP bridge against a live Radar instance.

Prerequisites (otherwise the tests skip):
  - RADAR_MCP_URL points at a reachable Radar /mcp endpoint (e.g.
    http://localhost:9280/mcp). If Radar runs with --auth-mode proxy, also set
    RADAR_AUTH_MODE=proxy and RADAR_FORWARDED_USER.
  - The full-RCA smoke additionally needs an LLM provider key.

Run:
    RADAR_MCP_URL=http://localhost:9280/mcp \
        uv run python -m pytest tests/e2e/radar -m e2e -v

These tests are excluded from the default offline suite (``norecursedirs`` in
pytest.ini).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import pytest

from app.integrations.catalog import resolve_effective_integrations
from app.integrations.radar import radar_config_from_env, validate_radar_config
from app.tools.RadarMCPTool import (
    _radar_available,
    get_radar_dashboard,
    get_radar_issues,
    list_radar_bridge_tools,
)

pytestmark = pytest.mark.e2e

_LLM_CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
)
_RADAR_READ_TOOLS = {"get_dashboard", "get_events", "get_pod_logs", "issues", "diagnose"}
_FIXTURE = Path(__file__).parent / "fixtures" / "k8s_crashloop_alert.json"


def _llm_present() -> bool:
    return any(os.environ.get(var) for var in _LLM_CREDENTIAL_ENV_VARS)


@pytest.fixture(scope="module")
def radar_live() -> None:
    """Skip the module unless a reachable Radar /mcp endpoint is configured."""
    config = radar_config_from_env()
    if config is None:
        pytest.skip("RADAR_MCP_URL is not set")
    result = validate_radar_config(config)
    if not result.ok:
        pytest.skip(f"Radar /mcp not reachable: {result.detail}")


def test_bridge_discovers_radar_read_tools(radar_live: None) -> None:
    """The live Radar MCP server advertises the read tools we depend on."""
    listing = list_radar_bridge_tools()
    assert listing["available"] is True
    tools = cast(list[dict[str, Any]], listing["tools"])
    names = {t["name"] for t in tools}
    missing = _RADAR_READ_TOOLS - names
    assert not missing, f"Radar is missing expected read tools: {sorted(missing)}"


def test_targeted_tools_return_live_cluster_data(radar_live: None) -> None:
    """Targeted wrappers return real, parseable cluster data from Radar."""
    dashboard = get_radar_dashboard()
    assert dashboard["available"] is True
    payload = json.loads(str(dashboard["text"]))
    assert "cluster" in payload and "nodes" in payload

    issues = get_radar_issues(limit=5)
    assert issues["available"] is True
    assert json.loads(str(issues["text"])) is not None  # valid JSON body


def test_radar_resolves_into_effective_integrations(radar_live: None) -> None:
    """With RADAR_MCP_URL set, radar surfaces as an available investigation source."""
    effective = resolve_effective_integrations()
    assert "radar" in effective
    sources = {key: entry.get("config", {}) for key, entry in effective.items()}
    assert _radar_available(sources) is True


def test_radar_tools_available_to_investigation_agent(radar_live: None) -> None:
    """The agent's tool-availability path surfaces the radar tools."""
    from app.agent.investigation import _availability_view
    from app.tools.registry import get_registered_tools

    effective = resolve_effective_integrations()
    sources = {key: entry.get("config", {}) for key, entry in effective.items()}
    view = _availability_view(sources)
    radar_tools = [
        t.name
        for t in get_registered_tools("investigation")
        if t.source == "radar" and t.is_available(view)
    ]
    assert "invoke_radar_tool" in radar_tools
    assert "diagnose_radar_resource" in radar_tools


@pytest.mark.skipif(not _llm_present(), reason="no LLM provider key configured")
def test_full_investigation_completes_with_radar_configured(radar_live: None) -> None:
    """Full RCA smoke: an investigation runs end-to-end with Radar as a source.

    Deterministic proof that radar is wired into the pipeline lives in the
    non-LLM tests above; this only asserts the investigation completes (the LLM
    may or may not choose a radar tool for any given alert).
    """
    from app.pipeline.runners import run_investigation

    raw_alert: dict[str, Any] = json.loads(_FIXTURE.read_text())
    state = run_investigation(raw_alert)
    assert isinstance(state, dict)
    assert str(state.get("root_cause", "")).strip(), "investigation produced no root_cause"
