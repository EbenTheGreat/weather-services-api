"""
test_weather_service.py — Unit tests for the three service classes.

- WeatherCacheService  (fakeredis — already the real implementation)
- WeatherApiService    (httpx calls mocked with respx)
- WeatherHistoryService (SQLite in-memory DB)

No FastAPI app is started here — we test the service classes directly.
"""
import json
import uuid
from datetime import datetime, UTC
from unittest.mock import patch, AsyncMock

import pytest
import respx
from fastapi import HTTPException
from httpx import Response as HttpxResponse, Request, TimeoutException, ConnectError

from models import Units, WeatherResponse, WeatherHistory, Bookmark
from weather_service import WeatherApiService, WeatherCacheService, WeatherHistoryService
from tests.conftest import MOCK_OWM_PAYLOAD, make_weather_response


# ─────────────────────────────────────────────────────────────
# CacheService fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def cache():
    """Fresh WeatherCacheService backed by its own FakeRedis instance."""
    svc = WeatherCacheService()
    svc.cache.flushdb()  # start clean
    return svc


@pytest.fixture
def weather_response():
    return make_weather_response()


# ─────────────────────────────────────────────────────────────
# WeatherCacheService
# ─────────────────────────────────────────────────────────────

class TestWeatherCacheService:
    def test_cache_key_format(self, cache):
        key = cache._cache_key("London", "GB", Units.METRIC)
        assert key == "weather:london:gb:metric"

    def test_cache_key_case_insensitive(self, cache):
        k1 = cache._cache_key("LONDON", "GB", Units.METRIC)
        k2 = cache._cache_key("london", "gb", Units.METRIC)
        assert k1 == k2

    def test_cache_miss_returns_none(self, cache):
        result = cache.get_from_cache("London", "GB", Units.METRIC)
        assert result is None

    def test_save_and_retrieve(self, cache, weather_response):
        cache.save_to_cache("London", "GB", Units.METRIC, weather_response)
        result = cache.get_from_cache("London", "GB", Units.METRIC)
        assert result is not None
        assert result.city == "London"
        assert result.temperature == weather_response.temperature

    def test_cached_flag_set_to_true(self, cache, weather_response):
        """Weather stored with cached=False should be returned with cached=True."""
        assert weather_response.cached is False
        cache.save_to_cache("London", "GB", Units.METRIC, weather_response)
        result = cache.get_from_cache("London", "GB", Units.METRIC)
        assert result.cached is True

    def test_different_units_different_entries(self, cache, weather_response):
        imperial = make_weather_response(units=Units.IMPERIAL)
        cache.save_to_cache("London", "GB", Units.METRIC, weather_response)
        cache.save_to_cache("London", "GB", Units.IMPERIAL, imperial)
        assert cache.get_from_cache("London", "GB", Units.METRIC).units == Units.METRIC
        assert cache.get_from_cache("London", "GB", Units.IMPERIAL).units == Units.IMPERIAL

    def test_cache_stats_empty(self, cache):
        stats = cache.get_cache_stats()
        assert stats["total_entries"] == 0
        assert stats["cached_locations"] == []

    def test_cache_stats_populated(self, cache, weather_response):
        cache.save_to_cache("London", "GB", Units.METRIC, weather_response)
        stats = cache.get_cache_stats()
        assert stats["total_entries"] == 1
        assert any("london" in loc for loc in stats["cached_locations"])

    def test_flush_cache_clears_all(self, cache, weather_response):
        cache.save_to_cache("London", "GB", Units.METRIC, weather_response)
        cache.flush_cache()
        assert cache.get_from_cache("London", "GB", Units.METRIC) is None
        assert cache.get_cache_stats()["total_entries"] == 0

    def test_rate_limit_not_exceeded(self, cache):
        """Should not raise when under the limit."""
        from config import settings
        for _ in range(settings.RATE_LIMIT_MAX_REQUESTS):
            cache.check_rate_limit("1.2.3.4")  # should not raise

    def test_rate_limit_exceeded_raises_429(self, cache):
        from config import settings
        for _ in range(settings.RATE_LIMIT_MAX_REQUESTS):
            cache.check_rate_limit("9.9.9.9")
        with pytest.raises(HTTPException) as exc_info:
            cache.check_rate_limit("9.9.9.9")
        assert exc_info.value.status_code == 429
        assert "Retry-After" in exc_info.value.headers

    def test_rate_limit_different_ips_independent(self, cache):
        """Two different IPs share no rate-limit counter."""
        from config import settings
        for _ in range(settings.RATE_LIMIT_MAX_REQUESTS):
            cache.check_rate_limit("10.0.0.1")
        # A different IP should still be fine
        cache.check_rate_limit("10.0.0.2")  # must not raise


# ─────────────────────────────────────────────────────────────
# WeatherApiService
# ─────────────────────────────────────────────────────────────

OWM_URL = "https://api.openweathermap.org/data/2.5/weather"


@pytest.fixture
def api_service():
    cache = WeatherCacheService()
    cache.cache.flushdb()
    return WeatherApiService(cache_service=cache)


class TestWeatherApiService:
    @pytest.mark.asyncio
    @respx.mock
    async def test_get_weather_success(self, api_service):
        respx.get(OWM_URL).mock(return_value=HttpxResponse(200, json=MOCK_OWM_PAYLOAD))
        result = await api_service.get_weather("London", "GB", Units.METRIC)
        assert isinstance(result, WeatherResponse)
        assert result.city == "London"
        assert result.cached is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_weather_404_raises_http_exception(self, api_service):
        respx.get(OWM_URL).mock(return_value=HttpxResponse(404, json={"message": "city not found"}))
        with pytest.raises(HTTPException) as exc_info:
            await api_service.get_weather("Nowhere", "XX", Units.METRIC)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_weather_timeout_raises_504(self, api_service):
        from httpx import TimeoutException, Request
        respx.get(OWM_URL).mock(side_effect=TimeoutException("timeout", request=Request("GET", OWM_URL)))
        with pytest.raises(HTTPException) as exc_info:
            await api_service.get_weather("London", "GB", Units.METRIC)
        assert exc_info.value.status_code == 504

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_weather_request_error_raises_503(self, api_service):
        from httpx import ConnectError, Request
        respx.get(OWM_URL).mock(side_effect=ConnectError("conn failed", request=Request("GET", OWM_URL)))
        with pytest.raises(HTTPException) as exc_info:
            await api_service.get_weather("London", "GB", Units.METRIC)
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_weather_for_bookmark_uses_cache(self, api_service):
        """When the cache already has data, no HTTP call should be made."""
        cached_weather = make_weather_response(cached=True)
        api_service.cache.save_to_cache("London", "GB", Units.METRIC, cached_weather)
        # respx would raise an error if any real HTTP call slips through
        result = await api_service.get_weather_for_bookmark("London", "GB", Units.METRIC)
        assert result.cached is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_weather_for_bookmark_force_refresh_bypasses_cache(self, api_service):
        """force_refresh=True must skip the cache and call the API."""
        cached_weather = make_weather_response(cached=True)
        api_service.cache.save_to_cache("London", "GB", Units.METRIC, cached_weather)
        respx.get(OWM_URL).mock(return_value=HttpxResponse(200, json=MOCK_OWM_PAYLOAD))
        result = await api_service.get_weather_for_bookmark(
            "London", "GB", Units.METRIC, force_refresh=True
        )
        # A fresh call returns cached=False from get_weather()
        assert result.cached is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_weather_saves_to_cache_after_fetch(self, api_service):
        respx.get(OWM_URL).mock(return_value=HttpxResponse(200, json=MOCK_OWM_PAYLOAD))
        await api_service.get_weather_for_bookmark("London", "GB", Units.METRIC)
        cached = api_service.cache.get_from_cache("London", "GB", Units.METRIC)
        assert cached is not None


# ─────────────────────────────────────────────────────────────
# WeatherHistoryService
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def history_service():
    return WeatherHistoryService()


@pytest.fixture
def seeded_bookmark(db_session):
    """A Bookmark row written directly to the in-memory DB."""
    bm = Bookmark(city="London", country_code="GB", units=Units.METRIC)
    db_session.add(bm)
    db_session.commit()
    db_session.refresh(bm)
    return bm


class TestWeatherHistoryService:
    def test_save_history_creates_record(self, db_session, history_service, seeded_bookmark, weather_response):
        record = history_service.save_history(db_session, seeded_bookmark.id, weather_response)
        assert record.id is not None
        assert record.city == weather_response.city
        assert record.temperature == weather_response.temperature
        assert record.bookmark_id == seeded_bookmark.id

    def test_get_history_returns_records(self, db_session, history_service, seeded_bookmark, weather_response):
        history_service.save_history(db_session, seeded_bookmark.id, weather_response)
        history_service.save_history(db_session, seeded_bookmark.id, weather_response)
        results = history_service.get_history(db_session, seeded_bookmark.id, limit=10)
        assert len(results) == 2

    def test_get_history_empty_for_unknown_bookmark(self, db_session, history_service):
        results = history_service.get_history(db_session, uuid.uuid4(), limit=10)
        assert results == []

    def test_get_history_fetches_limit_plus_one(self, db_session, history_service, seeded_bookmark, weather_response):
        """
        The service fetches limit+1 records so the route can detect a next page.
        With 3 records and limit=2, we get 3 items back (2+1).
        """
        for _ in range(3):
            history_service.save_history(db_session, seeded_bookmark.id, weather_response)
        results = history_service.get_history(db_session, seeded_bookmark.id, limit=2)
        assert len(results) == 3  # limit+1

    def test_get_history_ordered_oldest_first(self, db_session, history_service, seeded_bookmark):
        # Use naive datetimes — SQLite strips timezone info
        older = make_weather_response(fetched_at=datetime(2024, 1, 1))
        newer = make_weather_response(fetched_at=datetime(2024, 6, 1))
        history_service.save_history(db_session, seeded_bookmark.id, newer)
        history_service.save_history(db_session, seeded_bookmark.id, older)
        results = history_service.get_history(db_session, seeded_bookmark.id, limit=10)
        assert results[0].fetched_at < results[1].fetched_at

    def test_get_history_cursor_filters_correctly(self, db_session, history_service, seeded_bookmark):
        """Records at or before the cursor timestamp must be excluded."""
        # Use naive datetimes — SQLite strips timezone info
        old = make_weather_response(fetched_at=datetime(2024, 1, 1))
        new = make_weather_response(fetched_at=datetime(2024, 6, 1))
        history_service.save_history(db_session, seeded_bookmark.id, old)
        history_service.save_history(db_session, seeded_bookmark.id, new)

        cursor = datetime(2024, 3, 1)  # naive, between old and new
        results = history_service.get_history(db_session, seeded_bookmark.id, cursor=cursor, limit=10)
        assert len(results) == 1

    def test_set_threshold_updates_bookmark(self, db_session, history_service, seeded_bookmark):
        updated = history_service.set_threshold(db_session, seeded_bookmark.id, 25.0)
        assert updated.temperature_threshold == 25.0

    def test_set_threshold_not_found_raises_404(self, db_session, history_service):
        with pytest.raises(HTTPException) as exc_info:
            history_service.set_threshold(db_session, uuid.uuid4(), 20.0)
        assert exc_info.value.status_code == 404
