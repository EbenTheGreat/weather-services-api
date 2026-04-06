from sqlmodel import SQLModel, Field as SQLField
from typing import Optional
from pydantic import BaseModel, Field, field_validator, ConfigDict
from enum import Enum
from datetime import datetime, UTC
import uuid

class Units(str, Enum):
    METRIC = "metric"
    IMPERIAL = "imperial"
    

class SortOrder(str, Enum):
    ASC = "asc"
    DESC = "desc"

class SortBy(str, Enum):
    CITY = "city"
    COUNTRY_CODE = "country_code"
    CREATED_AT = "created_at"
    UNITS = "units"


class Bookmark(SQLModel, table= True):
    """
    database model
    """
    id: uuid.UUID= SQLField(default_factory=uuid.uuid4, primary_key=True)
    city: str= SQLField(index=True, min_length=1, max_length=99)
    country_code: str= SQLField(index=True, min_length=2, max_length=2)
    notes: str | None = SQLField(default=None,min_length=2, max_length=999)
    units: Units = SQLField(default=Units.METRIC)
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))

    #use alembic migrations for this data models in order to learn it
    #is_favorite = SQLField(default= False)
    #temperature_threshold: str | None = SQLField(default= None)


class WeatherHistory(SQLModel, table=True):
    """
    Stores historical weather snapshots linked to a specific bookmark.
    """
    id: uuid.UUID = SQLField(default_factory=uuid.uuid4, primary_key=True)
    bookmark_id: uuid.UUID = SQLField(foreign_key="bookmark.id", index=True)
    city: str = SQLField(index=True)
    country_code: str = SQLField(index=True)
    temperature: float
    feels_like: float
    description: str = SQLField(min_length=2, max_length=999)
    humidity: int
    wind_speed: float
    units: Units
    fetched_at: datetime = SQLField(index=True, default_factory=lambda: datetime.now(UTC))
    


# ─────────────────────────────────────────────
# API INPUT / OUTPUT MODELS (Pydantic — no table=True)
# kept separate from DB model for security and flexibility
# ─────────────────────────────────────────────
class BookmarkBase(BaseModel):
    """
    shared field for api input and output models
    """
    city: str = Field(..., min_length=2, max_length=99)
    units: Units = Field(default=Units.METRIC)
    notes: str | None = Field(default=None, min_length=2, max_length=999)
    """
    temperature_threshold: float | None = Field(
        None,
        alias="temperatureThreshold",
        description="Alert threshold for temperature in degrees"
    )
    is_favourite: bool = Field(False, alias="isFavourite", description="Mark as favourite")
    @field_validator("temperature_threshold")
    @classmethod
    def validate_temperature_threshold(cls, v: float | None) -> float | None:
        if v is not None and (v < -100 or v > 100):
            raise ValueError("Temperature threshold must be between -100 and 100")
        return v

    """

    model_config = ConfigDict(populate_by_name=True)


class BookmarkCreate(BookmarkBase):
    """
    What the client sends when creating a bookmark.
    """
    country_code: str = Field(..., max_length=2, min_length=2, alias="countryCode")

    @field_validator("country_code")
    @classmethod
    def validate_country_code(cls, v: str) -> str:
        if not v.isalpha() or not v.isupper():
            raise ValueError("Country code must be 2 uppercase letters (e.g. GB, NG)")

        return v

    model_config=  ConfigDict(populate_by_name=True)


class BookmarkUpdate(BaseModel):
    """
    All fields optional — used for PATCH (partial updates).
    """
    city: str | None = Field(default=None, min_length=2, max_length=99)
    country_code: str | None = Field(default=None, min_length=2, max_length=2, alias="countryCode")
    notes: str | None = Field(default=None, min_length=2, max_length=999)
    units: Units | None = None
    #temperature_threshold: float | None = Field(None, alias="temperatureThreshold", description="Alert threshold for temperature in degrees")
    #is_favourite: bool | None = Field(None, alias="isFavourite", description="Mark as favourite")

    model_config= ConfigDict(populate_by_name=True)


# ─────────────────────────────────────────────
# API RESPONSE MODELS
# ─────────────────────────────────────────────
class BookmarkResponse(BookmarkBase):
    """
    What the API returns — includes id and timestamps. Never exposes DB internals.
    """
    id: uuid.UUID
    country_code: str = Field(..., max_length=2, min_length=2, alias="countryCode")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")

    # from_attributes=True lets FastAPI construct this from a SQLModel ORM object (Bookmark)
    model_config = ConfigDict(populate_by_name=True, from_attributes=True)


class BookmarkListResponse(BaseModel):
    """
    Response for listing multiple bookmarks.
    """
    bookmarks: list[BookmarkResponse]
    page: int
    total: int
    total_pages: int = Field(alias="totalPages")

    model_config = ConfigDict(populate_by_name=True)


class BookmarkAlertResponse(BaseModel):
    """
    Response for bookmark alerts.
    """
    bookmark_id: uuid.UUID = Field(alias="bookmarkId")
    city: str
    threshold: float
    current_temperature: float = Field(alias="currentTemperature")
    units: Units
    alert_triggered: bool = Field(alias="alertTriggered")
    triggered_at: datetime | None = Field(alias="triggeredAt")

    model_config = ConfigDict(populate_by_name=True)


class WeatherResponse(BaseModel):
    """
    Response for weather.
    """
    city: str
    country_code: str = Field(alias="countryCode")
    temperature: float
    feels_like: float = Field(alias="feelsLike")
    description: str
    humidity: int
    wind_speed: float = Field(alias="windSpeed")
    units: Units
    fetched_at: datetime = Field(alias="fetchedAt")
    cached: bool
    alert: str | None = Field(default=None)

    model_config = ConfigDict(populate_by_name=True)


class WeatherCompareItem(BaseModel):
    """
    Response for weather comparison.
    """
    bookmark_id: uuid.UUID = Field(alias="bookmarkId")
    city: str = Field(..., min_length=2, max_length=99)
    country_code: str = Field(..., max_length=2, min_length=2, alias="countryCode")
    weather: WeatherResponse | None = None
    error: str | None = None


    model_config = ConfigDict(populate_by_name=True)


class WeatherHistoryListResponse(BaseModel):
    """
    Response model for paginated weather history
    Includes the actual data and a cursor for the next page.
    """
    data: list[WeatherHistory]
    next_cursor: datetime | None = Field(alias="nextCursor")

    model_config = ConfigDict(populate_by_name=True)


