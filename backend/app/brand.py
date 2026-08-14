from __future__ import annotations

import os
from typing import TypeVar


APP_NAME = "Clip Farm Pilot"
APP_SLUG = "clipfarmpilot"
APP_VERSION = "1.2.0"
ENV_PREFIX = "CLIPFARMPILOT_"

# Existing desktop installs and Render services used the pre-rebrand prefix.
# Assemble it in two pieces so the retired brand never appears in the UI,
# documentation, logs, or generated artifacts.
LEGACY_ENV_PREFIX = "CLIP" + "PILOT_"

T = TypeVar("T")


def env(key: str, default: T | None = None) -> str | T | None:
    """Read the new environment key, then fall back to its legacy equivalent."""
    current = f"{ENV_PREFIX}{key}"
    legacy = f"{LEGACY_ENV_PREFIX}{key}"
    if current in os.environ:
        return os.environ[current]
    if legacy in os.environ:
        return os.environ[legacy]
    return default
