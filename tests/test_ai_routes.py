"""
test_ai_routes.py — Integration tests for the AI chat endpoints.

Uses the standard TestClient fixture (from conftest) whose AIOrchestrator
is already replaced with a mock — no real LLM calls.
"""
import uuid
import pytest
from unittest.mock import AsyncMock


# ─────────────────────────────────────────────────────────────
# POST /v1/ai/chat
# ─────────────────────────────────────────────────────────────

class TestAiChat:
    def test_new_session_returns_200_with_session_id(self, client):
        resp = client.post("/v1/ai/chat", json={"prompt": "What is the weather in London?"})
        assert resp.status_code == 200
        data = resp.json()
        assert "reply" in data
        assert "sessionId" in data
        # sessionId should be a valid UUID
        uuid.UUID(data["sessionId"])

    def test_reply_matches_mock_orchestrator_output(self, client, mock_orchestrator):
        mock_orchestrator.handle_chat = AsyncMock(return_value="Here is today's weather!")
        resp = client.post("/v1/ai/chat", json={"prompt": "Tell me the weather"})
        assert resp.json()["reply"] == "Here is today's weather!"

    def test_existing_session_id_preserved(self, client):
        session_id = str(uuid.uuid4())
        resp = client.post(
            "/v1/ai/chat",
            json={"prompt": "Continue my chat", "sessionId": session_id},
        )
        assert resp.status_code == 200
        assert resp.json()["sessionId"] == session_id

    def test_empty_prompt_rejected(self, client):
        resp = client.post("/v1/ai/chat", json={"prompt": ""})
        assert resp.status_code == 422

    def test_missing_prompt_rejected(self, client):
        resp = client.post("/v1/ai/chat", json={})
        assert resp.status_code == 422

    def test_value_error_from_orchestrator_returns_422(self, client, mock_orchestrator):
        mock_orchestrator.handle_chat = AsyncMock(side_effect=ValueError("malformed session"))
        resp = client.post(
            "/v1/ai/chat",
            json={"prompt": "This will fail"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["error"] == "Invalid request parameters"
        assert "session_id" in body["detail"]

    def test_generic_exception_from_orchestrator_returns_503(self, client, mock_orchestrator):
        mock_orchestrator.handle_chat = AsyncMock(side_effect=RuntimeError("LLM offline"))
        resp = client.post(
            "/v1/ai/chat",
            json={"prompt": "This will also fail"},
        )
        assert resp.status_code == 503
        body = resp.json()
        assert body["detail"]["error"] == "AI assistant is temporarily unavailable"
        assert body["detail"]["hint"] == "RuntimeError"

    def test_503_response_includes_session_id_for_retry(self, client, mock_orchestrator):
        """Client must receive their session_id even on failure so they can retry."""
        sid = str(uuid.uuid4())
        mock_orchestrator.handle_chat = AsyncMock(side_effect=Exception("boom"))
        resp = client.post(
            "/v1/ai/chat",
            json={"prompt": "Retry this", "sessionId": sid},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["session_id"] == sid

    def test_session_id_alias_accepted(self, client):
        """'sessionId' (camelCase alias) should work in the request body."""
        resp = client.post(
            "/v1/ai/chat",
            json={"prompt": "hello", "sessionId": str(uuid.uuid4())},
        )
        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────
# DELETE /v1/ai/chat/{session_id}
# ─────────────────────────────────────────────────────────────

class TestDeleteChat:
    def test_delete_existing_session_returns_204(self, client):
        session_id = str(uuid.uuid4())
        resp = client.delete(f"/v1/ai/chat/{session_id}")
        assert resp.status_code == 204

    def test_delete_calls_orchestrator_clear_history(self, client, mock_orchestrator):
        session_id = str(uuid.uuid4())
        client.delete(f"/v1/ai/chat/{session_id}")
        mock_orchestrator.clear_history.assert_called_once()
        call_kwargs = mock_orchestrator.clear_history.call_args.kwargs
        assert call_kwargs["session_id_str"] == session_id

    def test_delete_invalid_uuid_returns_422(self, client):
        resp = client.delete("/v1/ai/chat/not-a-valid-uuid")
        assert resp.status_code == 422

    def test_delete_orchestrator_failure_returns_503(self, client, mock_orchestrator):
        from unittest.mock import MagicMock
        mock_orchestrator.clear_history = MagicMock(side_effect=Exception("DB down"))
        resp = client.delete(f"/v1/ai/chat/{uuid.uuid4()}")
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "AI history service is temporarily unavailable"
