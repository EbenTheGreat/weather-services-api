import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """
    Configure logging for the entire application.
    Call this once at startup in main.py.

    Log format:
        2026-04-10 13:00:00,000 | INFO     | ai_layer.orchestrator | Message here
    """
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt=date_fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),  # Console output
        ],
    )

    # Quieten noisy third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pydantic_ai").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
