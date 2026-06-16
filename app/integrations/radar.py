"""Shared Radar bridge integration helpers.

Radar (https://github.com/skyhook-io/radar) is a Kubernetes visibility tool that
exposes a Model Context Protocol (MCP) server with read-only cluster tools
(topology, events, logs, audit, RBAC, GitOps). This module centralizes Radar
bridge configuration, validation, and tool-calling so the onboarding wizard,
verify CLI, and investigation flows share the same transport logic — mirroring
``app/integrations/openclaw.py`` and ``app/integrations/github_mcp.py``.

Supported transports:
  - streamable-http  (default) — HTTP-based MCP via Streamable HTTP (Radar /mcp)
  - sse              — Server-Sent Events MCP transport

Authentication (see integration/DECISIONS.md ADR-005): Radar has no static
bearer / service-token path. Against ``--auth-mode proxy`` Radar trusts
``X-Forwarded-User`` / ``X-Forwarded-Groups`` request headers, so the bridge
emits a fixed read-only identity via those headers. ``--auth-mode none`` needs
no headers. An optional bearer ``auth_token`` is supported for deployments that
front Radar with an oauth2-proxy.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Coroutine, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal, cast

import httpx
from mcp import ClientSession, types  # type: ignore[import-not-found]
from mcp.client.sse import sse_client  # type: ignore[import-not-found]
from pydantic import Field, field_validator, model_validator
from typing_extensions import TypedDict

from app.integrations._validation_helpers import report_validation_failure
from app.integrations.mcp_streamable_http_compat import streamable_http_client
from app.strict_config import StrictConfigModel

logger = logging.getLogger(__name__)

DEFAULT_RADAR_MCP_MODE: Literal["streamable-http", "sse"] = "streamable-http"
DEFAULT_RADAR_USER_HEADER = "X-Forwarded-User"
DEFAULT_RADAR_GROUPS_HEADER = "X-Forwarded-Groups"


class RadarToolDescriptor(TypedDict):
    """A tool exposed by the Radar MCP server."""

    name: str
    description: str
    input_schema: object | None


class RadarContentItem(TypedDict, total=False):
    """Normalized content item returned by an MCP tool call."""

    type: str
    text: str
    uri: str
    mime_type: str


class RadarToolCallResult(TypedDict, total=False):
    """Normalized response from a Radar MCP tool call."""

    is_error: bool
    text: str
    content: list[RadarContentItem]
    structured_content: object | None
    tool: str
    arguments: dict[str, object]


class RadarConfig(StrictConfigModel):
    """Normalized Radar bridge connection settings."""

    url: str = ""
    mode: Literal["streamable-http", "sse"] = DEFAULT_RADAR_MCP_MODE
    auth_mode: Literal["none", "proxy"] = "none"
    forwarded_user: str = ""
    forwarded_groups: str = ""
    user_header: str = DEFAULT_RADAR_USER_HEADER
    groups_header: str = DEFAULT_RADAR_GROUPS_HEADER
    auth_token: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=15.0, gt=0)
    integration_id: str = ""

    @field_validator("url", mode="before")
    @classmethod
    def _normalize_url(cls, value: object) -> str:
        return str(value or "").strip().rstrip("/")

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: object) -> str:
        normalized = str(value or DEFAULT_RADAR_MCP_MODE).strip().lower()
        return normalized or DEFAULT_RADAR_MCP_MODE

    @field_validator("auth_mode", mode="before")
    @classmethod
    def _normalize_auth_mode(cls, value: object) -> str:
        normalized = str(value or "none").strip().lower()
        return normalized or "none"

    @field_validator("auth_token", mode="before")
    @classmethod
    def _normalize_auth_token(cls, value: object) -> str:
        token = str(value or "").strip()
        if token.lower().startswith("bearer "):
            token = token.split(None, 1)[1].strip()
        return token

    @field_validator("user_header", mode="before")
    @classmethod
    def _normalize_user_header(cls, value: object) -> str:
        return str(value or "").strip() or DEFAULT_RADAR_USER_HEADER

    @field_validator("groups_header", mode="before")
    @classmethod
    def _normalize_groups_header(cls, value: object) -> str:
        return str(value or "").strip() or DEFAULT_RADAR_GROUPS_HEADER

    @field_validator("headers", mode="before")
    @classmethod
    def _normalize_headers(cls, value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {str(k): str(v).strip() for k, v in value.items() if str(v).strip()}

    @model_validator(mode="after")
    def _validate_transport_requirements(self) -> RadarConfig:
        if not self.url:
            raise ValueError(f"Radar MCP mode '{self.mode}' requires a non-empty url.")
        if self.auth_mode == "proxy" and not self.forwarded_user:
            raise ValueError(
                "Radar auth_mode 'proxy' requires forwarded_user "
                "(set RADAR_FORWARDED_USER or pass forwarded_user in config)."
            )
        return self

    @property
    def is_configured(self) -> bool:
        return bool(self.url)

    @property
    def mcp_url(self) -> str:
        """Full Radar MCP endpoint; appends ``/mcp`` when only a base url is given."""
        if not self.url:
            return ""
        return self.url if self.url.endswith("/mcp") else f"{self.url}/mcp"

    @property
    def request_headers(self) -> dict[str, str]:
        headers = {k: v for k, v in self.headers.items() if v}
        if self.auth_mode == "proxy" and self.forwarded_user:
            headers.setdefault(self.user_header, self.forwarded_user)
            if self.forwarded_groups:
                headers.setdefault(self.groups_header, self.forwarded_groups)
        if self.auth_token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers


@dataclass(frozen=True)
class RadarValidationResult:
    """Result of validating a Radar bridge integration."""

    ok: bool
    detail: str
    tool_names: tuple[str, ...] = field(default_factory=tuple)


def build_radar_config(raw: Mapping[str, object] | None) -> RadarConfig:
    """Build a normalized Radar config object from env/store data."""
    payload = dict(raw or {})
    allowed = set(RadarConfig.model_fields)
    sanitized = {key: value for key, value in payload.items() if key in allowed}
    return RadarConfig.model_validate(sanitized)


def radar_config_from_env() -> RadarConfig | None:
    """Load a Radar bridge config from environment variables."""
    url = os.getenv("RADAR_MCP_URL", "").strip()
    if not url:
        return None
    return build_radar_config(
        {
            "url": url,
            "mode": os.getenv("RADAR_MCP_MODE", DEFAULT_RADAR_MCP_MODE).strip().lower(),
            "auth_mode": os.getenv("RADAR_AUTH_MODE", "none").strip().lower(),
            "forwarded_user": os.getenv("RADAR_FORWARDED_USER", "").strip(),
            "forwarded_groups": os.getenv("RADAR_FORWARDED_GROUPS", "").strip(),
            "auth_token": os.getenv("RADAR_AUTH_TOKEN", "").strip(),
        }
    )


def describe_radar_error(err: BaseException, config: RadarConfig) -> str:
    """Render a Radar bridge error with a short, actionable hint."""
    if isinstance(err, BaseExceptionGroup):
        parts: list[str] = []
        for sub in err.exceptions:
            parts.append(describe_radar_error(sub, config))
        detail = "; ".join(dict.fromkeys(p for p in parts if p))
    elif isinstance(err, httpx.HTTPStatusError):
        status = err.response.status_code
        detail = f"HTTP {status} from {err.request.method} {err.request.url}"
        if status in (401, 403):
            detail += (
                " — Radar rejected the request. If Radar runs with --auth-mode proxy, "
                "set RADAR_AUTH_MODE=proxy and RADAR_FORWARDED_USER to a read-only identity."
            )
    elif isinstance(err, httpx.ConnectError):
        detail = f"Could not connect to {config.mcp_url}: {err}"
    elif isinstance(err, TimeoutError):
        detail = f"Radar MCP call timed out after {config.timeout_seconds:.1f}s"
    else:
        detail = str(err).strip() or err.__class__.__name__
    return detail


@asynccontextmanager
async def _open_radar_session(config: RadarConfig) -> AsyncIterator[ClientSession]:
    """Open an MCP client session for Radar using the configured transport."""
    stack = AsyncExitStack()
    try:
        if config.mode == "sse":
            read_stream, write_stream = await stack.enter_async_context(
                sse_client(
                    config.mcp_url,
                    headers=config.request_headers,
                    timeout=config.timeout_seconds,
                    sse_read_timeout=max(60.0, config.timeout_seconds),
                )
            )
        elif config.mode == "streamable-http":
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=config.request_headers,
                    timeout=config.timeout_seconds,
                )
            )
            read_stream, write_stream, _ = await stack.enter_async_context(
                streamable_http_client(
                    config.mcp_url,
                    http_client=http_client,
                    headers=config.request_headers,
                    timeout=config.timeout_seconds,
                    sse_read_timeout=max(60.0, config.timeout_seconds),
                )
            )
        else:
            raise ValueError(
                f"Unsupported Radar MCP mode '{config.mode}'. "
                "Supported modes: streamable-http, sse."
            )

        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        yield session
    finally:
        await stack.aclose()


def _run_async(coro: Coroutine[object, object, object]) -> object:
    return asyncio.run(coro)


def _tool_result_to_dict(result: types.CallToolResult) -> RadarToolCallResult:
    text_parts: list[str] = []
    content_items: list[RadarContentItem] = []

    for item in result.content:
        if isinstance(item, types.TextContent):
            text_parts.append(item.text)
            content_items.append({"type": "text", "text": item.text})
        elif isinstance(item, types.EmbeddedResource):
            resource = item.resource
            if isinstance(resource, types.TextResourceContents):
                content_items.append(
                    {"type": "resource_text", "uri": str(resource.uri), "text": resource.text}
                )
                text_parts.append(resource.text)
            elif isinstance(resource, types.BlobResourceContents):
                content_items.append(
                    {
                        "type": "resource_blob",
                        "uri": str(resource.uri),
                        "mime_type": resource.mimeType or "",
                    }
                )
        else:
            content_items.append({"type": getattr(item, "type", "unknown")})

    structured = getattr(result, "structuredContent", None)
    text_output = "\n".join(part.strip() for part in text_parts if part.strip()).strip()
    return {
        "is_error": bool(result.isError),
        "text": text_output,
        "content": content_items,
        "structured_content": structured,
    }


async def _list_tools_async(config: RadarConfig) -> list[types.Tool]:
    async with _open_radar_session(config) as session:
        result = await session.list_tools()
        return list(result.tools)


def list_radar_tools(config: RadarConfig) -> list[RadarToolDescriptor]:
    """List available tools from the Radar MCP server."""
    tools = cast(list[types.Tool], _run_async(_list_tools_async(config)))
    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": getattr(tool, "inputSchema", None),
        }
        for tool in tools
    ]


async def _call_tool_async(
    config: RadarConfig,
    tool_name: str,
    arguments: dict[str, object] | None = None,
) -> RadarToolCallResult:
    async with _open_radar_session(config) as session:
        result = await asyncio.wait_for(
            session.call_tool(tool_name, arguments or {}),
            timeout=config.timeout_seconds,
        )
        payload = _tool_result_to_dict(result)
        payload["tool"] = tool_name
        payload["arguments"] = arguments or {}
        return payload


def call_radar_tool(
    config: RadarConfig,
    tool_name: str,
    arguments: dict[str, object] | None = None,
) -> RadarToolCallResult:
    """Call a Radar MCP tool and normalize the result."""
    return cast(RadarToolCallResult, _run_async(_call_tool_async(config, tool_name, arguments)))


def validate_radar_config(config: RadarConfig) -> RadarValidationResult:
    """Validate Radar bridge connectivity by listing available tools."""
    if not config.is_configured:
        return RadarValidationResult(
            ok=False,
            detail="Radar is not configured: provide a URL (set RADAR_MCP_URL).",
        )

    try:
        tools = list_radar_tools(config)
        tool_names = tuple(sorted(t["name"] for t in tools))
        return RadarValidationResult(
            ok=True,
            detail=(
                f"Radar bridge connected via {config.mode} ({config.mcp_url}); "
                f"discovered {len(tool_names)} tool(s)."
            ),
            tool_names=tool_names,
        )
    except Exception as err:
        report_validation_failure(
            err,
            logger=logger,
            integration="radar",
            method="validate_radar_config",
        )
        return RadarValidationResult(
            ok=False,
            detail=f"Radar bridge validation failed: {describe_radar_error(err, config)}",
        )
