from fastapi import APIRouter, Query, Request, Response, HTTPException, status
from typing import Any
from db import SessionDep
from models import (
    Bookmark, WeatherHistory, BookmarkCreate,
    BookmarkUpdate, BookmarkResponse, BookmarkAlertResponse,
    BookmarkListResponse, WeatherCompareItem, WeatherResponse,
    SortBy, SortOrder, Units, WeatherHistoryListResponse
)
from weather_service import (
    WeatherApiService, WeatherCacheService, WeatherHistoryService
)

from sqlmodel import select, func, or_
from datetime import datetime, UTC
import math
import hashlib
import asyncio
import uuid
import json


cache_service = WeatherCacheService()
api_service = WeatherApiService(cache_service=cache_service)
weather_history_service = WeatherHistoryService()

v1 = APIRouter(prefix="/v1", tags=["bookmarks"])

@v1.post("/bookmarks", response_model= BookmarkResponse, status_code=status.HTTP_201_CREATED)
async def create_new_bookmark(bookmark: BookmarkCreate, session: SessionDep):
    """
    creates a new bookmark entry
    """
    # Pre-verify that the city actually exists in OpenWeatherMap
    await api_service.get_weather_for_bookmark(
        city=bookmark.city,
        country_code=bookmark.country_code,
        units=bookmark.units
    )

    # Check if a bookmark for the same city and country already exists
    existing = session.exec(
        select(Bookmark).where(
            func.lower(Bookmark.city )== bookmark.city.lower(),
            func.lower(Bookmark.country_code) == bookmark.country_code.lower()
        )
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Bookmark for {bookmark.city} {bookmark.country_code} exists" 
        )

    
    # Convert BookMarkCreate → Bookmark DB model
    db_bookmark = Bookmark.model_validate(bookmark)
    session.add(db_bookmark)
    session.commit()
    session.refresh(db_bookmark)
    return db_bookmark


@v1.get("/bookmarks", response_model=BookmarkListResponse, status_code=status.HTTP_200_OK)
async def get_all_bookmarks(
    session: SessionDep,
    page: int= Query(1, ge=1, description="page number"),
    page_limit: int = Query(10, ge=1, le=100, description="items per page. Max items is 100"),
    sort_by: SortBy= Query(SortBy.CREATED_AT, description="field to use in sorting"),
    sort_order: SortOrder= Query(SortOrder.ASC, description="sort by asc or desc"),
    country_code: str | None = Query(None, description="filter by country code"),
    favourite: bool | None = Query(None, description="filter by favorite"),
    search: str | None= Query(None, description="search in city or notes")
) -> BookmarkListResponse:
    """
    Get all bookmarks with filtering, sorting, pagination
    """
    statement = select(Bookmark)
    if country_code:
        statement = statement.where(Bookmark.country_code == country_code)
    
    if favourite is not None:
        statement = statement.where(Bookmark.is_favorite == favourite)

    if search:
        search_lower = search.strip().lower()
        statement = statement.where(
            or_(
                Bookmark.city.icontains(search_lower),
                Bookmark.notes.icontains(search_lower)
            )
        )

    count_statement = select(func.count()).select_from(statement.subquery())
    total = session.exec(count_statement).one()

    sort_column = getattr(Bookmark, sort_by.value)
    if sort_order == SortOrder.DESC:
        statement = statement.order_by(sort_column.desc())
    else:
        statement = statement.order_by(sort_column.asc())

    start = (page - 1) * page_limit
    statement = statement.offset(start).limit(limit=page_limit)
    paginated = session.exec(statement).all()

    total_pages = math.ceil(total / page_limit) if total > 0 else 1

    return BookmarkListResponse(
        bookmarks=paginated,
        page=page,
        total=total,
        totalPages=total_pages
    )


@v1.get("/bookmarks/{bookmark_id}", response_model=BookmarkResponse, status_code=status.HTTP_200_OK)
async def get_bookmark(session: SessionDep, bookmark_id: uuid.UUID, request: Request) -> BookmarkResponse:
    """
    Get single bookmark from database
    """
    bookmark = session.get(Bookmark, bookmark_id)
    if not bookmark:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bookmark not found"
        )

    data = BookmarkResponse.model_validate(bookmark, from_attributes=True).model_dump(mode="json")
    content_str = json.dumps(data, sort_keys=True)
    etag = hashlib.sha256(content_str.encode("utf-8")).hexdigest()

    if_none_match = request.headers.get("If-None-Match")
    if if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})

    response = Response(content=content_str, media_type="application/json")
    response.headers["ETag"] = etag
    response.headers["Cache-control"] = "public, max-age=3600"
    
    return response


@v1.patch("/bookmarks/{bookmark_id}", response_model=BookmarkResponse, status_code=status.HTTP_200_OK)
async def update_bookmark(session: SessionDep, bookmark_id: uuid.UUID, bookmark_update: BookmarkUpdate):
    """
    Update bookmark fields
    """
    bookmark = session.get(Bookmark, bookmark_id)
    if not bookmark:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Bookmark not found"
        )

    update_data = bookmark_update.model_dump(exclude_unset=True, by_alias=False)
    bookmark.sqlmodel_update(update_data)
    bookmark.updated_at = datetime.now(UTC)
    session.add(bookmark)
    session.commit()
    session.refresh(bookmark)
    return bookmark



@v1.delete("/bookmarks/{bookmark_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bookmark(session: SessionDep, bookmark_id: uuid.UUID):
    """ 
    Delete bookmark 
    """
    bookmark = session.get(Bookmark, bookmark_id)
    if not bookmark:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bookmark not found"
        )

    session.delete(bookmark)
    session.commit()
    return


@v1.get("/bookmark/{bookmark_id}/weather", response_model=WeatherResponse, status_code=status.HTTP_200_OK)
async def get_bookmark_weather(
    bookmark_id: uuid.UUID, session: SessionDep, request: Request,
    force_refresh: bool = Query(False)):
    """
    Get weather for a saved bookmark. Returns 200 OK, 404, 429, 502/503/504.
    """
    #Check rate limit
    cache_service.check_rate_limit(request.client.host)

    bookmark = session.get(Bookmark, bookmark_id)
    if not bookmark:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bookmark not found"
        )
    
    #Get weather for bookmarked city
    weather = await api_service.get_weather_for_bookmark(
        city=bookmark.city,
        country_code=bookmark.country_code,
        units=bookmark.units,
        force_refresh=force_refresh
    )

    #Save weather history
    weather_history_service.save_history(session, bookmark_id, weather)
    return weather



@v1.get("/weather", status_code=status.HTTP_200_OK, response_model=WeatherResponse)
async def quick_weather_lookup(
    request: Request,
    city: str= Query(..., min_length=1, max_length=99),
    country_code: str=Query(..., min_length=2, max_length=2),
    units: Units= Query(Units.METRIC),
    force_refresh: bool = Query(False)
    ):
    """
    Quick weather lookup without needing a saved bookmark
    """
    #check rate limit
    cache_service.check_rate_limit(request.client.host)

    weather = await api_service.get_weather_for_bookmark(
        city=city,
        country_code=country_code,
        units=units,
        force_refresh=force_refresh
    )

    return weather


@v1.get("/bookmarks/{bookmark_id}/weather/history", response_model=WeatherHistoryListResponse, status_code=status.HTTP_200_OK)
async def get_weather_history(
    bookmark_id: uuid.UUID,
    session: SessionDep,
    cursor: datetime | None=Query(None, description="Starting point for pagination"),
    limit: int= Query(1, ge=1, le=100)
    ):
    """
    Return weather history for a bookmark using Cursor pagination.
    Returns the nextCursor to use for subsequent requests
    """
    bookmark = session.get(Bookmark, bookmark_id)
    if not bookmark:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bookmark not found"
        )

    # Fetch 1 more than limit to see if there's another page
    results = weather_history_service.get_history(
        session=session,
        bookmark_id=bookmark_id,
        cursor=cursor,
        limit=limit
    )

    next_cursor = None
    if len(results) > limit:
        data = results[:limit]
        next_cursor = data[-1].fetched_at
    else: 
        data= results
    
    return WeatherHistoryListResponse(data=data, next_cursor=next_cursor)


@v1.get("/bookmarks/alerts/temperature", response_model=list[BookmarkAlertResponse], status_code=status.HTTP_200_OK)
async def get_temperature_alerts(session: SessionDep):
    """
    Checks all bookmarks with a set threshold
    Returns those whose temperatures exceed it.
    """
    statement = session.get(Bookmark).where(Bookmark.temperature_threshold.is_not(None))
    bookmarks = session.exec(statement).all()

    fetch_tasks = [asyncio.create_task(
        api_service.get_weather_for_bookmark(
            city= b.city,
            country_code=b.country_code,
            units=b.units
        )
    )
        for b in bookmarks
    ]

    weather_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    alerts = []

    for bookmark, result in zip(bookmarks, weather_results):
        if isinstance(result, Exception):
            from ai_layer.ai_service import logger
            logger.warning(f"Alert check failed for {bookmark.city}: {result}")
            continue

        if result.temperature >= bookmark.temperature_threshold:
            alerts.append(
                BookmarkAlertResponse(
                    bookmark_id=str(bookmark.id),
                    city=bookmark.city,
                    threshold=bookmark.temperature_threshold,
                    current_temperature=result.temperature,
                    message=f"Alert! Current temperature ({result.temperature}°) is at or above your threshold ({bookmark.temperature_threshold}°)."
                )
            )

    return alerts


@v1.get("/bookmarks/weather/bulk", status_code=status.HTTP_200_OK)
async def fetch_weather_for_all_bookmarks(
    session: SessionDep,
    page: int = Query(1, ge=1),
    page_limit: int = Query(5, ge=5, le=100)
    ) -> dict[str, Any]:
    """
    Fetch weather for multiple bookmarks concurrently using asyncio.gather
    """
    total = session.exec(select(func.count()).select_from(Bookmark)).one()
    start = (page - 1) * page_limit
    paginated = session.exec(select(Bookmark).offset(start).limit(limit=page_limit)).all()

    fetch_tasks = [
        asyncio.create_task(
            api_service.get_weather_for_bookmark(
                city=b.city,
                country_code=b.country_code,
                units=b.units,
                force_refresh=True
            )
        )

        for b in paginated
    ]

    weather_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    results_list = []
    for b, w in zip(paginated, weather_results):
        if isinstance(w, Exception):
            results_list.append({
                "bookmark_id": str(b.id),
                "city": b.city,
                "weather": None,
                "error": str(w)
            })
        else:
            results_list.append({
                "bookmark_id": str(b.id),
                "city": b.city,
                "weather": w
            })

    total_pages = math.ceil(total / page_limit) if total > 0 else 1
    return {
        "data": results_list,
        "total": total,
        "page": page,
        "totalPages": total_pages
    }


@v1.get("/weather/compare", response_model=list[WeatherCompareItem])
async def compare_weather(
    session: SessionDep,
    ids: list[uuid.UUID] = Query(..., description="bookmark UUIDs to compare")
    ):
    """
    Fetch and compare multiple bookmarks side by side
    """
    if not ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No IDs provided"
        )

    bookmarks = session.exec(select(Bookmark).where(Bookmark.id.in_(ids))).all()
    found = {b.id: b for b in bookmarks}

    tasks = [
        api_service.get_weather_for_bookmark(
            city=b.city,
            country_code=b.country_code,
            units=b.units,
            force_refresh=True
        )
        for b in bookmarks
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    comparison = []

    for b, result in zip(bookmarks, results):
        if isinstance(result, Exception):
            comparison.append(
                WeatherCompareItem(
                    bookmark_id= str(b.id),
                    city= b.city,
                    country_code= b.country_code,
                    weather= None,
                    error= str(result)
            )
        )
        else:
            comparison.append(
                WeatherCompareItem(
                    bookmark_id=str(b.id),
                    city=b.city,
                    country_code= b.country_code,
                    weather= result
                )
            )

    for pid in ids:
        if pid not in found:
            comparison.append(
                WeatherCompareItem(
                    bookmark_id=str(pid),
                    city="unknown",
                    country_code="??",
                    weather=None,
                    error="Bookmark not found"
                )
            )

    return comparison


@v1.get("/cache/stats", status_code=status.HTTP_200_OK)
async def cache_stats():
    """
    Return how many items are in the weather cache and which locations are cached.
    """
    return cache_service.get_cache_stats()


@v1.delete("/cache", status_code=status.HTTP_204_NO_CONTENT)
async def clear_cache():
    """
    Clear all weather cache data.
    """
    cache_service.flush_cache()
    return
    
    


    


