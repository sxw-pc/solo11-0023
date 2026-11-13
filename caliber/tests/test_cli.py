from __future__ import annotations

import json
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


def test_cli_baseline_flags_and_switch_off(
    tmp_path: Path, fixture_tree: Path, capsys
):
    db_path = tmp_path / "cli-baseline.db"
    prefix = base_args(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()

    # 首次 run：无基线，降级提示出现在输出中
    code = main(
        prefix + ["run", "--metrics", "active_users",
                  "--datasets", str(fixture_tree / "datasets")]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "基线对照：" in output
    assert "降级" in output

    # 第二次 run：自动锚定上一个同集合 run
    code = main(
        prefix + ["run", "--metrics", "active_users",
                  "--datasets", str(fixture_tree / "datasets")]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "基线 run #1（auto）" in output
    assert "可比指标 1 个" in output

    # 显式基线 id 锚定
    code = main(
        prefix + ["run", "--metrics", "active_users",
                  "--baseline-run-id", "1",
                  "--datasets", str(fixture_tree / "datasets")]
    )
    assert code == 0
    assert "基线 run #1（explicit）" in capsys.readouterr().out

    # 关闭对照：输出与 summary 中完全没有基线痕迹
    code = main(
        prefix + ["run", "--metrics", "active_users",
                  "--no-baseline-compare",
                  "--datasets", str(fixture_tree / "datasets")]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "基线对照" not in output

    connection = connect(db_path)
    try:
        disabled_row = connection.execute(
            "SELECT summary_json FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        summary = json.loads(disabled_row[0])
        assert "baseline_comparison" not in summary
    finally:
        connection.close()

