"""Tests for the /publish endpoint (Proposal 06 — Radar diagnoses via OpenSRE's
Telegram publisher). Verifies channel gating, noise suppression, not-configured
no-op, message formatting/escaping, and the happy-path send."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.remote import server as remote_server
from app.remote.server import PublishRequest, _format_diagnosis_for_telegram


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(remote_server, "_AUTH_KEY", "k")
    return TestClient(remote_server.app, raise_server_exceptions=False)


def _post(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post("/publish", headers={"x-api-key": "k"}, json=body)


def test_publish_requires_api_key(client: TestClient) -> None:
    assert client.post("/publish", json={}).status_code == 403


def test_unsupported_channel_is_reported(client: TestClient) -> None:
    resp = _post(client, {"channel": "carrier-pigeon", "root_cause": "x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["published"] is False and body["reason"] == "unsupported channel"


def test_noise_is_suppressed(client: TestClient) -> None:
    resp = _post(client, {"is_noise": True, "root_cause": "flaky"})
    assert resp.json() == {"published": False, "channel": "telegram", "reason": "noise suppressed"}


def test_not_configured_is_a_clean_noop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No creds resolvable → published:false, never raises.
    monkeypatch.setattr(remote_server, "_resolve_telegram_target", lambda _c: ("", ""))
    resp = _post(client, {"root_cause": "boom"})
    assert resp.json()["published"] is False
    assert resp.json()["reason"] == "telegram not configured"


def test_happy_path_sends_and_threads_deep_link(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_server, "_resolve_telegram_target", lambda _c: ("bot:tok", "123"))
    captured: dict[str, Any] = {}

    def _fake_send(message: str, ctx: dict[str, Any], **kwargs: Any) -> tuple[bool, str]:
        captured["message"] = message
        captured["ctx"] = ctx
        captured["parse_mode"] = kwargs.get("parse_mode")
        return True, ""

    # send_telegram_report is imported lazily inside the handler; patch at source.
    import app.utils.telegram_delivery as tg

    monkeypatch.setattr(tg, "send_telegram_report", _fake_send)

    resp = _post(
        client,
        {
            "root_cause": "OOMKilled: memory limit too low",
            "kind": "Deployment",
            "namespace": "payments",
            "name": "api",
            "resource_url": "https://radar.example.com/workload/Deployment/payments/api",
            "validity_score": 0.95,
            "trigger": "auto",
        },
    )
    assert resp.json() == {"published": True, "channel": "telegram", "reason": ""}
    assert captured["ctx"] == {"bot_token": "bot:tok", "chat_id": "123"}
    assert captured["parse_mode"] == "HTML"
    msg = captured["message"]
    assert "payments/api" in msg
    assert "confidence: 95%" in msg and "trigger: auto" in msg
    assert 'href="https://radar.example.com/workload/Deployment/payments/api"' in msg


def test_send_failure_is_reported_not_raised(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_server, "_resolve_telegram_target", lambda _c: ("bot:tok", "123"))
    import app.utils.telegram_delivery as tg

    monkeypatch.setattr(tg, "send_telegram_report", lambda *_a, **_k: (False, "chat not found"))
    resp = _post(client, {"root_cause": "x"})
    assert resp.status_code == 200
    assert resp.json()["published"] is False
    assert resp.json()["reason"] == "chat not found"


def test_formatter_escapes_html_in_dynamic_fields() -> None:
    msg = _format_diagnosis_for_telegram(
        PublishRequest(root_cause="bad <script>alert(1)</script> & co", name="api<b>")
    )
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg and "&amp; co" in msg
