"""Validate the explicit development-only showcase boundary."""

import os
from collections.abc import Mapping


def showcase_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """Return whether the caller explicitly selected the showcase runtime."""
    selected = os.environ if environment is None else environment
    return selected.get("SHOWCASE_MODE", "").strip().lower() == "true"


def validate_showcase_environment(
    environment: Mapping[str, str] | None = None,
) -> None:
    """Reject showcase adapters outside an explicitly selected development mode."""
    selected = os.environ if environment is None else environment
    if not showcase_enabled(selected):
        return
    if selected.get("APP_ENV", "").strip().lower() != "development":
        raise RuntimeError("SHOWCASE_MODE is allowed only when APP_ENV=development")
    if selected.get("IDENTITY_PROVIDER", "").strip().lower() != "demo":
        raise RuntimeError("SHOWCASE_MODE requires the explicit demo identity provider")

