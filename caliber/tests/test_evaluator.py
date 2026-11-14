from __future__ import annotations

from pathlib import Path

import pytest

from app.core.dataset_loader import DatasetCatalog
from app.core.dsl_parser import parse_metric_file
from app.core.evaluator import (
    evaluate_metric_impls,
    evaluate_sql,
    run_metric,
    truncate_dimension,
    validate_readonly_sql,
)
from app.errors import EvaluationError, EvaluationTimeout, UnsafeSQLError


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO users_a VALUES (1, 'x', 'y', 'z')",
        "UPDATE users_a SET status = 'x'",
        "DELETE FROM users_a",
        "DROP TABLE users_a",
        "ATTACH DATABASE 'other.db' AS other",
        "PRAGMA table_info(users_a)",
        "VACUUM",
        "SELECT 1; SELECT 2",
        "WITH x AS (SELECT 1) DELETE FROM users_a",
        "nothing",
    ],
)
def test_unsafe_sql_is_rejected(sql: str):
    with pytest.raises(UnsafeSQLError):
        validate_readonly_sql(sql)


def test_safe_sql_and_literals_are_accepted():
    assert validate_readonly_sql("SELECT 1;")
    assert validate_readonly_sql("-- DELETE is just a comment\nSELECT 1")
    assert validate_readonly_sql("SELECT 'DROP TABLE x' AS note")
    assert validate_readonly_sql("WITH x AS (SELECT 1 AS n) SELECT n FROM x")


def test_select_evaluation_and_single_column_normalization(catalog: DatasetCatalog):
    conn = catalog.readonly_connection()
    values, rows, _ = evaluate_sql(
        conn,
        "SELECT last_active_date, COUNT(*) FROM users_a "
        "WHERE status = 'active' GROUP BY last_active_date",
        grain="day",
    )
    assert values == {"2026-02-01": 2.0, "2026-02-02": 2.0, "2026-02-03": 2.0}
    assert rows == 3
    aggregate, count, _ = evaluate_sql(conn, "SELECT COUNT(*) FROM users_a")
    assert aggregate == {"__all__": 7.0}
    assert count == 1


def test_row_limit_is_enforced(catalog: DatasetCatalog):
    with pytest.raises(EvaluationError) as caught:
        evaluate_sql(
            catalog.readonly_connection(),
            "SELECT channel, COUNT(*) FROM orders_a GROUP BY channel",
            max_rows=1,
        )
    assert caught.value.code == "row_limit_exceeded"


def test_timeout_aborts_long_query(catalog: DatasetCatalog):
    sql = (
        "WITH RECURSIVE cnt(x) AS (VALUES(1) UNION ALL "
        "SELECT x + 1 FROM cnt WHERE x < 100000000) SELECT 'all', COUNT(*) FROM cnt"
    )
    with pytest.raises(EvaluationTimeout):
        evaluate_sql(
            catalog.readonly_connection(),
            sql,
            timeout_seconds=0.05,
        )


def test_non_numeric_and_bad_arity_are_rejected(catalog: DatasetCatalog):
    conn = catalog.readonly_connection()
    with pytest.raises(EvaluationError):
        evaluate_sql(conn, "SELECT 'text' AS value")
    with pytest.raises(EvaluationError):
        evaluate_sql(conn, "SELECT 1 AS a, 2 AS b, 3 AS c")


def test_duplicate_dimension_is_rejected(catalog: DatasetCatalog):
    with pytest.raises(EvaluationError):
        evaluate_sql(
            catalog.readonly_connection(),
            "SELECT 'same', order_id FROM orders_a",
        )


@pytest.mark.parametrize(
    ("raw", "grain", "expected"),
    [
        ("2026-02-03", "day", "2026-02-03"),
        ("2026-02-03", "week", "2026-W06"),
        ("2026-02-03T10:20:30", "month", "2026-02"),
        ("web", "day", "web"),
        (None, "day", "__null__"),
    ],
)
def test_grain_truncation(raw, grain: str, expected: str):
    assert truncate_dimension(raw, grain) == expected


def test_run_metric_on_fixtures(
    catalog: DatasetCatalog, fixture_tree: Path, sql_roots: tuple[Path, ...]
):
    metric = parse_metric_file(fixture_tree / "metrics" / "active_users.yaml")
    evaluations = evaluate_metric_impls(metric, 1, catalog, sql_roots)
    assert set(evaluations) == {"impl_a", "impl_b"}
    assert evaluations["impl_a"].values == evaluations["impl_b"].values
    assert evaluations["impl_a"].duration_ms >= 0
    comparison, _, attribution = run_metric(metric, 1, catalog, sql_roots)
    assert comparison.verdict == "consistent"
    assert attribution["status"] == "skipped"
    assert attribution["degraded_reasons"][0]["code"] == "natural_key_not_declared"

