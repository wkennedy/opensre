"""Tests for the feedback / quality-loop endpoint (/feedback)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.remote import server as remote_server


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    monkeypatch.setattr(remote_server, "_AUTH_KEY", "k")
    monkeypatch.setattr(remote_server, "INVESTIGATIONS_DIR", tmp_path)
    return TestClient(remote_server.app, raise_server_exceptions=False)


def test_feedback_requires_api_key(client: TestClient) -> None:
    assert client.post("/feedback", json={"verdict": "up"}).status_code == 403


def test_feedback_appends_jsonl(client: TestClient, tmp_path) -> None:
    r1 = client.post(
        "/feedback",
        headers={"x-api-key": "k"},
        json={
            "investigation_id": "i1",
            "verdict": "down",
            "note": "missed the real cause",
            "kind": "Deployment",
        },
    )
    r2 = client.post(
        "/feedback",
        headers={"x-api-key": "k"},
        json={"investigation_id": "i2", "verdict": "up"},
    )
    assert r1.status_code == 200 and r1.json() == {"ok": True}
    assert r2.status_code == 200

    lines = (tmp_path / "feedback.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["investigation_id"] == "i1" and first["verdict"] == "down"
    assert first["note"] == "missed the real cause" and "at" in first
