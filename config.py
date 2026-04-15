from  pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path



class Settings(BaseSettings):
    OPENWEATHER_API_KEY: str
    CACHE_TTL_SECONDS: int
    RATE_LIMIT_MAX_REQUESTS: int
    RATE_LIMIT_WINDOW_SECONDS: int 
    TIMEOUT: float
    DATABASE_URL: str
    GROQ_API_KEY: str
    LLM_MODEL: str

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8'
    )


settings = Settings()
