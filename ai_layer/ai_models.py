from pydantic import BaseModel, Field, ConfigDict
from sqlmodel import SQLModel, Field as SQLField
import uuid
from datetime import datetime, UTC



class ChatSession(SQLModel, table=True):
    """
    Persistent storage for AI sessions
    """
    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime= SQLField(default_factory=lambda: datetime.now(UTC))


class ChatMessage(SQLModel, table=True):
    """
    Individual messages within a chat session.
    Using message_json to store the serialized Pydantic AI ModelMessage.
    """
    id: uuid.UUID= SQLField(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID= SQLField(foreign_key="chatsession.id", index=True)
    message_json: str
    created_at: datetime=SQLField(default_factory=lambda: datetime.now(UTC))


class AILog(SQLModel, table=True):
    """
    Observability layer for tracking prompt usage, latency, and tokens
    """
    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    session_id: str = SQLField(default=None, index=True)
    prompt: str= SQLField(max_length=5000)
    tools_used: str | None = None
    response_time_ms: float
    token_usage: int | None = None
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))


# ─────────────────────────────────────────────
# AI LAYER MODELS
# ─────────────────────────────────────────────
class AIChatRequest(BaseModel):
    """
    For User queries
    """
    prompt: str= Field(..., min_length=1)
    session_id: str | None = Field(None, alias="sessionId", description="Omit for new conversation, provide to continue an existing one.")

    model_config = ConfigDict(populate_by_name=True)


class AIChatResponse(BaseModel):
    """
    For response to user requests
    """
    reply: str
    session_id: str=Field(..., alias="sessionId")

    model_config = ConfigDict(populate_by_name=True)


    




