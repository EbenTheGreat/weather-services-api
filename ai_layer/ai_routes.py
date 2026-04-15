import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from ai_layer.ai_models import AIChatRequest, AIChatResponse
from ai_layer.ai_service import WeatherApiDeps, weather_agent
from db import SessionDep
from ai_layer.orchestrator import AIOrchestrator
from weather_service import WeatherApiService, WeatherCacheService

logger = logging.getLogger(__name__)



_cache_service = WeatherCacheService()
_api_service = WeatherApiService(cache_service=_cache_service)
_orchestrator = AIOrchestrator(agent=weather_agent)


def get_api_service() -> WeatherApiService:
    return _api_service


def get_orchestrator() -> AIOrchestrator:
    return _orchestrator


ai_router = APIRouter(prefix="/v1/ai", tags=["ai"])


@ai_router.post("/chat", response_model=AIChatResponse, status_code=status.HTTP_200_OK)
async def chat(
    request: AIChatRequest,
    session: SessionDep,
    api_service: WeatherApiService = Depends(get_api_service),
    orchestrator: AIOrchestrator = Depends(get_orchestrator),
):
    """
    Send a message to the AI weather assistant.

    **Starting a new conversation:**
    Omit `session_id` — a new one is created and returned in the response.

    **Continuing a conversation:**
    Include the `session_id` from the previous response to maintain full
    conversation history (the agent remembers what was discussed).

    **What the AI can do:**
    - List your saved bookmarks
    - Fetch live or cached weather for any city
    - Check which bookmarks are currently exceeding their temperature threshold
    - Compare weather across multiple cities
    - Answer follow-up questions using the conversation context
    """
    # Resolve the session, use existing ID or mint a new one
    session_id = request.session_id or str(uuid.uuid4())

    # Build the dependency bundle for this request
    deps = WeatherApiDeps(
        session=session,
        api_service=api_service,
    )

    try:
        # Run the orchestrator logic (caching, history, model execution, logging)
        reply = await orchestrator.handle_chat(
            prompt=request.prompt,
            session_id=session_id,
            deps=deps,
        )
    except ValueError as exc:
        # Raised when input cannot be processed (e.g. malformed session ID)
        logger.warning("Invalid request to AI chat endpoint: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "Invalid request parameters",
                "message": str(exc),
                "session_id": session_id,
            },
        ) from exc
    except Exception as exc:
        # Catch-all for model failures, DB connectivity issues, etc.
        logger.exception(
            "AI chat failed for session_id=%s prompt_preview='%.80s'",
            session_id,
            request.prompt,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "AI assistant is temporarily unavailable",
                "message": (
                    "The request could not be completed due to an internal error. "
                    "Your session has been preserved, please retry with the same session_id."
                ),
                "session_id": session_id,
                "hint": type(exc).__name__,
            },
        ) from exc

    return AIChatResponse(
        reply=reply,
        session_id=session_id,
    )


@ai_router.delete("/chat/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(
    session_id: uuid.UUID,
    session: SessionDep,
    orchestrator: AIOrchestrator = Depends(get_orchestrator),
):
    """
    Clear the conversation history for a given session from the DB.

    Use this when you want to start a fresh conversation without creating
    a new session_id.
    """
    try:
        orchestrator.clear_history(db_session=session, session_id_str=str(session_id))
    except Exception as exc:
        logger.exception("Failed to clear chat history for session_id=%s", session_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "AI history service is temporarily unavailable",
                "message": (
                    "Could not clear conversation history due to a temporary server issue. "
                    "Please try again in a moment."
                ),
                "session_id": str(session_id),
            },
        ) from exc
