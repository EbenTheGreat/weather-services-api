"""
conftest.py — Shared fixtures for the entire test suite.

Strategy:
- All DB tests use an in-memory SQLite engine (no Supabase needed).
- All HTTP tests use FastAPI's TestClient with overridden dependencies.
- The router.py module-level singletons (cache_service, api_service) are patched
  per-test so weather calls never hit the real OpenWeather API.
- The AI agent is never called for real — it is mocked at the orchestrator level.
"""
import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool

from models import Units, WeatherResponse


# ─────────────────────────────────────────────────────────────
# IN-MEMORY DATABASE ENGINE
# ─────────────────────────────────────────────────────────────

@pytest.fixture(name="engine")
def engine_fixture():
    """
    Fresh in-memory SQLite engine for every test.
    StaticPool ensures the same connection is reused within
    a test so that data written in one step is visible in the next.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)


@pytest.fixture(name="db_session")
def db_session_fixture(engine):
    """Plain SQLModel Session backed by in-memory SQLite."""
    with Session(engine) as session:
        yield session


# ─────────────────────────────────────────────────────────────
# MOCK WEATHER RESPONSE FACTORY
# ─────────────────────────────────────────────────────────────

MOCK_OWM_PAYLOAD = {
    "name": "London",
    "sys": {"country": "GB"},
    "main": {
        "temp": 15.0,
        "feels_like": 13.0,
        "humidity": 72,
    },
    "weather": [{"description": "light rain"}],
    "wind": {"speed": 5.5},
}


def make_weather_response(**overrides) -> WeatherResponse:
    """Factory for a WeatherResponse. Keyword args override defaults."""
    defaults = dict(
        city="London",
        country_code="GB",
        temperature=15.0,
        feels_like=13.0,
        description="light rain",
        humidity=72,
        wind_speed=5.5,
        units=Units.METRIC,
        fetched_at=datetime.now(UTC),
        cached=False,
        alert=None,
    )
    defaults.update(overrides)
    return WeatherResponse(**defaults)


@pytest.fixture
def mock_weather_response() -> WeatherResponse:
    return make_weather_response()


# ─────────────────────────────────────────────────────────────
# MOCK WEATHER API SERVICE
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def mock_api_service(mock_weather_response):
    """
    A mock WeatherApiService. Returned by get_weather_for_bookmark as an AsyncMock.
    This is also patched directly into router.py's module-level singletons.
    """
    from weather_service import WeatherApiService
    svc = MagicMock(spec=WeatherApiService)
    svc.get_weather_for_bookmark = AsyncMock(return_value=mock_weather_response)
    return svc


# ─────────────────────────────────────────────────────────────
# MOCK ORCHESTRATOR
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def mock_orchestrator():
    """AIOrchestrator whose handle_chat + clear_history are mocked."""
    from ai_layer.orchestrator import AIOrchestrator
    orc = MagicMock(spec=AIOrchestrator)
    orc.handle_chat = AsyncMock(return_value="Here is today's weather!")
    orc.clear_history = MagicMock()
    return orc


# ─────────────────────────────────────────────────────────────
# TEST CLIENT WITH DEPENDENCY OVERRIDES + MODULE PATCHES
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def client(db_session, mock_api_service, mock_orchestrator):
    """
    FastAPI TestClient with:
    - DB session → in-memory SQLite (via dependency override)
    - router.py module-level api_service → mock (patched directly)
    - ai_routes.py dependency → mock orchestrator
    - Fresh FakeRedis cache per test (via patching the class-level client)
    """
    import fakeredis
    from main import app
    from db import get_session
    from ai_layer.ai_routes import get_api_service, get_orchestrator

    # Fresh rate-limit-free cache for every test
    fresh_redis = fakeredis.FakeRedis()

    def override_get_session():
        yield db_session

    def override_get_api_service():
        return mock_api_service

    def override_get_orchestrator():
        return mock_orchestrator

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_api_service] = override_get_api_service
    app.dependency_overrides[get_orchestrator] = override_get_orchestrator

    with (
        # Patch the router module-level singletons
        patch("router.api_service", mock_api_service),
        patch("router.cache_service.cache", fresh_redis),
        patch("router.cache_service._redis_client", fresh_redis),
        TestClient(app, raise_server_exceptions=False) as c,
    ):
        yield c

    app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────
# HELPER: seed a bookmark via the API
# ─────────────────────────────────────────────────────────────

def create_bookmark(client, city="London", country_code="GB", **extra):
    """POST a bookmark and return the response JSON."""
    payload = {"city": city, "countryCode": country_code, **extra}
    resp = client.post("/v1/bookmarks", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()
