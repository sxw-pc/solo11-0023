"""Typed errors for the power assessment module."""
from __future__ import annotations

from typing import Any

from ..errors import CaliberError


class PowerError(CaliberError):
    """Base class for power-module failures."""

    code = "power_error"


class PowerInputError(PowerError):
    """The invocation or supplied artifacts cannot be used at all."""

    code = "power_input_error"


class PowerArtifactError(PowerError):
    """Artifacts were read but contained fatal, unusable content."""

    code = "power_artifact_error"

    def __init__(self, message: str, *, details: Any | None = None) -> None:
        super().__init__(message, details=details)


class PowerConfigError(PowerError):
    """Statistical parameters are outside their allowed ranges."""

    code = "power_config_error"
