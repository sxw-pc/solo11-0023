from __future__ import annotations

from pathlib import Path

from cli.main import main
from app.db import connect


def base_args(db_path: Path, fixture_tree: Path) -> list[str]:
    return [
        "--db", str(db_path),
        "--fixtures-dir", str(fixture_tree),
    ]


def test_import_run_and_report_end_to_end(
    tmp_path: Path, fixture_tree: Path, capsys
):
    db_path = tmp_path / "cli.db"
    prefix = base_args(db_path, fixture_tree)

    code = main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")])
    assert code == 0
    output = capsys.readouterr().out
    assert "导入 active_users v1" in output
    assert "成功 3" in output

    connection = connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM metric_versions").fetchone()[0] == 3
    finally:
        connection.close()

    code = main(
        prefix
        + [
            "run",
            "--metrics", "active_users,conversion_rate,avg_order_value",
            "--datasets", str(fixture_tree / "datasets"),
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "active_users" in output
    assert "inconsistent" in output

    connection = connect(db_path)
    try:
        verdicts = {
            row[0] for row in connection.execute("SELECT verdict FROM snapshots")
        }
        assert verdicts == {"consistent", "inconsistent"}
    finally:
        connection.close()

    code = main(prefix + ["report", "--last", "--format", "md"])
    assert code == 0
    output = capsys.readouterr().out
    assert "# 指标准星一致性报告" in output
    assert "avg_order_value" in output

    output_path = tmp_path / "report.json"
    code = main(
        prefix
        + ["report", "--last", "--format", "json", "--output", str(output_path)]
    )
    assert code == 0
    assert output_path.is_file()
    assert '"report_version": "1.0"' in output_path.read_text(encoding="utf-8")


def test_cli_run_unknown_metric_returns_nonzero(
    tmp_path: Path, fixture_tree: Path, capsys
):
    db_path = tmp_path / "cli-unknown.db"
    prefix = base_args(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()
    code = main(
        prefix
        + [
            "run",
            "--metrics", "missing_metric",
            "--datasets", str(fixture_tree / "datasets"),
        ]
    )
    assert code == 1
    assert "指标不存在" in capsys.readouterr().err


def test_cli_concurrent_run_reports_per_rule_status(
    tmp_path: Path, fixture_tree: Path, capsys
):
    db_path = tmp_path / "cli-concurrent.db"
    prefix = base_args(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()
    code = main(
        prefix
        + [
            "run",
            "--metrics", "active_users,conversion_rate,avg_order_value",
            "--datasets", str(fixture_tree / "datasets"),
            "--max-workers", "2",
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "active_users: success" in output
    assert "avg_order_value: success" in output
    assert '"max_workers": 2' in output
    assert "peak_workers" in output

    connection = connect(db_path)
    try:
        items = connection.execute(
            "SELECT metric_key, status FROM run_items ORDER BY position"
        ).fetchall()
        assert [row[0] for row in items] == [
            "active_users",
            "conversion_rate",
            "avg_order_value",
        ]
        assert all(row[1] == "success" for row in items)
    finally:
        connection.close()


def test_cli_resume_only_reruns_failed_rule(
    tmp_path: Path, fixture_tree: Path, capsys, monkeypatch
):
    from app.core import snapshot as snapshot_module
    from app.errors import EvaluationError

    db_path = tmp_path / "cli-resume.db"
    prefix = base_args(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0

    real_run_metric = snapshot_module.run_metric

    def flaky(metric, version, catalog, roots, **kwargs):
        if metric.key == "conversion_rate" and not flaky.failed:
            flaky.failed = True
            raise EvaluationError("CLI 首轮失败")
        return real_run_metric(metric, version, catalog, roots, **kwargs)

    flaky.failed = False
    monkeypatch.setattr(snapshot_module, "run_metric", flaky)

    first_code = main(
        prefix
        + [
            "run",
            "--metrics", "active_users,conversion_rate",
            "--datasets", str(fixture_tree / "datasets"),
            "--max-workers", "2",
        ]
    )
    assert first_code == 0
    first_out = capsys.readouterr()
    assert "conversion_rate: failed" in first_out.out
    assert "存在失败规则" in first_out.err

    # Second pass with --resume: only the failed rule is recomputed.
    second_code = main(
        prefix
        + [
            "run",
            "--metrics", "active_users,conversion_rate",
            "--datasets", str(fixture_tree / "datasets"),
            "--max-workers", "2",
            "--resume",
        ]
    )
    assert second_code == 0
    second_out = capsys.readouterr().out
    assert "active_users: skipped" in second_out
    assert "conversion_rate: success" in second_out

    connection = connect(db_path)
    try:
        rows = connection.execute(
            "SELECT run_id, metric_key, status FROM run_items ORDER BY run_id, position"
        ).fetchall()
        second_run = max(row[0] for row in rows)
        second_rows = [row for row in rows if row[0] == second_run]
        assert {row[1]: row[2] for row in second_rows} == {
            "active_users": "skipped",
            "conversion_rate": "success",
        }
    finally:
        connection.close()


def test_cli_rejects_non_positive_max_workers(
    tmp_path: Path, fixture_tree: Path, capsys
):
    db_path = tmp_path / "cli-bad-workers.db"
    prefix = base_args(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    code = main(
        prefix
        + [
            "run",
            "--metrics", "active_users",
            "--datasets", str(fixture_tree / "datasets"),
            "--max-workers", "0",
        ]
    )
    assert code == 2
    assert "max_workers" in capsys.readouterr().err

