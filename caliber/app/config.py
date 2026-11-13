"""Runtime configuration.

All paths are local.  The HTTP host is deliberately forced to a loopback
address; tests may change the port, but never expose the service publicly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(slots=True)
class Config:
    root_dir: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "instance"
    db_path: Path = PROJECT_ROOT / "instance" / "caliber.sqlite3"
    fixtures_dir: Path = PROJECT_ROOT / "fixtures"
    datasets_dir: Path = PROJECT_ROOT / "fixtures" / "datasets"
    metrics_dir: Path = PROJECT_ROOT / "fixtures" / "metrics"
    sql_roots: tuple[Path, ...] = (PROJECT_ROOT / "fixtures" / "impls",)
    host: str = "127.0.0.1"
    port: int = 5057
    eval_timeout_seconds: float = 5.0
    max_result_rows: int = 10_000
    abs_tolerance: float = 1e-6
    rel_tolerance: float = 0.0
    retention_days: int | None = None
    prune_batch_size: int = 200
    testing: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        """Build a config from a small, documented set of local env vars."""
        data_dir = Path(os.environ.get("CALIBER_DATA_DIR", PROJECT_ROOT / "instance"))
        fixtures = Path(os.environ.get("CALIBER_FIXTURES_DIR", PROJECT_ROOT / "fixtures"))
        host = os.environ.get("CALIBER_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("CALIBER_HOST 只允许回环地址")
        retention_raw = os.environ.get("CALIBER_RETENTION_DAYS", "").strip()
        retention_days: int | None = None
        if retention_raw:
            try:
                retention_days = int(retention_raw)
            except ValueError as exc:
                raise ValueError("CALIBER_RETENTION_DAYS 必须是正整数") from exc
            if retention_days < 1:
                raise ValueError(
                    "CALIBER_RETENTION_DAYS 必须是正整数；不裁剪请取消设置"
                )
        batch_raw = os.environ.get("CALIBER_PRUNE_BATCH_SIZE", "200")
        try:
            prune_batch_size = int(batch_raw)
        except ValueError as exc:
            raise ValueError("CALIBER_PRUNE_BATCH_SIZE 必须是正整数") from exc
        if prune_batch_size < 1:
            raise ValueError("CALIBER_PRUNE_BATCH_SIZE 必须是正整数")
        return cls(
            data_dir=data_dir,
            db_path=Path(os.environ.get("CALIBER_DB", data_dir / "caliber.sqlite3")),
            fixtures_dir=fixtures,
            datasets_dir=fixtures / "datasets",
            metrics_dir=fixtures / "metrics",
            sql_roots=(fixtures / "impls",),
            host=host,
            port=int(os.environ.get("CALIBER_PORT", "5057")),
            retention_days=retention_days,
            prune_batch_size=prune_batch_size,
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def merged_sql_roots(self, *additional: Path | str) -> tuple[Path, ...]:
        roots: list[Path] = []
        for raw in (*self.sql_roots, *additional):
            path = Path(raw).resolve()
            if path not in roots:
                roots.append(path)
        return tuple(roots)

