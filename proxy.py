#!/usr/bin/env python3
"""
proxy.py
========

Entry point for ai-proxy: a local OpenAI-compatible reverse proxy /
API gateway, designed to run inside Termux on Android.

Run with:

    python proxy.py

This script:
  1. Prompts interactively for the real upstream API key, base URL,
     and a Model ID to verify against.
  2. Normalizes the base URL (handles trailing slashes, missing /v1).
  3. Verifies the upstream is reachable and the API key is accepted
     via GET {base_url}/models (no inference call, so no tokens are
     spent just to verify).
  4. Generates a random, session-only proxy API key that the client
     will use instead of the real key.
  5. Starts the local-only (127.0.0.1) FastAPI/Uvicorn server, which
     forwards every request under /v1/* to the upstream, swapping the
     proxy key for the real key, and streams responses through
     unbuffered.

The real upstream API key lives only in process memory. It is never
written to disk, never logged, and never sent to the client.
"""

from __future__ import annotations

import asyncio
import getpass
import sys

import uvicorn

from ai_proxy.app import create_app
from ai_proxy.config import (
    DEFAULT_PORT,
    LOCAL_HOST,
    InvalidBaseURLError,
    RuntimeConfig,
    normalize_base_url,
)
from ai_proxy.logging_setup import quiet_uvicorn_access_log, setup_logging
from ai_proxy.security import generate_proxy_api_key
from ai_proxy.upstream import build_upstream_client, verify_upstream

BANNER = """========================================
            AI PROXY v1
      OpenAI-Compatible Gateway
========================================"""


def prompt_nonempty(label: str, *, secret: bool = False) -> str:
    """Prompt until a non-empty value is entered. Ctrl+C exits cleanly."""
    while True:
        try:
            if secret:
                # getpass avoids echoing the real API key to the
                # terminal / shell history / screen recordings.
                value = getpass.getpass(f"{label}\n> ")
            else:
                value = input(f"{label}\n> ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)

        value = value.strip()
        if value:
            return value
        print("This value cannot be empty. Please try again.\n")


def collect_startup_input() -> tuple[str, str, str]:
    print(BANNER)
    print()
    api_key = prompt_nonempty("api key:", secret=True)
    print()
    base_url_raw = prompt_nonempty("base url:")
    print()
    model_id = prompt_nonempty("Model ID:")
    print()
    return api_key, base_url_raw, model_id


async def run_verification(base_url: str, api_key: str, model_id: str):
    """
    Run the upstream verification check using a short-lived httpx
    client (separate from the long-lived one the running app will use).
    """
    client = build_upstream_client(connect_timeout=10.0, read_timeout=20.0)
    try:
        result = await verify_upstream(client, base_url, api_key, model_id)
    finally:
        await client.aclose()
    return result


def main() -> None:
    setup_logging()
    quiet_uvicorn_access_log()

    api_key, base_url_raw, model_id = collect_startup_input()

    try:
        base_url = normalize_base_url(base_url_raw)
    except InvalidBaseURLError as exc:
        print(f"[✗] Invalid base URL: {exc}")
        sys.exit(1)

    print("Verifying upstream...")
    try:
        result = asyncio.run(run_verification(base_url, api_key, model_id))
    except Exception:
        # Defensive catch-all: verification must never crash with a
        # traceback that could contain the API key (e.g. via an httpx
        # exception's request repr). Show a generic, safe message.
        print("----------------------------------------")
        print("[✗] Upstream verification failed.")
        print("An unexpected error occurred while contacting upstream.")
        print("----------------------------------------")
        sys.exit(1)

    if not result.ok:
        print("----------------------------------------")
        print("[✗] Upstream verification failed.")
        if result.status_code is not None:
            print(f"HTTP status: {result.status_code}")
        if result.message:
            print(result.message)
        print("----------------------------------------")
        sys.exit(1)

    print("----------------------------------------")
    print("[✓] Upstream verification successful")

    if result.model_list_supported and result.model_available is False:
        print()
        print(f"[!] Warning: Model ID '{model_id}' was not found in the")
        print("    upstream /models list. It will still be used for")
        print("    verification purposes only -- clients may request")
        print("    any model the upstream actually supports.")

    proxy_key = generate_proxy_api_key()
    config = RuntimeConfig(
        upstream_base_url=base_url,
        upstream_api_key=api_key,
        startup_model_id=model_id,
        proxy_api_key=proxy_key,
        host=LOCAL_HOST,
        port=DEFAULT_PORT,
    )

    print()
    print("Proxy API key:")
    print(proxy_key)
    print()
    print("Base URL:")
    print(config.local_base_url())
    print("----------------------------------------")
    print()

    try:
        input("Press ENTER to start cloud...")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)

    print()

    # Free the plaintext local variable references we no longer need;
    # the real key continues to live only inside `config`, in memory,
    # for the lifetime of this process.
    del api_key

    app = create_app(config)

    uvicorn_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)

    # uvicorn.Server installs its own SIGINT/SIGTERM handlers when run
    # this way, which trigger graceful shutdown -- this runs the
    # FastAPI lifespan's shutdown path: closing the httpx client and
    # releasing the Termux wake lock. No manual signal wiring needed.
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[✓] Proxy stopped. Goodbye.")


if __name__ == "__main__":
    main()
