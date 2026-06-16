"""Radar MCP-backed bridge tools.

Surface Radar's read-only Kubernetes MCP tools to the investigation agent. A
generic ``invoke_radar_tool`` keeps every Radar tool reachable (so new Radar
tools need no OpenSRE change), and targeted wrappers steer the planner toward
the high-value read tools with the right ``evidence_type``.

Read-only by design (see integration/DECISIONS.md ADR-004): Radar's write tools
(``apply_resource``, ``patch_resource``, ``manage_workload``, ``manage_cronjob``,
``manage_node``, ``manage_gitops``) are intentionally NOT wrapped.
"""

from __future__ import annotations

from app.integrations.radar import (
    RadarConfig,
    RadarToolCallResult,
    build_radar_config,
    describe_radar_error,
    radar_config_from_env,
)
from app.integrations.radar import (
    call_radar_tool as invoke_radar_mcp_tool,
)
from app.integrations.radar import (
    list_radar_tools as list_radar_mcp_tools,
)
from app.tools._telemetry import report_run_error
from app.tools.tool_decorator import tool

RadarParams = dict[str, object]
RadarBridgeResponse = dict[str, object]

# Connection params shared by every tool's input schema so ``extract_params``
# can inject the resolved Radar config at call time.
_CONNECTION_SCHEMA: dict[str, object] = {
    "radar_url": {"type": "string"},
    "radar_mode": {"type": "string"},
    "radar_auth_mode": {"type": "string"},
    "radar_forwarded_user": {"type": "string"},
    "radar_forwarded_groups": {"type": "string"},
    "radar_auth_token": {"type": "string"},
}


def _schema(properties: dict[str, object], required: list[str] | None = None) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {**properties, **_CONNECTION_SCHEMA},
        "required": required or [],
    }


def _first_string(radar: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = str(radar.get(key, "")).strip()
        if value:
            return value
    return None


def _radar_available(sources: dict[str, dict]) -> bool:
    return bool(sources.get("radar", {}).get("connection_verified"))


def _radar_extract_params(sources: dict[str, dict]) -> RadarParams:
    radar = sources.get("radar", {})
    if not radar:
        return {}
    return {
        "radar_url": _first_string(radar, "radar_url", "url"),
        "radar_mode": _first_string(radar, "radar_mode", "mode"),
        "radar_auth_mode": _first_string(radar, "radar_auth_mode", "auth_mode"),
        "radar_forwarded_user": _first_string(radar, "radar_forwarded_user", "forwarded_user"),
        "radar_forwarded_groups": _first_string(
            radar, "radar_forwarded_groups", "forwarded_groups"
        ),
        "radar_auth_token": _first_string(radar, "radar_auth_token", "auth_token"),
    }


def _resolve_config(
    radar_url: str | None,
    radar_mode: str | None,
    radar_auth_mode: str | None,
    radar_forwarded_user: str | None,
    radar_forwarded_groups: str | None,
    radar_auth_token: str | None,
) -> RadarConfig | None:
    env_config = radar_config_from_env()
    if any(
        (
            radar_url,
            radar_mode,
            radar_auth_mode,
            radar_forwarded_user,
            radar_forwarded_groups,
            radar_auth_token,
        )
    ):
        raw: RadarParams = {
            "url": radar_url or (env_config.url if env_config else ""),
            "mode": radar_mode or (env_config.mode if env_config else "streamable-http"),
            "auth_mode": radar_auth_mode or (env_config.auth_mode if env_config else "none"),
            "forwarded_user": radar_forwarded_user
            or (env_config.forwarded_user if env_config else ""),
            "forwarded_groups": radar_forwarded_groups
            or (env_config.forwarded_groups if env_config else ""),
            "auth_token": radar_auth_token or (env_config.auth_token if env_config else ""),
        }
        if not raw["url"]:
            return None
        return build_radar_config(raw)
    return env_config


def _unavailable(
    error: str,
    *,
    tool_name: str | None = None,
    arguments: RadarParams | None = None,
) -> RadarBridgeResponse:
    payload: RadarBridgeResponse = {"source": "radar", "available": False, "error": error}
    if tool_name:
        payload["tool"] = tool_name
    if arguments is not None:
        payload["arguments"] = arguments
    return payload


def _normalize_tool_result(result: RadarToolCallResult) -> RadarBridgeResponse:
    if result.get("is_error"):
        return _unavailable(
            str(result.get("text") or "Radar MCP tool call failed."),
            tool_name=str(result.get("tool", "")).strip() or None,
            arguments=result.get("arguments", {}),
        )
    return {
        "source": "radar",
        "available": True,
        "tool": result.get("tool"),
        "arguments": result.get("arguments", {}),
        "text": result.get("text", ""),
        "structured_content": result.get("structured_content"),
        "content": result.get("content", []),
    }


def _invoke(
    config: RadarConfig,
    *,
    surface_tool_name: str,
    mcp_tool: str,
    arguments: RadarParams,
) -> RadarBridgeResponse:
    """Call one Radar MCP tool and normalize its result."""
    try:
        result = invoke_radar_mcp_tool(config, mcp_tool, arguments)
    except Exception as err:
        report_run_error(
            err,
            tool_name=surface_tool_name,
            source="radar",
            component="app.tools.RadarMCPTool",
            method=f"invoke_radar_mcp_tool('{mcp_tool}')",
            extras={"mcp_tool": mcp_tool, "transport": config.mode},
        )
        return _unavailable(
            describe_radar_error(err, config), tool_name=mcp_tool, arguments=arguments
        )
    return _normalize_tool_result(result)


def _prune(arguments: RadarParams) -> RadarParams:
    """Drop empty/None args so Radar tools apply their own defaults."""
    return {k: v for k, v in arguments.items() if v not in (None, "", [])}


@tool(
    name="list_radar_tools",
    source="radar",
    description="List the tools exposed by the configured Radar Kubernetes MCP server.",
    evidence_type="other",
    side_effect_level="read_only",
    use_cases=[
        "Discovering which Radar cluster tools are available before calling one",
        "Confirming the Radar bridge is reachable during an investigation",
    ],
    surfaces=("investigation", "chat"),
    input_schema=_schema({}),
    is_available=_radar_available,
    extract_params=_radar_extract_params,
)
def list_radar_bridge_tools(
    radar_url: str | None = None,
    radar_mode: str | None = None,
    radar_auth_mode: str | None = None,
    radar_forwarded_user: str | None = None,
    radar_forwarded_groups: str | None = None,
    radar_auth_token: str | None = None,
    **_kwargs: object,
) -> RadarBridgeResponse:
    """List tools available from the configured Radar MCP server."""
    config = _resolve_config(
        radar_url,
        radar_mode,
        radar_auth_mode,
        radar_forwarded_user,
        radar_forwarded_groups,
        radar_auth_token,
    )
    if config is None:
        payload = _unavailable("Radar MCP integration is not configured (set RADAR_MCP_URL).")
        payload["tools"] = []
        return payload
    try:
        tools = list_radar_mcp_tools(config)
    except Exception as err:
        report_run_error(
            err,
            tool_name="list_radar_tools",
            source="radar",
            component="app.tools.RadarMCPTool",
            method="list_radar_mcp_tools",
            extras={"transport": config.mode},
        )
        payload = _unavailable(describe_radar_error(err, config))
        payload["tools"] = []
        return payload
    return {
        "source": "radar",
        "available": True,
        "transport": config.mode,
        "endpoint": config.mcp_url,
        "tools": tools,
    }


@tool(
    name="invoke_radar_tool",
    source="radar",
    description=(
        "Call a named read-only tool on the Radar Kubernetes MCP server "
        "(e.g. get_topology, get_resource, get_changes, search, top_resources). "
        "Use list_radar_tools first if unsure of the name or arguments."
    ),
    evidence_type="other",
    side_effect_level="read_only",
    requires=["tool_name"],
    use_cases=[
        "Fetching live Kubernetes context Radar exposes but OpenSRE has no targeted wrapper for",
        "Following up on a targeted Radar tool with a more specific Radar query",
    ],
    anti_examples=[
        "Do not call Radar write tools (apply_resource, patch_resource, manage_*) — read-only only",
    ],
    surfaces=("investigation", "chat"),
    input_schema=_schema(
        {"tool_name": {"type": "string"}, "arguments": {"type": "object"}},
        required=["tool_name"],
    ),
    is_available=_radar_available,
    extract_params=_radar_extract_params,
)
def invoke_radar_tool(
    tool_name: str | None = None,
    arguments: RadarParams | None = None,
    radar_url: str | None = None,
    radar_mode: str | None = None,
    radar_auth_mode: str | None = None,
    radar_forwarded_user: str | None = None,
    radar_forwarded_groups: str | None = None,
    radar_auth_token: str | None = None,
    **_kwargs: object,
) -> RadarBridgeResponse:
    """Call a named Radar MCP tool."""
    normalized = (tool_name or "").strip()
    if not normalized:
        return _unavailable(
            "tool_name is required to call a Radar MCP tool.", arguments=arguments or {}
        )
    config = _resolve_config(
        radar_url,
        radar_mode,
        radar_auth_mode,
        radar_forwarded_user,
        radar_forwarded_groups,
        radar_auth_token,
    )
    if config is None:
        return _unavailable(
            "Radar MCP integration is not configured (set RADAR_MCP_URL).",
            tool_name=normalized,
            arguments=arguments or {},
        )
    return _invoke(
        config,
        surface_tool_name="invoke_radar_tool",
        mcp_tool=normalized,
        arguments=_prune(arguments or {}),
    )


def _targeted(
    *,
    surface_tool_name: str,
    mcp_tool: str,
    arguments: RadarParams,
    radar_url: str | None,
    radar_mode: str | None,
    radar_auth_mode: str | None,
    radar_forwarded_user: str | None,
    radar_forwarded_groups: str | None,
    radar_auth_token: str | None,
) -> RadarBridgeResponse:
    config = _resolve_config(
        radar_url,
        radar_mode,
        radar_auth_mode,
        radar_forwarded_user,
        radar_forwarded_groups,
        radar_auth_token,
    )
    if config is None:
        return _unavailable(
            "Radar MCP integration is not configured (set RADAR_MCP_URL).",
            tool_name=mcp_tool,
        )
    return _invoke(
        config,
        surface_tool_name=surface_tool_name,
        mcp_tool=mcp_tool,
        arguments=_prune(arguments),
    )


@tool(
    name="get_radar_dashboard",
    source="radar",
    description="Cluster/namespace health triage from Radar: counts, problems, Helm releases.",
    evidence_type="other",
    side_effect_level="read_only",
    use_cases=["Getting a fast inventory of what is unhealthy in the cluster or a namespace"],
    surfaces=("investigation", "chat"),
    input_schema=_schema({"namespace": {"type": "string"}}),
    is_available=_radar_available,
    extract_params=_radar_extract_params,
)
def get_radar_dashboard(
    namespace: str | None = None,
    radar_url: str | None = None,
    radar_mode: str | None = None,
    radar_auth_mode: str | None = None,
    radar_forwarded_user: str | None = None,
    radar_forwarded_groups: str | None = None,
    radar_auth_token: str | None = None,
    **_kwargs: object,
) -> RadarBridgeResponse:
    return _targeted(
        surface_tool_name="get_radar_dashboard",
        mcp_tool="get_dashboard",
        arguments={"namespace": namespace},
        radar_url=radar_url,
        radar_mode=radar_mode,
        radar_auth_mode=radar_auth_mode,
        radar_forwarded_user=radar_forwarded_user,
        radar_forwarded_groups=radar_forwarded_groups,
        radar_auth_token=radar_auth_token,
    )


@tool(
    name="get_radar_issues",
    source="radar",
    description="List the Kubernetes problems Radar currently detects (what's broken right now).",
    evidence_type="other",
    side_effect_level="read_only",
    use_cases=["Enumerating active cluster issues to anchor an investigation"],
    surfaces=("investigation", "chat"),
    input_schema=_schema(
        {
            "namespace": {"type": "string"},
            "severity": {"type": "string"},
            "kind": {"type": "string"},
            "limit": {"type": "integer"},
        }
    ),
    is_available=_radar_available,
    extract_params=_radar_extract_params,
)
def get_radar_issues(
    namespace: str | None = None,
    severity: str | None = None,
    kind: str | None = None,
    limit: int | None = None,
    radar_url: str | None = None,
    radar_mode: str | None = None,
    radar_auth_mode: str | None = None,
    radar_forwarded_user: str | None = None,
    radar_forwarded_groups: str | None = None,
    radar_auth_token: str | None = None,
    **_kwargs: object,
) -> RadarBridgeResponse:
    return _targeted(
        surface_tool_name="get_radar_issues",
        mcp_tool="issues",
        arguments={"namespace": namespace, "severity": severity, "kind": kind, "limit": limit},
        radar_url=radar_url,
        radar_mode=radar_mode,
        radar_auth_mode=radar_auth_mode,
        radar_forwarded_user=radar_forwarded_user,
        radar_forwarded_groups=radar_forwarded_groups,
        radar_auth_token=radar_auth_token,
    )


@tool(
    name="get_radar_events",
    source="radar",
    description="Recent Kubernetes events from Radar, optionally scoped to a namespace/object.",
    evidence_type="events",
    side_effect_level="read_only",
    use_cases=["Pulling recent Warning events around a failing workload"],
    surfaces=("investigation", "chat"),
    input_schema=_schema(
        {
            "namespace": {"type": "string"},
            "kind": {"type": "string"},
            "name": {"type": "string"},
            "limit": {"type": "integer"},
        }
    ),
    is_available=_radar_available,
    extract_params=_radar_extract_params,
)
def get_radar_events(
    namespace: str | None = None,
    kind: str | None = None,
    name: str | None = None,
    limit: int | None = None,
    radar_url: str | None = None,
    radar_mode: str | None = None,
    radar_auth_mode: str | None = None,
    radar_forwarded_user: str | None = None,
    radar_forwarded_groups: str | None = None,
    radar_auth_token: str | None = None,
    **_kwargs: object,
) -> RadarBridgeResponse:
    return _targeted(
        surface_tool_name="get_radar_events",
        mcp_tool="get_events",
        arguments={"namespace": namespace, "kind": kind, "name": name, "limit": limit},
        radar_url=radar_url,
        radar_mode=radar_mode,
        radar_auth_mode=radar_auth_mode,
        radar_forwarded_user=radar_forwarded_user,
        radar_forwarded_groups=radar_forwarded_groups,
        radar_auth_token=radar_auth_token,
    )


@tool(
    name="get_radar_pod_logs",
    source="radar",
    description="Fetch logs for a specific pod/container via Radar (redaction-safe).",
    evidence_type="logs",
    side_effect_level="read_only",
    requires=["namespace", "name"],
    use_cases=["Reading logs of a crashing pod after narrowing to it"],
    surfaces=("investigation", "chat"),
    input_schema=_schema(
        {
            "namespace": {"type": "string"},
            "name": {"type": "string"},
            "container": {"type": "string"},
            "tail_lines": {"type": "integer"},
            "grep": {"type": "string"},
            "previous": {"type": "boolean"},
        },
        required=["namespace", "name"],
    ),
    is_available=_radar_available,
    extract_params=_radar_extract_params,
)
def get_radar_pod_logs(
    namespace: str | None = None,
    name: str | None = None,
    container: str | None = None,
    tail_lines: int | None = None,
    grep: str | None = None,
    previous: bool | None = None,
    radar_url: str | None = None,
    radar_mode: str | None = None,
    radar_auth_mode: str | None = None,
    radar_forwarded_user: str | None = None,
    radar_forwarded_groups: str | None = None,
    radar_auth_token: str | None = None,
    **_kwargs: object,
) -> RadarBridgeResponse:
    if not (namespace or "").strip() or not (name or "").strip():
        return _unavailable("namespace and name are required for get_radar_pod_logs.")
    return _targeted(
        surface_tool_name="get_radar_pod_logs",
        mcp_tool="get_pod_logs",
        arguments={
            "namespace": namespace,
            "name": name,
            "container": container,
            "tail_lines": tail_lines,
            "grep": grep,
            "previous": previous,
        },
        radar_url=radar_url,
        radar_mode=radar_mode,
        radar_auth_mode=radar_auth_mode,
        radar_forwarded_user=radar_forwarded_user,
        radar_forwarded_groups=radar_forwarded_groups,
        radar_auth_token=radar_auth_token,
    )


@tool(
    name="diagnose_radar_resource",
    source="radar",
    description=(
        "Radar's heuristic diagnosis of a workload or GitOps resource: aggregates logs, "
        "events, startup blockers, recent changes, and related issues."
    ),
    evidence_type="other",
    side_effect_level="read_only",
    requires=["kind", "namespace", "name"],
    use_cases=["Getting a one-shot diagnosis of a broken Deployment/Pod/Application"],
    surfaces=("investigation", "chat"),
    input_schema=_schema(
        {
            "kind": {"type": "string"},
            "namespace": {"type": "string"},
            "name": {"type": "string"},
            "container": {"type": "string"},
            "tail_lines": {"type": "integer"},
        },
        required=["kind", "namespace", "name"],
    ),
    is_available=_radar_available,
    extract_params=_radar_extract_params,
)
def diagnose_radar_resource(
    kind: str | None = None,
    namespace: str | None = None,
    name: str | None = None,
    container: str | None = None,
    tail_lines: int | None = None,
    radar_url: str | None = None,
    radar_mode: str | None = None,
    radar_auth_mode: str | None = None,
    radar_forwarded_user: str | None = None,
    radar_forwarded_groups: str | None = None,
    radar_auth_token: str | None = None,
    **_kwargs: object,
) -> RadarBridgeResponse:
    if not all((kind or "").strip() and (v or "").strip() for v in (kind, namespace, name)):
        return _unavailable("kind, namespace, and name are required for diagnose_radar_resource.")
    return _targeted(
        surface_tool_name="diagnose_radar_resource",
        mcp_tool="diagnose",
        arguments={
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "container": container,
            "tail_lines": tail_lines,
        },
        radar_url=radar_url,
        radar_mode=radar_mode,
        radar_auth_mode=radar_auth_mode,
        radar_forwarded_user=radar_forwarded_user,
        radar_forwarded_groups=radar_forwarded_groups,
        radar_auth_token=radar_auth_token,
    )


@tool(
    name="get_radar_neighborhood",
    source="radar",
    description="Radar topology subgraph around a resource (owners, dependents, related objects).",
    evidence_type="topology",
    side_effect_level="read_only",
    requires=["kind", "name"],
    use_cases=["Mapping what a failing resource depends on or affects before going deeper"],
    surfaces=("investigation", "chat"),
    input_schema=_schema(
        {
            "kind": {"type": "string"},
            "name": {"type": "string"},
            "namespace": {"type": "string"},
            "hops": {"type": "integer"},
            "max_nodes": {"type": "integer"},
        },
        required=["kind", "name"],
    ),
    is_available=_radar_available,
    extract_params=_radar_extract_params,
)
def get_radar_neighborhood(
    kind: str | None = None,
    name: str | None = None,
    namespace: str | None = None,
    hops: int | None = None,
    max_nodes: int | None = None,
    radar_url: str | None = None,
    radar_mode: str | None = None,
    radar_auth_mode: str | None = None,
    radar_forwarded_user: str | None = None,
    radar_forwarded_groups: str | None = None,
    radar_auth_token: str | None = None,
    **_kwargs: object,
) -> RadarBridgeResponse:
    if not (kind or "").strip() or not (name or "").strip():
        return _unavailable("kind and name are required for get_radar_neighborhood.")
    return _targeted(
        surface_tool_name="get_radar_neighborhood",
        mcp_tool="get_neighborhood",
        arguments={
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "hops": hops,
            "max_nodes": max_nodes,
        },
        radar_url=radar_url,
        radar_mode=radar_mode,
        radar_auth_mode=radar_auth_mode,
        radar_forwarded_user=radar_forwarded_user,
        radar_forwarded_groups=radar_forwarded_groups,
        radar_auth_token=radar_auth_token,
    )
