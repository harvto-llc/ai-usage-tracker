"""Keychain-backed storage for provider web credentials on macOS."""

import hmac
import os
import shlex
import subprocess
import sys
from pathlib import Path


KEYCHAIN_SERVICE = "com.harvto.usage-tracker.web-cookie"
SUPPORTED_PROVIDERS = frozenset({"claude", "codex"})
SECURITY_BIN = Path("/usr/bin/security")


def keychain_enabled() -> bool:
    setting = os.environ.get("USAGE_TRACKER_KEYCHAIN", "1").strip().lower()
    return (
        setting not in {"0", "false", "no", "off"}
        and sys.platform == "darwin"
        and SECURITY_BIN.exists()
    )


def load_provider_credential(provider: str) -> str | None:
    if provider not in SUPPORTED_PROVIDERS or not keychain_enabled():
        return None
    try:
        result = subprocess.run(
            [
                str(SECURITY_BIN),
                "find-generic-password",
                "-a",
                provider,
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.rstrip("\r\n")
    return value or None


def store_provider_credential(provider: str, value: str) -> bool:
    if provider not in SUPPORTED_PROVIDERS or not value or not keychain_enabled():
        return False
    keychain_command = " ".join((
        "add-generic-password",
        "-U",
        "-a", shlex.quote(provider),
        "-s", shlex.quote(KEYCHAIN_SERVICE),
        "-l", shlex.quote(f"Usage Tracker {provider.title()} web session"),
        "-w", shlex.quote(value),
    ))
    try:
        result = subprocess.run(
            [str(SECURITY_BIN), "-i"],
            input=f"{keychain_command}\n",
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return hmac.compare_digest(load_provider_credential(provider) or "", value)
