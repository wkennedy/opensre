"""Tests for the stateless conversational follow-up endpoint (/chat)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.services.llm_client as llm_client
from app.remote import server as remote_server


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(remote_server, "_AUTH_KEY", "k")
    return TestClient(remote_server.app, raise_server_exceptions=False)


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


def test_chat_requires_api_key(client: TestClient) -> None:
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 403


def test_chat_empty_message_is_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_client, "get_llm_for_reasoning", lambda: None)
    resp = client.post("/chat", headers={"x-api-key": "k"}, json={"message": "   "})
    assert resp.status_code == 400


def test_chat_grounds_in_context_and_includes_history(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    class _LLM:
        def invoke(self, messages: Any) -> _Resp:
            captured["messages"] = messages
            return _Resp("Because the memory limit was too low.")

    monkeypatch.setattr(llm_client, "get_llm_for_reasoning", lambda: _LLM())

    resp = client.post(
        "/chat",
        headers={"x-api-key": "k"},
        json={
            "message": "Why did it OOM?",
            "context": {
                "root_cause": "OOMKilled: memory limit 32Mi too low",
                "report": "## RCA\nThe container exceeded its limit.",
            },
            "history": [
                {"role": "user", "content": "earlier question"},
                {"role": "assistant", "content": "earlier answer"},
            ],
        },
    )

    assert resp.status_code == 200
    assert resp.json()["reply"] == "Because the memory limit was too low."

    msgs = captured["messages"]
    # System prompt is grounded in the RCA context.
    assert msgs[0]["role"] == "system"
    assert "OOMKilled" in msgs[0]["content"]
    # Prior turns are replayed, and the new question is last.
    assert {"role": "assistant", "content": "earlier answer"} in msgs
    assert msgs[-1] == {"role": "user", "content": "Why did it OOM?"}
