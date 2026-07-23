import logging
import re

import structlog


_SECRET_PATTERN = re.compile(r"(?i)(access_token|refresh_token|authorization|client_secret)")


def _redact(_: object, __: str, event_dict: dict) -> dict:
    for key in tuple(event_dict):
        if _SECRET_PATTERN.search(str(key)):
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            _redact,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


log = structlog.get_logger()

