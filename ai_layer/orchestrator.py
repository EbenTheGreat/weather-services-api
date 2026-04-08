from db import SessionDep
import fakeredis
import uuid
import time 
import json
import hashlib
from typing import Any
from ai_models import ChatSession, ChatMessage, AILog
from pydantic import TypeAdapter
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest
from sqlmodel import Session, select



messages_adapter = TypeAdapter(list[ModelMessage])

class AIOrchestrator():
    """
    Central orchestrator for AI logic.
    Handles guardrails, metrics, persistence (via postgres), and response caching.
    """

    _redis_client = fakeredis.FakeRedis()
    CACHE_TTL_SECONDS = 3600

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
                return cached_response.decode("utf-8")
        
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
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
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

        



    
    def _load_history(self, db_session: str, session_id: str):
        pass

    def _save_history(self, db_session: str, session_id: str, new_messages: list[ModelMessage]):
        pass

    def _extract_tool_calls(self, messages: list[ModelMessage]):
        pass

    def _log_execution(
        self, db_session: Session, session_id: str, prompt: str,
        tools_used: str | None ,duration_ms: float, tokens: int | None
        ):
        pass





