from  pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    OPENWEATHER_API_KEY: str
    CACHE_TTL_SECONDS: int
    RATE_LIMIT_MAX_REQUESTS: int
    RATE_LIMIT_WINDOW_SECONDS: int 

    class Config:
        env_file_encoding="utf-8"


settings = Settings()
