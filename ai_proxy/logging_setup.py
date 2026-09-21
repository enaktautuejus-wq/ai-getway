"""
logging_setup.py
=================

Configures a minimal, safe logger for the proxy.

Default logging is intentionally low-detail: method + path, upstream
status code, and stream completion markers. It never logs headers,
request bodies, or response bodies unless the caller explicitly opts
into debug mode -- and even then, secrets are redacted first.
"""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "[%(asctime)s] %(message)s"
DATE_FORMAT = "%H:%M:%S"


def setup_logging(debug: bool = False) -> logging.Logger:
    logger = logging.getLogger("ai_proxy")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False

    # Avoid duplicate handlers if setup_logging is called more than once
    # (e.g. under a reloader).
    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        logger.addHandler(handler)

    return logger


# Uvicorn's own access logger can leak query strings (which could in
# theory contain a credential a client mistakenly put in the URL) and
# duplicates what we log ourselves. We keep it, but at a level that
# only shows real problems, and rely on our own request logger
# (see ai_proxy/app.py) for the safe, redacted access log lines.
def quiet_uvicorn_access_log() -> None:
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
