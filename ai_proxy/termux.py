"""
termux.py
=========

Thin wrapper around Termux's wake lock commands.

``termux-wake-lock`` prevents Android from suspending the CPU while the
proxy is running in the background (e.g. screen off). This is optional:
if the Termux:API tools are not installed, the proxy must keep running
and simply warn the user instead of crashing.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger("ai_proxy")

WAKE_LOCK_CMD = "termux-wake-lock"
WAKE_UNLOCK_CMD = "termux-wake-unlock"


def _command_available(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def acquire_wake_lock() -> bool:
    """
    Attempt to acquire the Termux wake lock. Returns True on success,
    False if unavailable or it failed for any reason. Never raises.
    """
    if not _command_available(WAKE_LOCK_CMD):
        print("[!] Termux wake lock unavailable")
        return False
    try:
        subprocess.run(
            [WAKE_LOCK_CMD],
            check=True,
            capture_output=True,
            timeout=10,
        )
        print("[✓] Wake lock enabled")
        return True
    except Exception:
        # Deliberately broad: any failure here must never crash the
        # proxy. We only ever print a generic, safe message.
        print("[!] Termux wake lock unavailable")
        return False


def release_wake_lock() -> None:
    """
    Attempt to release the Termux wake lock. Best-effort, never raises.
    """
    if not _command_available(WAKE_UNLOCK_CMD):
        return
    try:
        subprocess.run(
            [WAKE_UNLOCK_CMD],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        # Nothing useful to do on shutdown if this fails; stay silent
        # so shutdown isn't noisy/alarming for something non-critical.
        pass
