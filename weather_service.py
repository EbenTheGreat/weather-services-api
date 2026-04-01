from settings import settings
from models import WeatherResponse, Units, WeatherHistory
import httpx
from datetime import datetime, UTC
import fakeredis
from sqlmodel import Session, select
import uuid
from fastapi import HTTPException, status

from config import settings
import json
import time


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
      "q": f"{city}:{country_code}",
      "units": units.value,
      "appid": self.api_key
    }

    async with httpx.AsyncClient()



