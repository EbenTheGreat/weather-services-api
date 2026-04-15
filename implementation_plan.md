# Implementation Plan: Production-Grade Observability for Project 2

This plan outlines the steps to integrate the "Three Pillars of Observability" (Logs, Metrics, Traces) into the Weather Bookmark API, following the best practices demonstrated in Lecture 18.

## User Review Required

> [!IMPORTANT]
> This plan introduces new dependencies: `python-json-logger`, `prometheus-fastapi-instrumentator`, and OpenTelemetry packages. Ensure these are installed in your environment before deployment.

> [!NOTE]
> We will shift from basic database-only AI logging to a unified logging system that outputs to both the console (for developers) and structured JSON (for production collectors), while maintaining the `AILog` table for business-specific audit trails.

## Proposed Changes

### Configuration & Setup

#### [MODIFY] [config.py](file:///c:/Users/user/Desktop/fastapi/backend-fastapi/personal-projects/project-2/config.py)
- Add `ENVIRONMENT` (defaulting to "development") to the `Settings` class.

#### [NEW] [logging_config.py](file:///c:/Users/user/Desktop/fastapi/backend-fastapi/personal-projects/project-2/logging_config.py)
- Detect `ENVIRONMENT`.
- Configure `logging.basicConfig`:
  - **Dev**: Human-readable console logs, `DEBUG` level.
  - **Prod**: Structured JSON logs (via `python-json-logger`), `INFO` level.
- Initialize `request_id_context` using `ContextVar` for thread-safe request correlation.
- Define `get_request_id()` helper.

---

### Middleware & Core Integration

#### [NEW] [middleware.py](file:///c:/Users/user/Desktop/fastapi/backend-fastapi/personal-projects/project-2/middleware.py)
- Implement `LoggingMiddleware(BaseHTTPMiddleware)`:
  - Generate/Extract `request_id`.
  - Log request receiving (method, path, client_ip).
  - Track execution time.
  - Log response completion (status code, duration).
  - Inject `X-Request-ID` into response headers.

#### [MODIFY] [main.py](file:///c:/Users/user/Desktop/fastapi/backend-fastapi/personal-projects/project-2/main.py)
- Import and initialize logging configuration.
- Add `LoggingMiddleware`.
- Setup `Prometheus` metrics at `/metrics`.
- Setup `OpenTelemetry` auto-instrumentation for FastAPI.

---

### Service Instrumentation

#### [MODIFY] [ai_orchestrator.py](file:///c:/Users/user/Desktop/fastapi/backend-fastapi/personal-projects/project-2/ai_orchestrator.py)
- Replace `print` statements (if any) with `logger.info/debug`.
- Add standard logging to `handle_chat` to track cache hits/misses and model latency alongside the DB log.
- Use `get_request_id()` to correlate AI logs with the originating HTTP request.
- Add OpenTelemetry spans for custom tracing of the AI execution flow.

#### [MODIFY] [weather_service_2.py](file:///c:/Users/user/Desktop/fastapi/backend-fastapi/personal-projects/project-2/weather_service_2.py)
- Inject `logger` into `WeatherCacheService` and `WeatherAPIService`.
- Log cache hits/misses, rate limit triggers, and external API call outcomes.

---

## Open Questions

- Should we include the `ConsoleSpanExporter` for OpenTelemetry tracing even in production, or only for development? (Usually, prod uses an OTLP exporter to a backend like Jaeger/Honeycomb).
- Do you have a preferred threshold for logging "slow" requests in the middleware (e.g., warning if > 500ms)?

---

## Verification Plan

### Automated Tests
- Run `pytest` (if existing) to ensure no regressions.
- Verify log output format by switching `ENVIRONMENT` variable.
- Access `http://localhost:8000/metrics` to verify Prometheus data.

### Manual Verification
1.  **Dev Mode**: Start the app with `ENVIRONMENT=development` and check for colorized, human-readable logs.
2.  **Prod Mode**: Start the app with `ENVIRONMENT=production` and check for JSON formatted logs.
3.  **Request Correlation**: Call `/v1/ai/chat`, check the `X-Request-ID` header, and verify that the same ID appears in all logs related to that request.
4.  **Error Handling**: Trigger an error (e.g., invalid city) and ensure the logs capture the stack trace and relevant metadata.
