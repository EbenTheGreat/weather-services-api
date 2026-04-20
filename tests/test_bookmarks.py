"""
test_bookmarks.py — Tests for all Bookmark CRUD routes.

Covers:
- Happy-path create / read / update / delete
- Duplicate detection
- Validation edge cases (country code, city length)
- Pagination, filtering, search, and sort
- ETag / 304 caching behaviour
"""
import uuid
import pytest
from tests.conftest import create_bookmark


# ─────────────────────────────────────────────────────────────
# POST /v1/bookmarks
# ─────────────────────────────────────────────────────────────

class TestCreateBookmark:
    def test_create_bookmark_success(self, client):
        resp = client.post("/v1/bookmarks", json={"city": "London", "countryCode": "GB"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["city"] == "London"
        assert data["countryCode"] == "GB"
        assert "id" in data
        assert "createdAt" in data
        assert "updatedAt" in data

    def test_create_bookmark_with_optional_fields(self, client):
        resp = client.post(
            "/v1/bookmarks",
            json={"city": "Tokyo", "countryCode": "JP", "notes": "Cherry blossom season", "units": "imperial"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["notes"] == "Cherry blossom season"
        assert data["units"] == "imperial"

    def test_create_bookmark_duplicate_raises_400(self, client):
        client.post("/v1/bookmarks", json={"city": "Lagos", "countryCode": "NG"})
        resp = client.post("/v1/bookmarks", json={"city": "Lagos", "countryCode": "NG"})
        assert resp.status_code == 400
        assert "exists" in resp.json()["detail"].lower()

    def test_create_bookmark_duplicate_case_insensitive(self, client):
        """
        Duplicate check should be case-insensitive for both city and country_code.
        """
        client.post("/v1/bookmarks", json={"city": "Paris", "countryCode": "FR"})
        resp = client.post("/v1/bookmarks", json={"city": "paris", "countryCode": "FR"})
        assert resp.status_code == 400

    def test_create_bookmark_invalid_country_code_lowercase(self, client):
        resp = client.post("/v1/bookmarks", json={"city": "Berlin", "countryCode": "de"})
        assert resp.status_code == 422

    def test_create_bookmark_invalid_country_code_numeric(self, client):
        resp = client.post("/v1/bookmarks", json={"city": "Berlin", "countryCode": "12"})
        assert resp.status_code == 422

    def test_create_bookmark_country_code_too_long(self, client):
        resp = client.post("/v1/bookmarks", json={"city": "Berlin", "countryCode": "DEU"})
        assert resp.status_code == 422

    def test_create_bookmark_city_too_short(self, client):
        resp = client.post("/v1/bookmarks", json={"city": "A", "countryCode": "US"})
        assert resp.status_code == 422

    def test_create_bookmark_city_too_long(self, client):
        resp = client.post("/v1/bookmarks", json={"city": "A" * 100, "countryCode": "US"})
        assert resp.status_code == 422

    def test_create_bookmark_missing_city(self, client):
        resp = client.post("/v1/bookmarks", json={"countryCode": "US"})
        assert resp.status_code == 422

    def test_create_bookmark_missing_country_code(self, client):
        resp = client.post("/v1/bookmarks", json={"city": "New York"})
        assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────
# GET /v1/bookmarks
# ─────────────────────────────────────────────────────────────

class TestGetAllBookmarks:
    def test_empty_list(self, client):
        resp = client.get("/v1/bookmarks")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bookmarks"] == []
        assert data["total"] == 0
        assert data["totalPages"] == 1

    def test_returns_created_bookmarks(self, client):
        create_bookmark(client, "London", "GB")
        create_bookmark(client, "Paris", "FR")
        resp = client.get("/v1/bookmarks")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_pagination_page_2(self, client):
        for city in ["LondonCity", "ParisCity", "TokyoCity"]:
            create_bookmark(client, city, "GB")
        resp = client.get("/v1/bookmarks?page=2&page_limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["bookmarks"]) == 1
        assert data["totalPages"] == 2
        assert data["page"] == 2

    def test_pagination_page_limit_capped_at_100(self, client):
        resp = client.get("/v1/bookmarks?page_limit=101")
        assert resp.status_code == 422

    def test_filter_by_country_code(self, client):
        create_bookmark(client, "London", "GB")
        create_bookmark(client, "Lagos", "NG")
        resp = client.get("/v1/bookmarks?country_code=NG")
        data = resp.json()
        assert data["total"] == 1
        assert data["bookmarks"][0]["countryCode"] == "NG"

    def test_filter_by_favourite_true(self, client, db_session):
        from sqlmodel import select
        from models import Bookmark
        create_bookmark(client, "London", "GB")
        bm = create_bookmark(client, "Lagos", "NG")
        # Set is_favorite directly in the DB (field not in BookmarkCreate)
        record = db_session.get(Bookmark, uuid.UUID(bm["id"]))
        record.is_favorite = True
        db_session.add(record)
        db_session.commit()

        resp = client.get("/v1/bookmarks?favourite=true")
        assert resp.json()["total"] == 1
        assert resp.json()["bookmarks"][0]["id"] == bm["id"]

    def test_filter_by_favourite_false(self, client, db_session):
        from sqlmodel import select
        from models import Bookmark
        create_bookmark(client, "London", "GB")
        bm = create_bookmark(client, "Lagos", "NG")
        record = db_session.get(Bookmark, uuid.UUID(bm["id"]))
        record.is_favorite = True
        db_session.add(record)
        db_session.commit()

        resp = client.get("/v1/bookmarks?favourite=false")
        assert resp.json()["total"] == 1

    def test_search_by_city(self, client):
        create_bookmark(client, "London", "GB")
        create_bookmark(client, "Lagos", "NG")
        resp = client.get("/v1/bookmarks?search=lon")
        assert resp.json()["total"] == 1
        assert resp.json()["bookmarks"][0]["city"] == "London"

    def test_search_by_notes(self, client):
        client.post("/v1/bookmarks", json={"city": "Paris", "countryCode": "FR", "notes": "vacation spot"})
        create_bookmark(client, "Berlin", "DE")
        resp = client.get("/v1/bookmarks?search=vacation")
        assert resp.json()["total"] == 1

    def test_sort_order_desc(self, client):
        create_bookmark(client, "London", "GB")
        create_bookmark(client, "Paris", "FR")
        resp = client.get("/v1/bookmarks?sort_by=city&sort_order=desc")
        cities = [b["city"] for b in resp.json()["bookmarks"]]
        assert cities == sorted(cities, reverse=True)

    def test_sort_order_asc(self, client):
        create_bookmark(client, "Tokyo", "JP")
        create_bookmark(client, "Amsterdam", "NL")
        resp = client.get("/v1/bookmarks?sort_by=city&sort_order=asc")
        cities = [b["city"] for b in resp.json()["bookmarks"]]
        assert cities == sorted(cities)


# ─────────────────────────────────────────────────────────────
# GET /v1/bookmarks/{id}
# ─────────────────────────────────────────────────────────────

class TestGetSingleBookmark:
    def test_get_by_id_success(self, client):
        bm = create_bookmark(client, "London", "GB")
        resp = client.get(f"/v1/bookmarks/{bm['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == bm["id"]

    def test_get_by_id_returns_etag_header(self, client):
        bm = create_bookmark(client, "London", "GB")
        resp = client.get(f"/v1/bookmarks/{bm['id']}")
        assert "etag" in resp.headers

    def test_get_by_id_not_found(self, client):
        resp = client.get(f"/v1/bookmarks/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_get_by_id_invalid_uuid(self, client):
        resp = client.get("/v1/bookmarks/not-a-uuid")
        assert resp.status_code == 422

    def test_etag_304_not_modified(self, client):
        """If-None-Match matching the current ETag should yield 304."""
        bm = create_bookmark(client, "London", "GB")
        first = client.get(f"/v1/bookmarks/{bm['id']}")
        etag = first.headers["etag"]
        second = client.get(f"/v1/bookmarks/{bm['id']}", headers={"If-None-Match": etag})
        assert second.status_code == 304

    def test_etag_200_after_update(self, client):
        """After editing the bookmark, the ETag should change."""
        bm = create_bookmark(client, "London", "GB")
        first_etag = client.get(f"/v1/bookmarks/{bm['id']}").headers["etag"]
        client.patch(f"/v1/bookmarks/{bm['id']}", json={"notes": "Updated notes"})
        second_etag = client.get(f"/v1/bookmarks/{bm['id']}").headers["etag"]
        assert first_etag != second_etag


# ─────────────────────────────────────────────────────────────
# PATCH /v1/bookmarks/{id}
# ─────────────────────────────────────────────────────────────

class TestUpdateBookmark:
    def test_update_notes(self, client):
        bm = create_bookmark(client, "London", "GB")
        resp = client.patch(f"/v1/bookmarks/{bm['id']}", json={"notes": "New note here"})
        assert resp.status_code == 200
        assert resp.json()["notes"] == "New note here"

    def test_update_is_favourite(self, client):
        bm = create_bookmark(client, "London", "GB")
        resp = client.patch(f"/v1/bookmarks/{bm['id']}", json={"isFavourite": True})
        assert resp.status_code == 200
        # Verify the field was persisted — subsequent list filter should return this bookmark
        fav_resp = client.get("/v1/bookmarks?favourite=true")
        assert fav_resp.json()["total"] == 1

    def test_update_not_found(self, client):
        resp = client.patch(f"/v1/bookmarks/{uuid.uuid4()}", json={"notes": "Nope"})
        assert resp.status_code == 404

    def test_update_partial_other_fields_unchanged(self, client):
        bm = create_bookmark(client, "London", "GB")
        resp = client.patch(f"/v1/bookmarks/{bm['id']}", json={"notes": "Only this changes"})
        data = resp.json()
        assert data["city"] == "London"
        assert data["countryCode"] == "GB"

    def test_update_units(self, client):
        bm = create_bookmark(client, "London", "GB")
        resp = client.patch(f"/v1/bookmarks/{bm['id']}", json={"units": "imperial"})
        assert resp.status_code == 200
        assert resp.json()["units"] == "imperial"

    def test_update_invalid_uuid(self, client):
        resp = client.patch("/v1/bookmarks/not-a-uuid", json={"notes": "X"})
        assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────
# DELETE /v1/bookmarks/{id}
# ─────────────────────────────────────────────────────────────

class TestDeleteBookmark:
    def test_delete_success(self, client):
        bm = create_bookmark(client, "London", "GB")
        resp = client.delete(f"/v1/bookmarks/{bm['id']}")
        assert resp.status_code == 204

    def test_delete_then_get_returns_404(self, client):
        bm = create_bookmark(client, "London", "GB")
        client.delete(f"/v1/bookmarks/{bm['id']}")
        resp = client.get(f"/v1/bookmarks/{bm['id']}")
        assert resp.status_code == 404

    def test_delete_not_found(self, client):
        resp = client.delete(f"/v1/bookmarks/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_delete_invalid_uuid(self, client):
        resp = client.delete("/v1/bookmarks/bad-id")
        assert resp.status_code == 422
