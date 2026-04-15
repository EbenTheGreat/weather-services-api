from fakeredis._msgs import TOO_MANY_KEYS_MSG
import fakeredis
import uuid
import time 
import json
import hashlib
from typing import Any
from ai_layer.ai_models import ChatSession, ChatMessage, AILog
from pydantic import TypeAdapter
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ToolCallPart
from sqlmodel import Session, select, delete
import logging

logger = logging.getLogger(__name__)



messages_adapter = TypeAdapter(list[ModelMessage])

class AIOrchestrator():
    """
    Central orchestrator for AI logic.
    Handles guardrails, metrics, persistence (via postgres), and response caching.
    """

    _redis_client = fakeredis.FakeRedis()
    CACHE_TTL_SECONDS = 3600
    MAX_HISTORY_MESSAGES = 20

    def __init__(self, agent: Agent):
        self.agent = agent
        self.cache = self._redis_client

    def _get_cache_key(self, prompt: str, session_id: str) -> str:
        #Hash the prompt to keep the key length manageable 
        prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
        key = f"ai_cache:{session_id}:{prompt_hash}"
        return key

    async def handle_chat(self, prompt: str, session_id: str, deps: Any) -> str:
        """
        Execute an agent with caching and observability
        """
        db_session: Session = deps.session

        # 1. Check Redis cache
        cache_key = self._get_cache_key(prompt, session_id)
        if self.cache:
            cached_response = self.cache.get(cache_key)
            if cached_response:
                logger.info(f"AI Cache HIT for session {session_id}")
                return cached_response.decode("utf-8")
        
        logger.info(f"AI Cache MISS for session {session_id}. Running agent...")
        
        # 2. Extract conversation history from PostgreSQL
        history = self._load_history(db_session, session_id)

        # 3. Model Execution
        start_time = time.perf_counter()
        duration_ms = 0.0

        try:
            result = await self.agent.run(
                user_prompt=prompt,
                deps=deps,
                message_history=history,
                model_settings={"max_tokens": 1000}  # Simple guardrail against massive output
            )
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.info(f"Agent execution successful in {duration_ms:.2f}ms")
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            logger.error(f"Agent execution failed after {duration_ms:.2f}ms: {str(e)}")
            self._log_execution(db_session, session_id, prompt, None, duration_ms, None)
            raise



        self._save_history(db_session, session_id, result.new_messages())

        # Extract observability metrics
        usage = result.usage()
        token_usage = usage.total_tokens if usage else None

        tools_invoked = self._extract_tool_calls(result.new_messages())
        tools_str = json.dumps(tools_invoked) if tools_invoked else None

        # Observability Logging
        self._log_execution(db_session, session_id, prompt, tools_str, duration_ms, token_usage)

        # Update cache
        if self.cache:
            self.cache.setex(cache_key, self.CACHE_TTL_SECONDS, result.output)
        return result.output

        
    def _extract_tool_calls(self, messages: list[ModelMessage]) -> list[str]:
        """
        Extract the names of all tools invoked during an agent run.
        Used for observability logging to record which tools were called.
        """
        return [
            part.tool_name
            for msg in messages
            if hasattr(msg, "parts")
            for part in msg.parts
            if isinstance(part, ToolCallPart)
        ]

    
    def _load_history(self, db_session: Session, session_id: str) -> list[ModelMessage]:
        try: 
            sid = uuid.UUID(session_id)
        except ValueError:
            return []

        statement = select(ChatMessage).where(ChatMessage.session_id == sid).order_by(ChatMessage.created_at)
        messages_record = db_session.exec(statement).all()

        history : list[ModelMessage] = []
        for record in messages_record:
            try:
                messages = messages_adapter.validate_json(record.message_json)
                history.extend(messages)
            except Exception as e:
                logger.error(f"Failed to parse chat message for session {session_id}: {e}")
                pass
        
        return history[-self.MAX_HISTORY_MESSAGES:]



    def _save_history(self, db_session: Session, session_id_str: str, new_messages: list[ModelMessage]):
        if not new_messages:
            return
        
        try:
            sid = uuid.UUID(session_id_str)
        except ValueError:
            return

        # Create session if it doesn't exist, flush so Postgres sees it
        # before the ChatMessage FK constraint is checked.
        session_obj = db_session.get(ChatSession, sid)
        if not session_obj:
            session_obj = ChatSession(id=sid)
            db_session.add(session_obj)
            db_session.flush()  # writes ChatSession row before ChatMessage insert

        # Save messages as a single JSON batch per turn
        json_data = messages_adapter.dump_json(new_messages).decode("utf-8")

        msg_record = ChatMessage(
            session_id=sid,
            message_json=json_data
        )

        db_session.add(msg_record)
        db_session.commit()



    def _log_execution(
        self, db_session: Session, session_id: str, prompt: str,
        tools_used: str | None ,duration_ms: float, tokens: int | None
        ):
        """
        To log the records for AI layer
        """
        log_record = AILog(
            session_id=session_id,
            prompt=prompt[:5000],
            tools_used=tools_used,
            response_time_ms=duration_ms,
            token_usage=tokens
        )

        db_session.add(log_record)
        db_session.commit()

    
    def clear_history(self, db_session: Session, session_id_str: str):
        """
        Clear all chat messages for a given session
        """
        try:
            sid = uuid.UUID(session_id_str)
        except ValueError:
            return

        statement = delete(ChatMessage).where(ChatMessage.session_id == sid)
        db_session.exec(statement)
        db_session.commit()







