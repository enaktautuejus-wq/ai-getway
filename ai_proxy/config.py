"""
config.py
=========

Runtime configuration container and URL normalization helpers.

Nothing in this module ever logs or prints the real upstream API key.
The ``RuntimeConfig`` object lives only in process memory for the
lifetime of the running proxy and is never written to disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

# Local-only bind address. v1 intentionally never binds to 0.0.0.0.
LOCAL_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

# Default timeouts (seconds). Read timeout is generous because LLM
# inference (especially long completions) can take a long time, and
# streaming responses must not be killed just because the proxy grew
# impatient waiting for the next chunk.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_WRITE_TIMEOUT = 30.0
DEFAULT_POOL_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 600.0


class InvalidBaseURLError(ValueError):
    """Raised when the user-provided base URL cannot be normalized."""


def normalize_base_url(raw_url: str) -> str:
    """
    Normalize a user-supplied upstream base URL.

    Rules:
      - Strip surrounding whitespace.
      - Require http:// or https:// scheme.
      - Strip any trailing slash(es).
      - If the path does not already end in "/v1" (or contain a deeper
        versioned API path ending in "/v1"), append "/v1".
      - Never produce a doubled "/v1/v1" suffix.

    Examples:
        https://example.com/v1     -> https://example.com/v1
        https://example.com/v1/    -> https://example.com/v1
        https://example.com        -> https://example.com/v1
        https://example.com/       -> https://example.com/v1
    """
    if not raw_url or not raw_url.strip():
        raise InvalidBaseURLError("Base URL cannot be empty.")

    candidate = raw_url.strip()

    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        raise InvalidBaseURLError(
            "Base URL must start with http:// or https://"
        )
    if not parts.netloc:
        raise InvalidBaseURLError("Base URL is missing a host.")

    # Collapse a trailing run of slashes in the path.
    path = re.sub(r"/+$", "", parts.path)

    if path == "" or path == "/":
        path = "/v1"
    elif not re.search(r"(^|/)v1$", path):
        path = f"{path}/v1"
    # else: path already ends in /v1 -> leave as-is, do not double it.

    normalized = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return normalized


@dataclass
class RuntimeConfig:
    """
    Holds everything the running proxy needs. Created once at startup
    and attached to ``app.state`` (see ai_proxy/app.py). The real
    upstream API key lives here and nowhere else persistent.
    """

    upstream_base_url: str
    upstream_api_key: str
    startup_model_id: str
    proxy_api_key: str
    host: str = LOCAL_HOST
    port: int = DEFAULT_PORT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    write_timeout: float = DEFAULT_WRITE_TIMEOUT
    pool_timeout: float = DEFAULT_POOL_TIMEOUT
    debug: bool = False

    def local_base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    # Deliberately no __repr__/__str__ override that includes the key —
    # dataclass default repr WOULD include upstream_api_key, so we
    # override it explicitly to avoid accidental leakage via logging,
    # tracebacks, or debuggers that call repr() on this object.
    def __repr__(self) -> str:  # pragma: no cover - safety, not logic
        return (
            "RuntimeConfig("
            f"upstream_base_url={self.upstream_base_url!r}, "
            "upstream_api_key='***redacted***', "
            f"startup_model_id={self.startup_model_id!r}, "
            "proxy_api_key='***redacted***', "
            f"host={self.host!r}, port={self.port!r})"
        )

    __str__ = __repr__
