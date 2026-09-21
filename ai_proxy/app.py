"""
app.py
======

The FastAPI application itself.

Design:
  - A single generic route matches ANY path under /v1/* and forwards
    it to the configured upstream, preserving method, query string,
    body, and (filtered) headers. This is deliberate: the proxy must
    not hard-code a fixed list of "supported" endpoints, so any
    OpenAI-compatible route the upstream exposes (chat/completions,
    completions, embeddings, models, and anything else) works.
  - Streaming responses (``"stream": true`` / text/event-stream) are
    relayed chunk-by-chunk as they arrive, never buffered in full.
  - The client's proxy API key is verified with a constant-time
    comparison; on success it is swapped for the real upstream key
    before the request leaves the proxy. The client's Authorization
    header is never forwarded upstream as-is.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import RuntimeConfig
from .logging_setup import setup_logging
from .security import constant_time_equals, redact_secret_strings
from .termux import acquire_wake_lock, release_wake_lock
from .upstream import build_upstream_client, filter_hop_by_hop

logger = setup_logging()


def _openai_error(message: str, error_type: str, status_code: int) -> JSONResponse:
    """Build an OpenAI-style error JSON response. Message is redacted
    defensively even though callers should only ever pass safe text."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": redact_secret_strings(message),
                "type": error_type,
            }
        },
    )


def create_app(config: RuntimeConfig, *, enable_wake_lock: bool = True) -> FastAPI:
    """
    Build and return the FastAPI app for a given runtime configuration.
    A fresh httpx.AsyncClient is created on startup and closed on
    shutdown via the lifespan context manager.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = config
        app.state.http_client = build_upstream_client(
            connect_timeout=config.connect_timeout,
            read_timeout=config.read_timeout,
            write_timeout=config.write_timeout,
            pool_timeout=config.pool_timeout,
        )
        wake_lock_held = False
        if enable_wake_lock:
            wake_lock_held = acquire_wake_lock()
        app.state.wake_lock_held = wake_lock_held

        print("[✓] Proxy started")
        print(f"[✓] Listening on http://{config.host}:{config.port}")
        print("[✓] OpenAI-compatible API enabled")
        print("[✓] Streaming enabled")
        print()
        print("Cloud is running...")
        print()

        try:
            yield
        finally:
            await app.state.http_client.aclose()
            if enable_wake_lock:
                release_wake_lock()

    app = FastAPI(
        title="ai-proxy",
        description="Local OpenAI-compatible reverse proxy for Termux.",
        version="1.0.0",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def _authenticate(request: Request) -> str | None:
        """
        Return None if authenticated, or a safe error message string
        if not. Uses constant-time comparison against the proxy key.
        """
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return "Missing or malformed Authorization header."

        supplied = auth_header[7:].strip()
        expected = request.app.state.config.proxy_api_key

        if not supplied or not constant_time_equals(supplied, expected):
            return "Invalid API key."

        return None

    # ------------------------------------------------------------------
    # Safe access logging (method + path + status only)
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def safe_access_log(request: Request, call_next):
        start = time.monotonic()
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] {request.method} {request.url.path}")
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("Unhandled error while processing request.")
            return _openai_error(
                "Internal proxy error.", "proxy_error", 500
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        timestamp = time.strftime("%H:%M:%S")
        print(
            f"[{timestamp}] upstream response: {response.status_code} "
            f"({duration_ms}ms)"
        )
        return response

    # ------------------------------------------------------------------
    # Health check (local-only convenience, not part of the OpenAI API)
    # ------------------------------------------------------------------
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "base_url": config.local_base_url()}

    # ------------------------------------------------------------------
    # Generic /v1/* forwarding -- this is the actual proxy.
    # ------------------------------------------------------------------
    @app.api_route(
        "/v1/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def forward(full_path: str, request: Request):
        auth_error = _authenticate(request)
        if auth_error:
            return _openai_error(auth_error, "authentication_error", 401)

        cfg: RuntimeConfig = request.app.state.config
        client: httpx.AsyncClient = request.app.state.http_client

        target_url = f"{cfg.upstream_base_url}/{full_path}"

        # Build outgoing headers: strip hop-by-hop + the client's own
        # Authorization, then set the real upstream credential.
        incoming_headers = dict(request.headers)
        incoming_headers.pop("authorization", None)
        incoming_headers.pop("host", None)
        outgoing_headers = filter_hop_by_hop(incoming_headers)
        outgoing_headers["authorization"] = f"Bearer {cfg.upstream_api_key}"

        body = await request.body()

        # Detect whether the client asked for a streamed response. We
        # sniff the JSON body for "stream": true, which is how the
        # OpenAI API signals this; if the body isn't JSON (e.g. GET
        # requests), this is simply False.
        wants_stream = False
        if body:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    wants_stream = bool(parsed.get("stream"))
            except ValueError:
                wants_stream = False

        # Preserve repeated query params (e.g. ?a=1&a=2) instead of
        # collapsing them, by passing explicit (key, value) pairs.
        query_pairs = list(request.query_params.multi_items())

        try:
            if wants_stream:
                return await _proxy_streaming(
                    client,
                    method=request.method,
                    url=target_url,
                    headers=outgoing_headers,
                    params=query_pairs,
                    content=body,
                )
            return await _proxy_buffered(
                client,
                method=request.method,
                url=target_url,
                headers=outgoing_headers,
                params=query_pairs,
                content=body,
            )
        except httpx.ConnectError:
            return _openai_error(
                "Could not reach upstream server.", "upstream_connection_error", 502
            )
        except httpx.ConnectTimeout:
            return _openai_error(
                "Connecting to upstream timed out.", "upstream_timeout", 504
            )
        except httpx.ReadTimeout:
            return _openai_error(
                "Upstream took too long to respond.", "upstream_timeout", 504
            )
        except httpx.HTTPError:
            return _openai_error(
                "Upstream request failed.", "upstream_error", 502
            )

    return app


async def _proxy_buffered(
    client: httpx.AsyncClient,
    *,
    method: str,
    url: str,
    headers: dict,
    params,
    content: bytes,
) -> Response:
    """Forward a non-streaming request and relay the full response,
    preserving the upstream status code and (filtered) headers."""
    upstream_response = await client.request(
        method,
        url,
        headers=headers,
        params=params,
        content=content if content else None,
    )
    response_headers = filter_hop_by_hop(dict(upstream_response.headers))
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )


async def _proxy_streaming(
    client: httpx.AsyncClient,
    *,
    method: str,
    url: str,
    headers: dict,
    params,
    content: bytes,
) -> StreamingResponse:
    """
    Forward a streaming (SSE) request and relay it chunk-by-chunk as
    it arrives from upstream, never buffering the full response before
    sending anything to the client.
    """
    req = client.build_request(
        method,
        url,
        headers=headers,
        params=params,
        content=content if content else None,
    )
    upstream_response = await client.send(req, stream=True)

    if upstream_response.status_code >= 400:
        # Surface upstream errors with their real status code, but
        # still avoid buffering unboundedly -- error bodies are small.
        error_body = await upstream_response.aread()
        await upstream_response.aclose()
        response_headers = filter_hop_by_hop(dict(upstream_response.headers))
        return Response(
            content=error_body,
            status_code=upstream_response.status_code,
            headers=response_headers,
            media_type=upstream_response.headers.get("content-type"),
        )

    async def event_stream():
        try:
            async for chunk in upstream_response.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await upstream_response.aclose()

    response_headers = filter_hop_by_hop(dict(upstream_response.headers))
    media_type = upstream_response.headers.get("content-type", "text/event-stream")

    return StreamingResponse(
        event_stream(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=media_type,
    )
