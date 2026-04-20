"""
test_weather.py — Tests for weather-fetch, history, cache, compare, and bulk routes.

All OpenWeather HTTP calls are handled by the mock_api_service fixture (no real HTTP).
The module-level router.api_service singleton is patched in conftest so the mock is used.
"""
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException, status

from tests.conftest import create_bookmark, make_weather_response
from models import Units


# ─────────────────────────────────────────────────────────────
# GET /v1/weather  (quick lookup)
# ─────────────────────────────────────────────────────────────

class TestQuickWeatherLookup:
    def test_success(self, client):
        resp = client.get("/v1/weather?city=London&country_code=GB")
        assert resp.status_code == 200
        data = resp.json()
        assert data["city"] == "London"
        assert "temperature" in data

    def test_missing_city_param(self, client):
        resp = client.get("/v1/weather?country_code=GB")
        assert resp.status_code == 422

    def test_missing_country_code_param(self, client):
        resp = client.get("/v1/weather?city=London")
        assert resp.status_code == 422

    def test_city_too_short(self, client):
        resp = client.get("/v1/weather?city=&country_code=GB")
        assert resp.status_code == 422

    def test_country_code_too_long(self, client):
        resp = client.get("/v1/weather?city=London&country_code=GBR")
        assert resp.status_code == 422

    def test_country_code_too_short(self, client):
        resp = client.get("/v1/weather?city=London&country_code=G")
        assert resp.status_code == 422

    def test_imperial_units(self, client, mock_api_service):
        imperial_weather = make_weather_response(units=Units.IMPERIAL)
        mock_api_service.get_weather_for_bookmark = AsyncMock(return_value=imperial_weather)
        resp = client.get("/v1/weather?city=London&country_code=GB&units=imperial")
        assert resp.status_code == 200
        assert resp.json()["units"] == "imperial"

    def test_rate_limit_exceeded(self, client):
        """
        Exceed RATE_LIMIT_MAX_REQUESTS in a single window → 429 with Retry-After.
        We use a fresh cache per test (from conftest), so there is no bleed.
        """
        from config import settings
        url = "/v1/weather?city=London&country_code=GB"
        for _ in range(settings.RATE_LIMIT_MAX_REQUESTS + 1):
            resp = client.get(url)
        assert resp.status_code == 429
        assert "retry-after" in resp.headers

    def test_upstream_error_propagates(self, client, mock_api_service):
        """If the upstream service raises an HTTPException, the route propagates it."""
        mock_api_service.get_weather_for_bookmark = AsyncMock(
            side_effect=HTTPException(status_code=502, detail="bad gateway")
        )
        with patch("router.api_service", mock_api_service):
            resp = client.get("/v1/weather?city=London&country_code=GB")
        assert resp.status_code == 502


# ─────────────────────────────────────────────────────────────
# GET /v1/bookmark/{id}/weather
# ─────────────────────────────────────────────────────────────

class TestBookmarkWeather:
    def test_success_returns_weather(self, client):
        bm = create_bookmark(client, "London", "GB")
        resp = client.get(f"/v1/bookmark/{bm['id']}/weather")
        assert resp.status_code == 200
        assert resp.json()["city"] == "London"

    def test_saves_weather_history(self, client, db_session):
        """After fetching weather, a WeatherHistory row is written to the DB."""
        from sqlmodel import select
        from models import WeatherHistory

        bm = create_bookmark(client, "London", "GB")
        resp = client.get(f"/v1/bookmark/{bm['id']}/weather")
        assert resp.status_code == 200

        records = db_session.exec(select(WeatherHistory)).all()
        assert len(records) == 1
        assert records[0].city == "London"

    def test_not_found(self, client):
        resp = client.get(f"/v1/bookmark/{uuid.uuid4()}/weather")
        assert resp.status_code == 404

    def test_invalid_uuid(self, client):
        resp = client.get("/v1/bookmark/not-a-uuid/weather")
        assert resp.status_code == 422

    def test_force_refresh_calls_service(self, client, mock_api_service):
        bm = create_bookmark(client, "London", "GB")
        resp = client.get(f"/v1/bookmark/{bm['id']}/weather?force_refresh=true")
        assert resp.status_code == 200
        mock_api_service.get_weather_for_bookmark.assert_called()
        call_kwargs = mock_api_service.get_weather_for_bookmark.call_args.kwargs
        assert call_kwargs.get("force_refresh") is True


# ─────────────────────────────────────────────────────────────
# GET /v1/bookmarks/{id}/weather/history
# ─────────────────────────────────────────────────────────────

class TestWeatherHistory:
    def _seed_history(self, client, bookmark_id: str, n: int = 3):
        """Trigger n weather fetches to create n history records."""
        for _ in range(n):
            resp = client.get(f"/v1/bookmark/{bookmark_id}/weather")
            assert resp.status_code == 200, resp.text

    def test_not_found(self, client):
        resp = client.get(f"/v1/bookmarks/{uuid.uuid4()}/weather/history")
        assert resp.status_code == 404

    def test_empty_history(self, client):
        bm = create_bookmark(client, "London", "GB")
        resp = client.get(f"/v1/bookmarks/{bm['id']}/weather/history?limit=10")
        assert resp.status_code == 200
        assert resp.json()["data"] == []
        assert resp.json()["nextCursor"] is None

    def test_returns_history_records(self, client):
        bm = create_bookmark(client, "London", "GB")
        self._seed_history(client, bm["id"], n=3)
        resp = client.get(f"/v1/bookmarks/{bm['id']}/weather/history?limit=10")
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 3

    def test_limit_enforced_next_cursor_set(self, client):
        bm = create_bookmark(client, "London", "GB")
        self._seed_history(client, bm["id"], n=3)
        resp = client.get(f"/v1/bookmarks/{bm['id']}/weather/history?limit=2")
        data = resp.json()
        assert len(data["data"]) == 2
        assert data["nextCursor"] is not None

    def test_limit_out_of_range(self, client):
        bm = create_bookmark(client, "London", "GB")
        resp = client.get(f"/v1/bookmarks/{bm['id']}/weather/history?limit=101")
        assert resp.status_code == 422

    def test_cursor_pagination_second_page(self, client, db_session, mock_api_service):
        """
        Seed 3 records with distinct timestamps directly in the DB (no http calls),
        then verify that cursor pagination works correctly.
        """
        from datetime import datetime, timedelta
        from models import WeatherHistory, Bookmark
        import sqlmodel

        bm = create_bookmark(client, "London", "GB")
        bid = uuid.UUID(bm["id"])

        # Seed 3 history records with distinct, well-spaced timestamps
        base = datetime(2024, 1, 1, 12, 0, 0)  # naive, to match SQLite storage
        for i in range(3):
            record = WeatherHistory(
                bookmark_id=bid,
                city="London",
                country_code="GB",
                temperature=15.0 + i,
                feels_like=13.0,
                description="clear sky",
                humidity=60,
                wind_speed=3.0,
                units=Units.METRIC,
                fetched_at=base + timedelta(hours=i),
            )
            db_session.add(record)
        db_session.commit()

        # Page 1 — limit=2, should get records 0 and 1, with a cursor
        first = client.get(f"/v1/bookmarks/{bm['id']}/weather/history?limit=2")
        first_data = first.json()
        assert len(first_data["data"]) == 2
        cursor = first_data["nextCursor"]
        assert cursor is not None

        # Page 2 — pass cursor, should get record 2
        second = client.get(
            f"/v1/bookmarks/{bm['id']}/weather/history?limit=2&cursor={cursor}"
        )
        assert second.status_code == 200
        second_data = second.json()
        assert len(second_data["data"]) == 1
        assert second_data["nextCursor"] is None


# ─────────────────────────────────────────────────────────────
# GET /v1/weather/compare
# ─────────────────────────────────────────────────────────────

class TestCompareWeather:
    def test_compare_success(self, client):
        bm1 = create_bookmark(client, "London", "GB")
        bm2 = create_bookmark(client, "Paris", "FR")
        resp = client.get(f"/v1/weather/compare?ids={bm1['id']}&ids={bm2['id']}")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        for item in items:
            assert "bookmarkId" in item

    def test_compare_missing_bookmark_included_with_error(self, client):
        bm = create_bookmark(client, "London", "GB")
        missing_id = str(uuid.uuid4())
        resp = client.get(f"/v1/weather/compare?ids={bm['id']}&ids={missing_id}")
        assert resp.status_code == 200
        items = resp.json()
        missing_items = [i for i in items if i["bookmarkId"] == missing_id]
        assert len(missing_items) == 1
        assert missing_items[0]["error"] == "Bookmark not found"

    def test_compare_no_ids(self, client):
        resp = client.get("/v1/weather/compare")
        assert resp.status_code == 422

    def test_compare_upstream_error_shown_in_error_field(self, client, mock_api_service):
        """If weather fetch fails for a bookmark, error appears in the response item."""
        bm = create_bookmark(client, "London", "GB")
        mock_api_service.get_weather_for_bookmark = AsyncMock(
            side_effect=Exception("API down")
        )
        with patch("router.api_service", mock_api_service):
            resp = client.get(f"/v1/weather/compare?ids={bm['id']}")
        assert resp.status_code == 200
        item = resp.json()[0]
        assert item["weather"] is None
        assert item["error"] is not None


# ─────────────────────────────────────────────────────────────
# GET /v1/bookmarks/weather/bulk
# ─────────────────────────────────────────────────────────────

class TestBulkWeather:
    def test_bulk_empty(self, client):
        resp = client.get("/v1/bookmarks/weather/bulk")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["data"] == []

    def test_bulk_returns_all(self, client):
        create_bookmark(client, "London", "GB")
        create_bookmark(client, "Paris", "FR")
        resp = client.get("/v1/bookmarks/weather/bulk")
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 2

    def test_bulk_error_per_city_in_response(self, client, mock_api_service):
        """If a fetch fails, that item shows an error field."""
        create_bookmark(client, "London", "GB")
        mock_api_service.get_weather_for_bookmark = AsyncMock(
            side_effect=Exception("boom")
        )
        with patch("router.api_service", mock_api_service):
            resp = client.get("/v1/bookmarks/weather/bulk")
        assert resp.status_code == 200
        assert resp.json()["data"][0]["error"] == "boom"
        assert resp.json()["data"][0]["weather"] is None

    def test_bulk_pagination(self, client):
        cities = ["LondonX", "ParisX", "TokyoX", "LagosX", "BerlinX", "SydneyX"]
        for city in cities:
            create_bookmark(client, city, "GB")
        resp = client.get("/v1/bookmarks/weather/bulk?page=2&page_limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["page"] == 2
        assert len(data["data"]) == 1  # 6 total, page 2 of 5 = 1 item

    def test_bulk_page_limit_below_minimum(self, client):
        resp = client.get("/v1/bookmarks/weather/bulk?page_limit=4")  # min is 5
        assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────
# GET /v1/cache/stats  and  DELETE /v1/cache
# ─────────────────────────────────────────────────────────────

class TestCacheEndpoints:
    def test_cache_stats_returns_dict(self, client):
        resp = client.get("/v1/cache/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_entries" in data
        assert "cached_locations" in data

    def test_clear_cache_returns_204(self, client):
        resp = client.delete("/v1/cache")
        assert resp.status_code == 204


# ─────────────────────────────────────────────────────────────
# GET /v1/bookmarks/alerts/temperature  (bug fixed in router.py)
# ─────────────────────────────────────────────────────────────

class TestTemperatureAlerts:
    def test_no_bookmarks_with_threshold(self, client):
        create_bookmark(client, "London", "GB")
        resp = client.get("/v1/bookmarks/alerts/temperature")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_alert_triggered_when_threshold_exceeded(self, client, db_session, mock_api_service):
        """Bookmark with threshold=10 and current temp=20 should appear in alerts."""
        hot_weather = make_weather_response(temperature=20.0)
        mock_api_service.get_weather_for_bookmark = AsyncMock(return_value=hot_weather)

        bm = create_bookmark(client, "Lagos", "NG")
        # Set threshold directly in DB
        from models import Bookmark
        record = db_session.get(Bookmark, uuid.UUID(bm["id"]))
        record.temperature_threshold = 10.0
        db_session.add(record)
        db_session.commit()

        with patch("router.api_service", mock_api_service):
            resp = client.get("/v1/bookmarks/alerts/temperature")

        assert resp.status_code == 200
        alerts = resp.json()
        assert len(alerts) == 1
        assert alerts[0]["city"] == "Lagos"
        assert alerts[0]["currentTemperature"] == 20.0

    def test_no_alert_when_below_threshold(self, client, db_session, mock_api_service):
        """Current temp 5° < threshold 10° — no alert."""
        cold_weather = make_weather_response(temperature=5.0)
        mock_api_service.get_weather_for_bookmark = AsyncMock(return_value=cold_weather)

        bm = create_bookmark(client, "London", "GB")
        from models import Bookmark
        record = db_session.get(Bookmark, uuid.UUID(bm["id"]))
        record.temperature_threshold = 10.0
        db_session.add(record)
        db_session.commit()

        with patch("router.api_service", mock_api_service):
            resp = client.get("/v1/bookmarks/alerts/temperature")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_alert_exactly_at_threshold(self, client, db_session, mock_api_service):
        """Threshold 15, temp exactly 15 — alert fires (>=)."""
        exact_weather = make_weather_response(temperature=15.0)
        mock_api_service.get_weather_for_bookmark = AsyncMock(return_value=exact_weather)

        bm = create_bookmark(client, "Paris", "FR")
        from models import Bookmark
        record = db_session.get(Bookmark, uuid.UUID(bm["id"]))
        record.temperature_threshold = 15.0
        db_session.add(record)
        db_session.commit()

        with patch("router.api_service", mock_api_service):
            resp = client.get("/v1/bookmarks/alerts/temperature")

        assert len(resp.json()) == 1
