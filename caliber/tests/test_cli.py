from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def _seed_cli_run(db_path: Path, run_id: int, days_old: int) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat(
        timespec="seconds"
    )
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO runs(id,status,started_at,finished_at,metrics_json,"
        "datasets_dir,tolerances_json) VALUES (?,?,?,?,?,?,?)",
        (run_id, "completed", old, old, '["m"]', "/tmp", "{}"),
    )
    conn.execute(
        "INSERT INTO snapshots(run_id,metric_key,metric_version,datasets_json,"
        "impl_ids_json,verdict,summary_json,result_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "m", 1, "[]", "[]", "consistent", "{}", "{}", old),
    )
    conn.execute(
        "INSERT INTO audit_log(created_at,action,entity,status,detail_json) "
        "VALUES (?,?,?,?,?)",
        (old, "run.completed", f"run:{run_id}", "ok", "{}"),
    )
    conn.commit()
    conn.close()


def test_cli_prune_dry_run_deletes_nothing(
    tmp_path: Path, fixture_tree: Path, capsys
):
    db_path = tmp_path / "cli-prune.db"
    prefix = base_args(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()
    _seed_cli_run(db_path, 1, 120)
    _seed_cli_run(db_path, 2, 5)

    code = main(prefix + ["prune", "--retention-days", "90", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "符合条件的运行：1 条" in out

    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2
    finally:
        conn.close()


def test_cli_prune_requires_yes_when_not_a_tty(
    tmp_path: Path, fixture_tree: Path, capsys
):
    db_path = tmp_path / "cli-prune-confirm.db"
    prefix = base_args(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()
    _seed_cli_run(db_path, 1, 120)

    # pytest captures stdin, so it is not a TTY: without --yes this must fail
    # safely rather than hang on input().
    code = main(prefix + ["prune", "--retention-days", "90"])
    assert code == 1
    assert "--yes" in capsys.readouterr().err

    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        conn.close()


def test_cli_prune_executes_with_yes_and_audits(
    tmp_path: Path, fixture_tree: Path, capsys
):
    db_path = tmp_path / "cli-prune-go.db"
    prefix = base_args(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()
    _seed_cli_run(db_path, 1, 120)
    _seed_cli_run(db_path, 2, 5)

    code = main(
        prefix
        + ["prune", "--retention-days", "90", "--batch-size", "1", "--yes"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "裁剪完成" in out

    conn = connect(db_path)
    try:
        remaining = [row[0] for row in conn.execute("SELECT id FROM runs")]
        assert remaining == [2]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM snapshots s LEFT JOIN runs r ON r.id=s.run_id "
            "WHERE r.id IS NULL"
        ).fetchone()[0]
        assert orphans == 0
        audit = conn.execute(
            "SELECT action, status FROM audit_log WHERE action='retention.prune'"
        ).fetchall()
        assert len(audit) == 1 and audit[0]["status"] == "ok"
    finally:
        conn.close()


def test_cli_prune_without_window_is_an_error(
    tmp_path: Path, fixture_tree: Path, capsys
):
    db_path = tmp_path / "cli-prune-nowindow.db"
    prefix = base_args(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()
    code = main(prefix + ["prune", "--dry-run"])
    assert code == 1
    assert "保留窗口" in capsys.readouterr().err


def test_cli_prune_rejects_invalid_window(
    tmp_path: Path, fixture_tree: Path, capsys
):
    db_path = tmp_path / "cli-prune-bad.db"
    prefix = base_args(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()
    code = main(prefix + ["prune", "--retention-days", "0", "--dry-run"])
    assert code == 1
    assert "保留窗口" in capsys.readouterr().err

