"""
test_ai_orchestrator.py — Unit tests for AIOrchestrator.

The pydantic-ai Agent is mocked — no real LLM calls are made.
The DB is the in-memory SQLite fixture from conftest.
"""
import json
import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart
from sqlmodel import select

from ai_layer.ai_models import ChatSession, ChatMessage, AILog
from ai_layer.orchestrator import AIOrchestrator


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _make_orchestrator(agent=None) -> AIOrchestrator:
    """Return an AIOrchestrator with a fresh FakeRedis and optional mock agent."""
    if agent is None:
        agent = MagicMock()
    orc = AIOrchestrator(agent=agent)
    orc.cache.flushdb()
    return orc


def _make_deps(db_session):
    """Minimal deps object with just a session."""
    deps = MagicMock()
    deps.session = db_session
    return deps


def _make_agent_result(text="AI reply", tool_names: list[str] | None = None):
    """Build a fake pydantic-ai agent result."""
    result = MagicMock()
    result.output = text

    usage = MagicMock()
    usage.total_tokens = 42
    result.usage.return_value = usage

    # Build messages with optional ToolCallPart entries
    parts = []
    if tool_names:
        for name in tool_names:
            tc = MagicMock(spec=ToolCallPart)
            tc.tool_name = name
            parts.append(tc)
    parts.append(TextPart(content=text))

    msg = MagicMock()
    msg.parts = parts
    msg.kind = "response"
    result.new_messages.return_value = [msg]

    return result


# ─────────────────────────────────────────────────────────────
# Cache behaviour
# ─────────────────────────────────────────────────────────────

class TestOrchestratorCache:
    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_value_without_running_agent(self, db_session):
        orc = _make_orchestrator()
        deps = _make_deps(db_session)
        session_id = str(uuid.uuid4())
        cache_key = orc._get_cache_key("hello", session_id)
        orc.cache.setex(cache_key, 3600, b"cached reply")

        result = await orc.handle_chat("hello", session_id, deps)
        assert result == "cached reply"
        orc.agent.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_miss_calls_agent(self, db_session):
        agent = MagicMock()
        fake_result = _make_agent_result("fresh reply")
        agent.run = AsyncMock(return_value=fake_result)
        orc = _make_orchestrator(agent)
        deps = _make_deps(db_session)
        session_id = str(uuid.uuid4())

        result = await orc.handle_chat("new question", session_id, deps)
        assert result == "fresh reply"
        agent.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_response_stored_in_cache_after_agent_run(self, db_session):
        agent = MagicMock()
        agent.run = AsyncMock(return_value=_make_agent_result("stored"))
        orc = _make_orchestrator(agent)
        deps = _make_deps(db_session)
        session_id = str(uuid.uuid4())

        await orc.handle_chat("store me", session_id, deps)

        cache_key = orc._get_cache_key("store me", session_id)
        cached = orc.cache.get(cache_key)
        assert cached is not None
        assert cached.decode("utf-8") == "stored"


# ─────────────────────────────────────────────────────────────
# Tool call extraction
# ─────────────────────────────────────────────────────────────

class TestExtractToolCalls:
    def test_no_messages_returns_empty(self):
        orc = _make_orchestrator()
        assert orc._extract_tool_calls([]) == []

    def test_messages_without_parts_skipped(self):
        msg = MagicMock(spec=[])  # no 'parts' attribute
        orc = _make_orchestrator()
        assert orc._extract_tool_calls([msg]) == []

    def test_extracts_tool_names(self):
        orc = _make_orchestrator()
        tc1 = MagicMock(spec=ToolCallPart)
        tc1.tool_name = "get_my_bookmarks"
        tc2 = MagicMock(spec=ToolCallPart)
        tc2.tool_name = "get_weather_for_city"

        msg = MagicMock()
        msg.parts = [tc1, tc2]

        result = orc._extract_tool_calls([msg])
        assert result == ["get_my_bookmarks", "get_weather_for_city"]

    def test_ignores_non_tool_parts(self):
        orc = _make_orchestrator()
        text_part = TextPart(content="hello")
        msg = MagicMock()
        msg.parts = [text_part]
        result = orc._extract_tool_calls([msg])
        assert result == []


# ─────────────────────────────────────────────────────────────
# History: load
# ─────────────────────────────────────────────────────────────

class TestLoadHistory:
    def test_invalid_uuid_returns_empty(self, db_session):
        orc = _make_orchestrator()
        result = orc._load_history(db_session, "not-a-uuid")
        assert result == []

    def test_valid_uuid_no_records_returns_empty(self, db_session):
        orc = _make_orchestrator()
        result = orc._load_history(db_session, str(uuid.uuid4()))
        assert result == []

    def test_loads_existing_messages(self, db_session):
        orc = _make_orchestrator()
        sid = uuid.uuid4()
        # Manually insert a ChatSession and ChatMessage
        db_session.add(ChatSession(id=sid))
        db_session.flush()

        # A minimal valid serialized message list (TextPart response)
        from pydantic_ai.messages import ModelResponse, TextPart
        from ai_layer.orchestrator import messages_adapter

        msg = ModelResponse(parts=[TextPart(content="hello")], model_name="test")
        raw = messages_adapter.dump_json([msg]).decode("utf-8")
        db_session.add(ChatMessage(session_id=sid, message_json=raw))
        db_session.commit()

        history = orc._load_history(db_session, str(sid))
        assert len(history) == 1

    def test_max_history_messages_truncated(self, db_session):
        """Only the last MAX_HISTORY_MESSAGES messages are returned."""
        from pydantic_ai.messages import ModelResponse, TextPart
        from ai_layer.orchestrator import messages_adapter

        orc = _make_orchestrator()
        orc.MAX_HISTORY_MESSAGES = 3
        sid = uuid.uuid4()
        db_session.add(ChatSession(id=sid))
        db_session.flush()

        for i in range(6):
            msg = ModelResponse(parts=[TextPart(content=f"msg {i}")], model_name="test")
            raw = messages_adapter.dump_json([msg]).decode("utf-8")
            db_session.add(ChatMessage(session_id=sid, message_json=raw))
        db_session.commit()

        history = orc._load_history(db_session, str(sid))
        assert len(history) == 3  # MAX_HISTORY_MESSAGES


# ─────────────────────────────────────────────────────────────
# History: save
# ─────────────────────────────────────────────────────────────

class TestSaveHistory:
    def _make_messages(self):
        from pydantic_ai.messages import ModelResponse, TextPart
        return [ModelResponse(parts=[TextPart(content="reply")], model_name="test")]

    def test_creates_chat_session_if_not_exists(self, db_session):
        orc = _make_orchestrator()
        sid = str(uuid.uuid4())
        orc._save_history(db_session, sid, self._make_messages())
        session = db_session.get(ChatSession, uuid.UUID(sid))
        assert session is not None

    def test_reuses_existing_session(self, db_session):
        orc = _make_orchestrator()
        sid = uuid.uuid4()
        db_session.add(ChatSession(id=sid))
        db_session.commit()

        orc._save_history(db_session, str(sid), self._make_messages())
        orc._save_history(db_session, str(sid), self._make_messages())

        sessions = db_session.exec(select(ChatSession)).all()
        assert len(sessions) == 1  # no duplicates

    def test_creates_chat_message_row(self, db_session):
        orc = _make_orchestrator()
        sid = str(uuid.uuid4())
        orc._save_history(db_session, sid, self._make_messages())

        messages = db_session.exec(select(ChatMessage)).all()
        assert len(messages) == 1
        assert messages[0].session_id == uuid.UUID(sid)

    def test_empty_messages_writes_nothing(self, db_session):
        orc = _make_orchestrator()
        orc._save_history(db_session, str(uuid.uuid4()), [])
        assert db_session.exec(select(ChatMessage)).first() is None

    def test_invalid_uuid_writes_nothing(self, db_session):
        orc = _make_orchestrator()
        orc._save_history(db_session, "garbage-id", self._make_messages())
        assert db_session.exec(select(ChatSession)).first() is None


# ─────────────────────────────────────────────────────────────
# Observability logging
# ─────────────────────────────────────────────────────────────

class TestLogExecution:
    def test_log_execution_creates_ai_log_row(self, db_session):
        orc = _make_orchestrator()
        orc._log_execution(db_session, str(uuid.uuid4()), "test prompt", '["tool_a"]', 123.4, 42)
        logs = db_session.exec(select(AILog)).all()
        assert len(logs) == 1
        log = logs[0]
        assert log.prompt == "test prompt"
        assert log.response_time_ms == 123.4
        assert log.token_usage == 42
        assert log.tools_used == '["tool_a"]'

    def test_log_execution_with_null_fields(self, db_session):
        orc = _make_orchestrator()
        orc._log_execution(db_session, str(uuid.uuid4()), "prompt", None, 50.0, None)
        log = db_session.exec(select(AILog)).first()
        assert log.tools_used is None
        assert log.token_usage is None

    def test_long_prompt_truncated_to_5000(self, db_session):
        orc = _make_orchestrator()
        long_prompt = "x" * 6000
        orc._log_execution(db_session, str(uuid.uuid4()), long_prompt, None, 10.0, None)
        log = db_session.exec(select(AILog)).first()
        assert len(log.prompt) == 5000


# ─────────────────────────────────────────────────────────────
# Clear history
# ─────────────────────────────────────────────────────────────

class TestClearHistory:
    def test_clears_messages_for_session(self, db_session):
        orc = _make_orchestrator()
        sid = uuid.uuid4()
        db_session.add(ChatSession(id=sid))
        db_session.flush()
        db_session.add(ChatMessage(session_id=sid, message_json="[]"))
        db_session.commit()

        orc.clear_history(db_session, str(sid))
        assert db_session.exec(select(ChatMessage)).first() is None

    def test_clear_does_not_affect_other_sessions(self, db_session):
        orc = _make_orchestrator()
        sid1 = uuid.uuid4()
        sid2 = uuid.uuid4()
        for sid in (sid1, sid2):
            db_session.add(ChatSession(id=sid))
        db_session.flush()
        for sid in (sid1, sid2):
            db_session.add(ChatMessage(session_id=sid, message_json="[]"))
        db_session.commit()

        orc.clear_history(db_session, str(sid1))
        remaining = db_session.exec(select(ChatMessage)).all()
        assert len(remaining) == 1
        assert remaining[0].session_id == sid2

    def test_clear_invalid_uuid_no_crash(self, db_session):
        orc = _make_orchestrator()
        orc.clear_history(db_session, "garbage-id")  # should not raise

    def test_clear_nonexistent_session_no_crash(self, db_session):
        orc = _make_orchestrator()
        orc.clear_history(db_session, str(uuid.uuid4()))  # should not raise

    @pytest.mark.asyncio
    async def test_agent_exception_logs_and_re_raises(self, db_session):
        """If agent.run() fails, the error is logged and then re-raised."""
        agent = MagicMock()
        agent.run = AsyncMock(side_effect=RuntimeError("agent exploded"))
        orc = _make_orchestrator(agent)
        deps = _make_deps(db_session)

        with pytest.raises(RuntimeError, match="agent exploded"):
            await orc.handle_chat("boom", str(uuid.uuid4()), deps)

        # An AILog should have been written even on failure
        log = db_session.exec(select(AILog)).first()
        assert log is not None
        assert log.token_usage is None
