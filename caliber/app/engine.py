"""SQLite engine version gate, runtime capability probing and execution profiles.

The version floor is enforced when a repository connection is established;
capabilities are probed on the live connection (never assumed from the
version string alone, because features such as JSON1 can be compiled out).
An :class:`ExecutionProfile` records which dialect was selected and which
fallback strategies fired during a run or report build, and is persisted
into the run's execution metadata.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .errors import DatabaseVersionError

DEFAULT_MIN_SQLITE_VERSION = "3.25.0"

_VERSION_RE = re.compile(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.\d+)?\s*$")

#: Human-readable labels and the SQLite version that introduced each feature.
FEATURE_INFO = {
    "window_functions": {"label": "窗口函数", "min_version": "3.25.0"},
    "json1": {"label": "JSON1 扩展函数", "min_version": "3.9.0"},
    "upsert": {"label": "UPSERT（ON CONFLICT DO UPDATE）", "min_version": "3.24.0"},
}


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse ``"3.40.1"`` style version strings into a comparable tuple."""
    match = _VERSION_RE.match(str(text))
    if not match:
        raise ValueError(f"无法解析的 SQLite 版本号: {text!r}")
    parts = [int(part) for part in match.groups() if part is not None]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def format_version(parts: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in parts)


def engine_version(conn: sqlite3.Connection) -> str:
    """Return the runtime SQLite version of an open connection.

    This queries the engine itself (``SELECT sqlite_version()``) rather than
    reading ``sqlite3.sqlite_version``, so it reflects the library that will
    actually execute SQL on this connection.
    """
    row = conn.execute("SELECT sqlite_version()").fetchone()
    return str(row[0])


def ensure_min_engine_version(
    conn: sqlite3.Connection, minimum: str | None = None
) -> str:
    """Reject connections whose engine is older than the configured floor."""
    required_text = minimum or DEFAULT_MIN_SQLITE_VERSION
    required = parse_version(required_text)
    current_text = engine_version(conn)
    if parse_version(current_text) < required:
        raise DatabaseVersionError(
            f"SQLite 引擎版本不满足要求：当前 {current_text}，最低要求 {required_text}。"
            "请升级运行环境的 SQLite（通常随 Python 一起升级）；"
            "如确认要在此环境运行，可将 CALIBER_MIN_SQLITE_VERSION 调低，"
            "受支持的新语法会自动回退为兼容写法。",
            details={"current": current_text, "required": required_text},
        )
    return current_text


def _probe(conn: sqlite3.Connection, sql: str) -> bool:
    try:
        conn.execute(sql).fetchall()
        return True
    except sqlite3.Error:
        return False


@dataclass(frozen=True)
class Capabilities:
    """Feature set detected on one live connection."""

    sqlite_version: str
    window_functions: bool
    json1: bool
    upsert: bool

    def supports(self, feature: str) -> bool:
        return bool(getattr(self, feature, False))

    @property
    def dialect(self) -> str:
        return "modern" if (self.window_functions and self.json1 and self.upsert) else "legacy"

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_functions": self.window_functions,
            "json1": self.json1,
            "upsert": self.upsert,
        }


def probe_capabilities(conn: sqlite3.Connection) -> Capabilities:
    """Detect the SQL dialect this connection can execute.

    Window functions and JSON1 are probed by executing trivial statements,
    because both can be compiled out of the library.  UPSERT cannot be
    compiled out, so its version bound is reliable.
    """
    version = engine_version(conn)
    return Capabilities(
        sqlite_version=version,
        window_functions=_probe(conn, "SELECT row_number() OVER ()"),
        json1=_probe(conn, "SELECT value FROM json_each('[1]')"),
        upsert=parse_version(version) >= (3, 24, 0),
    )


@dataclass
class ExecutionProfile:
    """Collects dialect/fallback decisions for one run or report build."""

    capabilities: Capabilities
    fallbacks: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)

    @property
    def dialect(self) -> str:
        return self.capabilities.dialect

    def record_fallback(self, feature: str, strategy: str, reason: str) -> None:
        entry = {"feature": feature, "strategy": strategy, "reason": reason}
        if entry not in self.fallbacks:
            self.fallbacks.append(entry)

    def record_blocked(self, feature: str, detail: str) -> None:
        entry = {"feature": feature, "detail": detail}
        if entry not in self.blocked:
            self.blocked.append(entry)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sqlite_version": self.capabilities.sqlite_version,
            "dialect": self.dialect,
            "capabilities": self.capabilities.to_dict(),
            "fallbacks": list(self.fallbacks),
            "blocked_features": list(self.blocked),
        }
