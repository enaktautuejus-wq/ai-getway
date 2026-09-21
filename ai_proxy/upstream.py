"""
upstream.py
===========

Everything related to talking to the upstream OpenAI-compatible API:

  - A shared httpx.AsyncClient factory with proper connection pooling
    and split timeouts (connect / read / write / pool).
  - Startup verification: confirms the upstream is reachable, the real
    API key is accepted, and (if the provider exposes GET /models)
    that the requested Model ID is actually available.

Verification never prints the real API key, and error messages never
include the Authorization header value.
"""

from __future__ import annotations

import httpx

from .config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_POOL_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_WRITE_TIMEOUT,
)

# Hop-by-hop headers per RFC 7230 §6.1 (plus a couple of practical
# additions). These must never be blindly forwarded in either
# direction between client <-> proxy <-> upstream.
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",  # recomputed by the HTTP layer as needed
}


def build_timeout(
    connect: float = DEFAULT_CONNECT_TIMEOUT,
    read: float = DEFAULT_READ_TIMEOUT,
    write: float = DEFAULT_WRITE_TIMEOUT,
    pool: float = DEFAULT_POOL_TIMEOUT,
) -> httpx.Timeout:
    return httpx.Timeout(connect=connect, read=read, write=write, pool=pool)


def build_upstream_client(
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    write_timeout: float = DEFAULT_WRITE_TIMEOUT,
    pool_timeout: float = DEFAULT_POOL_TIMEOUT,
) -> httpx.AsyncClient:
    """
    Build the shared async HTTP client used for all upstream requests.
    Connection pooling is on by default in httpx.AsyncClient; we just
    make limits and timeouts explicit and sane for long-lived LLM
    streaming responses.
    """
    limits = httpx.Limits(
        max_connections=20,
        max_keepalive_connections=10,
    )
    timeout = build_timeout(
        connect_timeout, read_timeout, write_timeout, pool_timeout
    )
    return httpx.AsyncClient(
        limits=limits,
        timeout=timeout,
        follow_redirects=True,
    )


class VerificationResult:
    def __init__(
        self,
        ok: bool,
        status_code: int | None = None,
        message: str = "",
        model_available: bool | None = None,
        model_list_supported: bool = False,
    ) -> None:
        self.ok = ok
        self.status_code = status_code
        self.message = message
        self.model_available = model_available
        self.model_list_supported = model_list_supported


async def verify_upstream(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model_id: str,
) -> VerificationResult:
    """
    Verify the upstream is reachable and the API key is valid by
    calling GET {base_url}/models. If the endpoint responds with a
    model list, also check whether ``model_id`` is present in it.

    This deliberately does NOT fall back to a completion/inference
    call that would consume tokens -- per spec, /models is the
    authoritative check for v1. If /models itself is unreachable or
    errors, verification simply fails with a safe error message.
    """
    url = f"{base_url}/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    try:
        response = await client.get(url, headers=headers)
    except httpx.ConnectTimeout:
        return VerificationResult(
            ok=False, message="Connection to upstream timed out."
        )
    except httpx.ReadTimeout:
        return VerificationResult(
            ok=False, message="Upstream took too long to respond."
        )
    except httpx.ConnectError:
        return VerificationResult(
            ok=False, message="Could not connect to upstream host."
        )
    except httpx.HTTPError as exc:
        return VerificationResult(
            ok=False, message=f"Upstream request failed ({type(exc).__name__})."
        )

    if response.status_code == 401 or response.status_code == 403:
        return VerificationResult(
            ok=False,
            status_code=response.status_code,
            message="Upstream rejected the API key.",
        )

    if response.status_code >= 500:
        return VerificationResult(
            ok=False,
            status_code=response.status_code,
            message="Upstream server error.",
        )

    if response.status_code >= 400:
        return VerificationResult(
            ok=False,
            status_code=response.status_code,
            message="Upstream rejected the request.",
        )

    # 2xx: try to parse a model list, but don't hard-fail verification
    # if the response isn't the shape we expect -- some providers
    # implement /models slightly differently.
    model_ids: list[str] = []
    model_list_supported = False
    try:
        data = response.json()
        raw_list = data.get("data") if isinstance(data, dict) else None
        if isinstance(raw_list, list):
            model_list_supported = True
            for entry in raw_list:
                if isinstance(entry, dict) and "id" in entry:
                    model_ids.append(str(entry["id"]))
                elif isinstance(entry, str):
                    model_ids.append(entry)
    except ValueError:
        # Not JSON, or unexpected shape -- still treat connectivity +
        # auth as verified since we got a non-error HTTP status.
        pass

    model_available = None
    if model_list_supported:
        model_available = model_id in model_ids

    return VerificationResult(
        ok=True,
        status_code=response.status_code,
        message="Upstream verification successful.",
        model_available=model_available,
        model_list_supported=model_list_supported,
    )


def filter_hop_by_hop(headers: dict) -> dict:
    """Remove hop-by-hop headers (case-insensitively) from a header dict."""
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
