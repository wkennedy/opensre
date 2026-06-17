"""Tests for the typed remediation-planning endpoint (/remediation)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.services.llm_client as llm_client
from app.remote import server as remote_server
from app.remote.server import RemediationAction, RemediationPlan


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(remote_server, "_AUTH_KEY", "k")
    return TestClient(remote_server.app, raise_server_exceptions=False)


def _stub_plan(
    plan: RemediationPlan, monkeypatch: pytest.MonkeyPatch, capture: dict[str, Any]
) -> None:
    """Make get_llm_for_reasoning().with_structured_output(M).invoke(p) return plan."""

    class _Structured:
        def invoke(self, prompt: str) -> RemediationPlan:
            capture["prompt"] = prompt
            return plan

    class _LLM:
        def with_structured_output(self, _model: Any) -> _Structured:
            return _Structured()

    monkeypatch.setattr(llm_client, "get_llm_for_reasoning", lambda: _LLM())


def test_remediation_requires_api_key(client: TestClient) -> None:
    assert client.post("/remediation", json={}).status_code == 403


def test_remediation_returns_typed_actions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture: dict[str, Any] = {}
    plan = RemediationPlan(
        actions=[
            RemediationAction(
                type="restart", kind="Deployment", namespace="payments", name="api", risk="low"
            ),
            RemediationAction(
                type="scale", kind="Deployment", namespace="payments", name="api", replicas=3
            ),
        ],
        rationale="Restart to clear the wedged state; scale up for headroom.",
    )
    _stub_plan(plan, monkeypatch, capture)

    resp = client.post(
        "/remediation",
        headers={"x-api-key": "k"},
        json={
            "root_cause": "Pods wedged after a transient dependency outage.",
            "report": "## RCA ...",
            "subject": {"kind": "Deployment", "namespace": "payments", "name": "api"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [a["type"] for a in body["actions"]] == ["restart", "scale"]
    assert body["actions"][1]["replicas"] == 3
    # The subject is threaded into the planning prompt.
    assert "payments" in capture["prompt"]


def test_remediation_drops_scale_without_replicas(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = RemediationPlan(
        actions=[
            RemediationAction(
                type="scale", kind="Deployment", namespace="ns", name="x"
            ),  # no replicas
            RemediationAction(type="restart", kind="Deployment", namespace="ns", name="x"),
        ],
    )
    _stub_plan(plan, monkeypatch, {})

    resp = client.post("/remediation", headers={"x-api-key": "k"}, json={"subject": {"name": "x"}})
    assert resp.status_code == 200
    # The malformed scale (no replicas) is dropped; restart remains.
    assert [a["type"] for a in resp.json()["actions"]] == ["restart"]
