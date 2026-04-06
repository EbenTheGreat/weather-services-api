from config import settings
from models import WeatherResponse, Units, WeatherHistory, Bookmark
import httpx
from datetime import datetime, UTC
import fakeredis
from sqlmodel import Session, select
import uuid
from fastapi import HTTPException, status
from db import SessionDep
from config import settings
import json
import time
from typing import Any


#________________________________#
#CACHE-SERVICE
#________________________________#
class WeatherCacheService:
    """
    Handles all fakeredis interactions:
      - weather result caching (Cache-Aside pattern)
      - per-IP rate limiting (atomic INCR pattern)
      - cache introspection and flushing
    """
    CACHE_TTL= settings.CACHE_TTL_SECONDS
    RATE_LIMIT_WINDOW_SECONDS = settings.RATE_LIMIT_WINDOW_SECONDS
    RATE_LIMIT_MAX_REQUESTS = settings.RATE_LIMIT_MAX_REQUESTS

    _redis_client = fakeredis.FakeRedis()
    
    def __init__(self):
        self.cache = self._redis_client

    
    def _cache_key(self, city: str, country_code: str, units: Units) -> str:
      return f"weather:{city.lower()}:{country_code.lower()}:{units.value}"
        

    def save_to_cache(self, city: str, country_code: str, units: Units, data: WeatherResponse) -> None:
      """
      Store a WeatherResponse in fakeredis with a TTL
      """
      key = self._cache_key(city, country_code, units)
      self.cache.setex(key, self.CACHE_TTL, data.model_dump_json())

    def get_from_cache(self, city: str, country_code: str, units: Units) -> WeatherResponse | None:
        """
        Check fakeredis for a cached weather result
        """
        key = self._cache_key(city, country_code, units)
        cached_data = self.cache.get(key)
        
        if cached_data:
          data = json.loads(cached_data)
          data["cached"] = True
          return WeatherResponse(**data)
        return None


    def get_cache_stats(self) -> dict:
        """
        Return statistics about what's currently in the weather cache
        """
        keys = self.cache.scan_iter("weather:*")
        weather_keys = [k.decode("utf-8") for k in keys]
        return {
          "total_entries": len(weather_keys),
          "cached_locations": weather_keys
        }

    
    def flush_cache(self) -> None:
      """
      Clear all cache data
      """
      self.cache.flushdb()

    
    def check_rate_limit(self, client_ip: str) -> None:
      """
      Enforce a per-IP rate limit using fakeredis atomic INCR
      """
      current_minute = int(time.time() / self.RATE_LIMIT_WINDOW_SECONDS)
      rate_key = f"rate_limit:{client_ip}:{current_minute}"

      count = self.cache.incr(rate_key)

      if count == 1:
        self.cache.expire(rate_key, self.RATE_LIMIT_WINDOW_SECONDS)

      if count > self.RATE_LIMIT_MAX_REQUESTS:
        ttl = self.cache.ttl(rate_key)
        ttl = ttl if ttl > 0 else self.RATE_LIMIT_WINDOW_SECONDS
        raise HTTPException(
          status_code=status.HTTP_429_TOO_MANY_REQUESTS,
          detail=f"Too many requests. Retry in {ttl}s",
          headers={"Retry-After": str(ttl)}
        )


#______________________________#
#API-SERVICE
#______________________________#
class WeatherApiService:
  """
  Handles all communication with the OpenWeather HTTP API and
  orchestrates the Cache-Aside flow via WeatherCacheService.
  """
  Base_Url= "https://api.openweathermap.org/data/2.5/weather"

  def __init__(self, cache_service: WeatherCacheService):
    self.api_key = settings.OPENWEATHER_API_KEY
    self.cache = cache_service

  
  async def get_weather(self, city: str, country_code: str, units: Units) -> WeatherResponse:
    """
    Fetch live weather from OpenWeather API
    """
    params = {
      "q": f"{city},{country_code}",
      "units": units.value,
      "appid": self.api_key
    }

    async with httpx.AsyncClient() as client:
      try:
        response = await client.get(self.Base_Url, params=params, timeout=settings.TIMEOUT)
        response.raise_for_status()
        data = response.json()

      except httpx.TimeoutException:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="weather Api timeout")
      
      except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"Weather API error: {e.response.status_code} {e.response.reason_phrase}")

      except httpx.RequestError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Weather API unavailable")

      return WeatherResponse(
        city=data["name"],
        country_code=data["sys"]["country"],
        temperature=data["main"]["temp"],
        feels_like=data["main"]["feels_like"],
        description=data["weather"][0]["description"],
        humidity=data["main"]["humidity"],
        wind_speed=data["wind"]["speed"],
        units=units,
        fetched_at=datetime.now(UTC),
        cached=False
      )

  
  async def get_weather_for_bookmark(
    self, city: str, country_code: str,
    units: Units, force_refresh: bool= False) -> WeatherResponse:

    if not force_refresh:
      cached = self.cache.get_from_cache(city, country_code, units)
      if cached:
        return cached

    weather = await self.get_weather(city, country_code, units)
    self.cache.save_to_cache(city, country_code, units, weather)
    return weather


#______________________________#
#HISTORY-SERVICE
#______________________________#
class WeatherHistoryService:
  """
  Handles all database interactions for weather history
  and bookmark temperature thresholds.
  """
  def save_history(
    self, session: Session, bookmark_id: uuid.UUID,
    weather: WeatherResponse) -> WeatherHistory:
    """Append a WeatherResponse to the history list for this bookmark."""
    history_record= WeatherHistory(
      bookmark_id=bookmark_id,
      city=weather.city,
      country_code=weather.country_code,
      temperature=weather.temperature,
      feels_like=weather.feels_like,
      description=weather.description,
      humidity=weather.humidity,
      wind_speed=weather.wind_speed,
      units=weather.units,
      fetched_at=weather.fetched_at
    )
    session.add(history_record)
    session.commit()
    session.refresh(history_record)
    return history_record

  
  def get_history(self, session: Session, bookmark_id: uuid.UUID, cursor: datetime | None= None, limit: int = 100) -> list[WeatherHistory]:
    """
    Return a slice of weather history for this bookmark using cursor pagination.
    Fetches 'limit + 1' items to easily determine if there is a next page.
    """
    statement = (
      select(WeatherHistory)
      .where(WeatherHistory.bookmark_id == bookmark_id)
    )

    if cursor:
      # Only return records fetched strictly AFTER our marker
      statement = statement.where(WeatherHistory.fetched_at > cursor)

    # Order by time (oldest first) and limit results
    statement = statement.order_by(WeatherHistory.fetched_at.asc()).limit(limit + 1)
    result = session.exec(statement)
    return result.all()


  def set_threshold(self, session: Session, bookmark_id: uuid.UUID, temperature_threshold: float) -> Bookmark:
    """
    Set the temperature alert threshold for a bookmark.
    """
    bookmark = session.get(Bookmark, bookmark_id)
    if not bookmark:
      raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bookmark not found")

    bookmark.temperature_threshold = temperature_threshold
    session.add(bookmark)
    session.commit()
    session.refresh(bookmark)
    return bookmark
    
    

