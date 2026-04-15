from collections import defaultdict
from dataclasses import dataclass
from pydantic_ai import Agent, RunContext
from pydantic_ai.tools import Tool
from pydantic_ai._function_schema import FunctionSchema
from pydantic_ai._utils import is_async_callable as _is_async_callable
from pydantic_core import SchemaValidator, core_schema
from config import settings
from models import Bookmark, Units, WeatherHistory
from weather_service import WeatherApiService
from sqlmodel import Session, select
import os
import asyncio
import logging


logger = logging.getLogger(__name__)


if settings.GROQ_API_KEY:
    os.environ.setdefault("GROQ_API_KEY", settings.GROQ_API_KEY)


@dataclass
class WeatherApiDeps:
    """
    Dependency container injected into every agent tool call.
    - session: SQLModel DB session for bookmark queries
    - api_service: WeatherAPIService for live + cached weather
    """
    session: Session
    api_service: WeatherApiService


# ─────────────────────────────────────────────
# AGENT INSTRUCTIONS
# ─────────────────────────────────────────────
_INSTRUCTIONS = """
You are an intelligent, proactive weather assistant and planner.
The user has saved cities they care about (called "bookmarks").

You have access to tools that let you:
1. get_my_bookmarks    — list their saved bookmarks from the database
2. get_weather_for_city — fetch live (or cached) weather for any city
3. check_temperature_alerts — see which bookmarks exceed their temperature threshold
4. get_weather_trends — analyze historical data to provide predictions and trends

Guidelines:
- Act as a clever planner: combine multiple tools to answer complex questions (e.g. which city is hottest, what is the temperature trend).
- ALWAYS call a tool to get real data — never hallucinate data.
- Tell the user if data came from cache vs live API.
- Provide insights beyond raw data where applicable. Use get_weather_trends to proactively offer insights.
- Keep responses concise unless requested otherwise.
"""

# ─────────────────────────────────────────────
# NO-ARG TOOL HELPER
#
# Tools that only take `ctx` with no LLM-facing args need special handling.
# pydantic-ai builds a typed_dict validator with zero fields, which rejects
# `null` args sent by Groq. Tool.from_schema uses any_schema() which passes
# None through — but the tool executor asserts validated_args is not None.
#
# Solution: build a FunctionSchema with a null-tolerant validator that maps
# None → {} so the assertion passes and _call_args gets a valid empty dict.
# ─────────────────────────────────────────────
_EMPTY_SCHEMA: dict = {"type": "object", "properties": {}}

_null_tolerant_validator = SchemaValidator(
    schema=core_schema.no_info_plain_validator_function(
        lambda v: {} if v is None else (v if isinstance(v, dict) else {})
    )
)


def _no_args_tool(func, name: str, description: str) -> Tool:
    """Register a context-only tool (no LLM args) with a null-tolerant validator."""
    fs = FunctionSchema(
        function=func,
        description=description,
        validator=_null_tolerant_validator,
        json_schema=_EMPTY_SCHEMA,
        takes_ctx=True,
        is_async=_is_async_callable(func),
    )
    return Tool(
        func,
        takes_ctx=True,
        name=name,
        description=description,
        function_schema=fs,
    )


def get_my_bookmarks(ctx: RunContext[WeatherApiDeps]) -> list[dict]:
    """
    Retrieve all bookmarks the user has saved in their account.

    Returns a list of saved locations including:
    - id, city, country_code, notes, units (metric/imperial),
      is_favorite, and temperature_threshold (if set).
    """
    logger.info("Tool called: get_my_bookmarks")
    try:
        bookmarks = ctx.deps.session.exec(select(Bookmark)).all()
        if not bookmarks:
            return [{"message": "You have no saved bookmarks yet."}]

        return [
            {
                "id": str(b.id),
                "city": b.city,
                "country_code": b.country_code,
                "notes": b.notes,
                "units": b.units.value,
                "is_favorite": b.is_favorite,
                "temperature_threshold": b.temperature_threshold,
            }
            for b in bookmarks
        ]
    except Exception as e:
        logger.error(f"Error in get_my_bookmarks: {str(e)}")
        return [{"error": f"Could not retrieve bookmarks from database. {str(e)}"}]


async def check_temperature_alerts(ctx: RunContext[WeatherApiDeps]) -> list[dict]:
    """
    Check all bookmarks that have a temperature alert threshold configured.

    Returns only the bookmarks where the current live temperature is at or
    above their threshold (i.e. the alert is actively triggered right now).
    If no bookmarks have thresholds set, or none are currently exceeded,
    returns a friendly message.
    """
    logger.info("Tool called: check_temperature_alerts")
    statement = select(Bookmark).where(Bookmark.temperature_threshold.is_not(None))
    bookmarks = ctx.deps.session.exec(statement).all()

    if not bookmarks:
        return [{"message": "No bookmarks have a temperature threshold configured."}]

    # Create concurrent fetch tasks
    tasks = [
        ctx.deps.api_service.get_weather_for_bookmark(
            city=b.city,
            country_code=b.country_code,
            units=b.units
        )
        for b in bookmarks
    ]

    # Execute all tasks concurrently, allowing individual failures via return_exceptions
    results = await asyncio.gather(*tasks, return_exceptions=True)

    alerts = []
    for b, res in zip(bookmarks, results):
        if isinstance(res, Exception):
            logger.warning(f"Failed to fetch weather for {b.city}, {b.country_code}: {res}")
            alerts.append({
                "city": b.city,
                "country_code": b.country_code,
                "error": "Could not fetch weather for this location.",
                "alert_triggered": False,
            })
            continue

        alert_triggered = res.temperature >= b.temperature_threshold
        alerts.append({
            "city": b.city,
            "country_code": b.country_code,
            "current_temperature": res.temperature,
            "threshold": b.temperature_threshold,
            "units": b.units.value,
            "alert_triggered": alert_triggered,
        })

    triggered = [a for a in alerts if a.get("alert_triggered")]
    errors = [a for a in alerts if "error" in a]

    if not triggered and not errors:
        return [{"message": "No temperature alerts are currently triggered."}]

    response = triggered if triggered else [{"message": "No temperature alerts are currently triggered."}]

    # Surface any fetch failures so the LLM can inform the user accurately
    if errors:
        response.append({
            "warning": f"{len(errors)} location(s) could not be checked.",
            "failed_cities": [e["city"] for e in errors],
        })

    return response


def get_weather_trends(ctx: RunContext[WeatherApiDeps]) -> list[dict]:
    """
    Analyze historical weather data for all saved bookmarks to identify recent trends or anomalies.
    Returns the recent temperatures and averages to predict future weather trends.
    """
    logger.info("Tool called: get_weather_trends")
    statement = select(WeatherHistory).order_by(WeatherHistory.fetched_at.desc()).limit(100)
    history_record = ctx.deps.session.exec(statement).all()

    if not history_record:
        return [{"message": "No historical weather data available for trend analysis"}]

    # Group by location
    trends: defaultdict[tuple, list[WeatherHistory]] = defaultdict(list)
    for record in history_record:
        key = (record.city, record.country_code, record.units)
        trends[key].append(record)

    results = []

    for (city, country_code, units), records in trends.items():
        temperatures = [r.temperature for r in records]
        # Include timestamps so the LLM can reason about the direction of the trend over time
        recent = [
            {"temperature": r.temperature, "fetched_at": r.fetched_at.isoformat()}
            for r in records[:5]
        ]
        results.append({
            "city": city,
            "country_code": country_code,
            "units": units.value,
            "recent_temperatures": recent,
            "average_temperature": round(sum(temperatures) / len(temperatures), 2),
            "data_points": len(temperatures)
        })

    return results


# ─────────────────────────────────────────────
# AGENT DEFINITION
# No-arg tools passed via tools=[Tool.from_schema(...)] to bypass
# the typed_dict None-rejection bug. Arg-bearing tools use @decorator.
# ─────────────────────────────────────────────
weather_agent = Agent(
    settings.LLM_MODEL,
    deps_type=WeatherApiDeps,
    instructions=_INSTRUCTIONS,
    tools=[
        _no_args_tool(
            get_my_bookmarks,
            name="get_my_bookmarks",
            description=(
                "Retrieve all bookmarks the user has saved in their account. "
                "Returns id, city, country_code, notes, units, is_favorite, temperature_threshold."
            ),
        ),
        _no_args_tool(
            check_temperature_alerts,
            name="check_temperature_alerts",
            description=(
                "Check all bookmarks that have a temperature alert threshold configured. "
                "Returns bookmarks where current temperature meets or exceeds the threshold."
            ),
        ),
        _no_args_tool(
            get_weather_trends,
            name="get_weather_trends",
            description=(
                "Analyze historical weather data for all saved bookmarks to identify "
                "recent trends or anomalies. Returns recent temperatures and averages."
            ),
        ),
    ]
)


# ─────────────────────────────────────────────
# TOOLS WITH LLM-FACING ARGUMENTS (use decorator)
# ─────────────────────────────────────────────
@weather_agent.tool
async def get_weather_for_city(
    ctx: RunContext[WeatherApiDeps],
    city: str,
    country_code: str,
    units: Units = Units.METRIC
    ) -> dict:
    """
    Fetch the current weather for a given city and country code.

    Args:
        city:         Name of the city (e.g. 'London', 'Lagos', 'Tokyo').
        country_code: 2-letter ISO country code (e.g. 'GB', 'NG', 'JP').
        units:        'metric' for Celsius (default) or 'imperial' for Fahrenheit.

    Returns temperature, feels_like, description, humidity, wind_speed,
    and whether the data was served from cache or live API.
    """
    logger.info(f"Tool called: get_weather_for_city(city='{city}', country_code='{country_code}', units='{units}')")

    try:
        weather = await ctx.deps.api_service.get_weather_for_bookmark(
            city=city,
            country_code=country_code.upper(),
            units=units
        )

        return {
            "city": weather.city,
            "country_code": weather.country_code,
            "temperature": weather.temperature,
            "feels_like": weather.feels_like,
            "description": weather.description,
            "humidity": weather.humidity,
            "wind_speed": weather.wind_speed,
            "units": weather.units.value,
            "cached": weather.cached,
            "fetched_at": weather.fetched_at.isoformat(),
        }
    except Exception as e:
        logger.error(f"Error in get_weather_for_city: {str(e)}")
        return {
            "error": f"Could not fetch weather for {city}, {country_code}. Error: {str(e)}"
        }
