"""Public API for the power assessment module.

Typical usage::

    from app.power import load_artifacts, assess, render

    loaded = load_artifacts(["run-a.json", "run-b.json"])
    report = assess(loaded, AssessmentSettings())
    print(render(report, "md"))
"""
from __future__ import annotations

from .engine import REPORT_VERSION, assess
from .errors import (
    PowerArtifactError,
    PowerConfigError,
    PowerError,
    PowerInputError,
)
from .loader import ARTIFACT_VERSION, load_artifacts
from .models import AssessmentSettings, Issue, RuleObservation
from .render import render

__all__ = [
    "ARTIFACT_VERSION",
    "AssessmentSettings",
    "Issue",
    "PowerArtifactError",
    "PowerConfigError",
    "PowerError",
    "PowerInputError",
    "REPORT_VERSION",
    "RuleObservation",
    "assess",
    "load_artifacts",
    "render",
]
