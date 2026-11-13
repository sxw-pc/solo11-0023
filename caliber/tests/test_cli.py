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


def test_cli_rollback_undo_and_trace(tmp_path: Path, fixture_tree: Path, capsys):
    db_path = tmp_path / "cli-rollback.db"
    prefix = base_args(db_path, fixture_tree)
    metrics_dir = fixture_tree / "metrics"
    assert main(prefix + ["import-metrics", "--dir", str(metrics_dir)]) == 0
    # 再导入一次制造 v2
    assert main(prefix + ["import-metrics", "--dir", str(metrics_dir)]) == 0
    assert main(
        prefix
        + ["run", "--metrics", "active_users",
           "--datasets", str(fixture_tree / "datasets")]
    ) == 0
    capsys.readouterr()

    code = main(
        prefix
        + ["rollback", "--metric", "active_users", "--to-version", "1",
           "--reason", "cli 验收"]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "已回退 active_users: v2 → v1" in output
    assert "生成新版本 v3" in output

    connection = connect(db_path)
    try:
        assert connection.execute(
            "SELECT current_version FROM metrics WHERE key = 'active_users'"
        ).fetchone()[0] == 3
    finally:
        connection.close()

    code = main(prefix + ["rollback-undo", "--metric", "active_users"])
    assert code == 0
    assert "已撤销回退 active_users" in capsys.readouterr().out

    connection = connect(db_path)
    try:
        assert connection.execute(
            "SELECT current_version FROM metrics WHERE key = 'active_users'"
        ).fetchone()[0] == 4
        snapshot_id = connection.execute("SELECT MAX(id) FROM snapshots").fetchone()[0]
    finally:
        connection.close()

    code = main(prefix + ["trace", "--snapshot-id", str(snapshot_id)])
    assert code == 0
    output = capsys.readouterr().out
    assert '"definition_source": "snapshot_frozen"' in output
    assert '"degraded": false' in output

    # 回退到不存在的版本 → 非零退出
    code = main(
        prefix + ["rollback", "--metric", "active_users", "--to-version", "99"]
    )
    assert code == 1
    assert "指标版本不存在" in capsys.readouterr().err

