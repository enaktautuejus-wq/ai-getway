"""
security.py
============

Security-sensitive helpers:

  - Cryptographically secure generation of the client-facing proxy API key.
  - Constant-time comparison for verifying that key on each request.
  - Redaction helpers used by logging/error handling so secrets never
    end up in logs, tracebacks, or client-facing error responses.

Nothing here ever prints or logs the real upstream API key.
"""

from __future__ import annotations

import re
import secrets

PROXY_KEY_PREFIX = "sk-xx-"
PROXY_KEY_RANDOM_BYTES = 18  # -> 24 base64url chars, plenty of entropy

# Headers that must never be written to logs, even in debug mode.
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "cookie",
    "set-cookie",
}

_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9\-_]{6,}\b")


def generate_proxy_api_key() -> str:
    """
    Generate a new random client-facing proxy API key using the
    ``secrets`` module (CSPRNG), never the ``random`` module.

    Format: sk-xx-<url-safe random string>
    """
    token = secrets.token_urlsafe(PROXY_KEY_RANDOM_BYTES)
    return f"{PROXY_KEY_PREFIX}{token}"


def constant_time_equals(a: str, b: str) -> bool:
    """
    Constant-time string comparison, used to check the client-supplied
    proxy API key against the real one without leaking timing info.
    """
    return secrets.compare_digest(a, b)


def redact_secret_strings(text: str) -> str:
    """
    Best-effort redaction of anything that looks like a bearer token or
    an API key (sk-... style) inside a free-text string. Used as a
    defensive last line before any text is logged or ever surfaced in
    an error message, in case it originated from something that might
    contain a credential.
    """
    if not text:
        return text
    redacted = _BEARER_RE.sub(r"\1***redacted***", text)
    redacted = _SK_KEY_RE.sub("sk-***redacted***", redacted)
    return redacted


def is_sensitive_header(header_name: str) -> bool:
    return header_name.lower() in SENSITIVE_HEADER_NAMES


def safe_header_preview(headers) -> dict:
    """
    Build a dict of headers safe for logging: sensitive header values
    are replaced with a redaction marker. Accepts anything with a
    ``.items()`` iterator (dict, httpx.Headers, starlette Headers).
    """
    preview = {}
    for key, value in headers.items():
        if is_sensitive_header(key):
            preview[key] = "***redacted***"
        else:
            preview[key] = value
    return preview
