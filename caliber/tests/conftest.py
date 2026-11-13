"""Shared pytest fixtures: temporary databases, fixture copies and Flask client."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

import pytest

from app import create_app
from app.config import Config
from app.core.dataset_loader import DatasetCatalog
from app.core.registry import MetricRegistry
from app.core.snapshot import perform_run
from app.db import init_db

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "caliber.sqlite3"


@pytest.fixture
def conn(db_path: Path):
    connection = init_db(db_path)
    yield connection
    connection.close()


@pytest.fixture
def fixture_tree(tmp_path: Path) -> Path:
    target = tmp_path / "fixtures"
    shutil.copytree(PROJECT_ROOT / "fixtures", target)
    return target


@pytest.fixture
def sql_roots(fixture_tree: Path) -> tuple[Path, ...]:
    return (fixture_tree / "impls",)


@pytest.fixture
def app_config(tmp_path: Path, fixture_tree: Path) -> Config:
    data_dir = tmp_path / "app-data"
    return Config(
        root_dir=PROJECT_ROOT,
        data_dir=data_dir,
        db_path=data_dir / "caliber.sqlite3",
        fixtures_dir=fixture_tree,
        datasets_dir=fixture_tree / "datasets",
        metrics_dir=fixture_tree / "metrics",
        sql_roots=(fixture_tree / "impls",),
        eval_timeout_seconds=2.0,
        max_result_rows=100,
        testing=True,
    )


@pytest.fixture
def app(app_config: Config):
    return create_app(app_config)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def imported_client(app, app_config: Config):
    connection = init_db(app_config.db_path)
    try:
        MetricRegistry(connection, app_config.sql_roots).import_directory(
            app_config.metrics_dir
        )
    finally:
        connection.close()
    return app.test_client()


@pytest.fixture
def registry_conn(conn, fixture_tree: Path, sql_roots: tuple[Path, ...]):
    MetricRegistry(conn, sql_roots).import_directory(fixture_tree / "metrics")
    return conn


@pytest.fixture
def catalog(fixture_tree: Path):
    instance = DatasetCatalog(fixture_tree / "datasets")
    yield instance
    instance.close()


@pytest.fixture
def execute_fixture_run(
    registry_conn,
    catalog: DatasetCatalog,
    sql_roots: tuple[Path, ...],
    fixture_tree: Path,
) -> Callable:
    def _execute(metric_keys: list[str] | None = None) -> int:
        return perform_run(
            registry_conn,
            metric_keys,
            catalog,
            sql_roots,
            datasets_dir=fixture_tree / "datasets",
            timeout_seconds=2.0,
            max_rows=100,
            abs_tolerance=1e-6,
            rel_tolerance=0.0,
        )

    return _execute

