# 📓 Project Revelations & Lessons Learned

This file documents key technical insights and "aha!" moments discovered during the development and debugging of the Weather Bookmark API.

## 🛠️ Pydantic v2 Configuration
**Insight:** In Pydantic v2, `model_config` MUST be assigned with an equals sign (`=`), not annotated with a colon (`:`).
- **The Issue:** `model_config: ConfigDict(...)` tells Pydantic that there is a field named `model_config`. Since `model_config` is a reserved name for configuration, Pydantic throws a `PydanticUserError`.
- **The Fix:** Always use `model_config = ConfigDict(populate_by_name=True)`.

## 📦 Python Module Isolation
**Insight:** Each Python file (module) is isolated. Imports must be explicit in every file where a name is used.
- **The Issue:** Common types like `Any` or decorators like `asynccontextmanager` will throw a `NameError` even if they are imported in a neighboring file like `main.py`.
- **The Lesson:** Always check that `typing` and `contextlib` imports are present in every file that uses them.

## 🔌 Supabase & Network Compatibility (IPv4 vs IPv6)
**Insight:** Direct database connections to Supabase (`db.[PROJECT_ID].supabase.co`) often resolve to IPv6 addresses.
- **The Issue:** Many local development environments (ISPs, VPNs, or local router settings) do not support IPv6 or have DNS resolution issues for AAAA records, leading to `OperationalError: could not translate host name`.
- **The Fix:** Use the **Supabase Connection Pooler** hostname (typically `aws-0-[REGION].pooler.supabase.com` on port `6543`). The pooler provides an IPv4 address and is more robust for local development and serverless environments.

## 📁 Consistent Project Imports
**Insight:** When refactoring file names (e.g., from `settings.py` to `config.py`), all import statements must be updated immediately.
- **The Lesson:** Use global search (`grep` or IDE search) to ensure no "ghost" imports remain (like `from settings import...`) that could cause a `ModuleNotFoundError`.

## 📏 FastAPI Type Hinting
**Insight:** Always provide explicit type hints for query parameters in FastAPI, even if a default value is used.
- **The Issue:** `page_limit = Query(10, ge=1)` defaults to a **string** if no type hint is provided. This causes a `TypeError` when numeric constraints (`ge=1`) are applied during Pydantic validation.
- **The Fix:** Explicitly hint the type: `page_limit: int = Query(...)`.

## 🔄 SQLModel Parameter Unpacking
**Insight:** Be careful when converting between Pydantic and SQLModel objects using `model_validate`.
- **The Issue:** `Bookmark.model_validate(**bookmark)` fails with a `TypeError` if `bookmark` is a Pydantic model. The `**` operator only works with real dictionaries.
- **The Fix:** Use `Bookmark.model_validate(bookmark)` (SQLModel can handle Pydantic objects) or `Bookmark.model_validate(bookmark.model_dump())`.

## 🆔 UUID Initialization in Models
**Insight:** When setting a default factory for UUID fields, always pass the function name, not the class name.
- **The Issue:** `default_factory=uuid.UUID` calls the class without arguments, which is an invalid operation. 
- **The Fix:** Use `default_factory=uuid.uuid4`.

## 🔢 SQLModel/SQLAlchemy Dynamic Sorting
**Insight:** When using `order_by` with dynamic columns, always call the `.asc()` or `.desc()` methods.
- **The Issue:** `statement.order_by(column.asc)` passes the unbound method itself to SQLAlchemy, which results in an `ArgumentError`.
- **The Fix:** Always call the method: `statement.order_by(column.asc())`.

## 🏗️ Service Layer Method Consistency
**Insight:** Be precise when calling service methods, as similar-sounding methods (like `get_weather` vs `get_weather_for_bookmark`) may have different signatures.
- **The Issue:** `get_weather` is a "raw" method for direct API calls and does not accept `force_refresh`. The cached version is `get_weather_for_bookmark`.
- **The Fix:** Ensure the correct method is used based on whether caching logic and the `force_refresh` parameter are required.

## 🏗️ Pydantic & SQLModel Instantiation
**Insight:** Always use keyword arguments when instantiating Pydantic `BaseModel` or SQLModel classes.
- **The Issue:** `BaseModel.__init__()` takes only keyword arguments (after `self`). Passing positional arguments (e.g., `Response(data, cursor)`) results in a `TypeError`.
- **The Fix:** Explicitly pass keyword arguments: `Response(data=data, next_cursor=cursor)`.
