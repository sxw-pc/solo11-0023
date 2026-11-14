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


def _write_tolerance_tree(root: Path) -> Path:
    (root / "datasets").mkdir(parents=True)
    (root / "impls").mkdir()
    (root / "metrics").mkdir()
    (root / "datasets" / "t_a.csv").write_text("id,v,ratio\n1,100.0,0.001\n", encoding="utf-8")
    (root / "datasets" / "t_b.csv").write_text("id,v,ratio\n1,100.0004,0.002\n", encoding="utf-8")
    specs = {
        "amount_metric": ("v", "amount"),
        "ratio_metric": ("ratio", "ratio"),
    }
    for metric, (column, prefix) in specs.items():
        (root / "metrics" / f"{metric}.yaml").write_text(
            f"key: {metric}\nname: {metric}\nowner: qa\ndescription: d\nunit: x\ngrain: day\n"
            "impls:\n"
            f"  - id: impl_a\n    source: t_a\n    sql_file: {prefix}_a.sql\n"
            f"  - id: impl_b\n    source: t_b\n    sql_file: {prefix}_b.sql\n",
            encoding="utf-8",
        )
        (root / "impls" / f"{prefix}_a.sql").write_text(
            f"SELECT '__all__' AS day, {column} FROM t_a\n", encoding="utf-8"
        )
        (root / "impls" / f"{prefix}_b.sql").write_text(
            f"SELECT '__all__' AS day, {column} FROM t_b\n", encoding="utf-8"
        )
    return root


def test_tolerance_values_flow_into_snapshot_and_verdict(tmp_path: Path, capsys):
    tree = _write_tolerance_tree(tmp_path / "fx")
    db_path = tmp_path / "tol.db"
    prefix = ["--db", str(db_path), "--fixtures-dir", str(tree)]
    assert main(prefix + ["import-metrics", "--dir", str(tree / "metrics")]) == 0
    capsys.readouterr()

    # 金额差 0.0004，绝对容差 0.001：通过，且快照记录本次生效的绝对容差
    assert main(prefix + ["run", "--metrics", "amount_metric", "--abs-tol", "0.001"]) == 0
    assert main(prefix + ["report", "--last", "--format", "json",
                          "--output", str(tmp_path / "abs.json")]) == 0
    abs_report = json.loads((tmp_path / "abs.json").read_text(encoding="utf-8"))
    assert abs_report["run"]["tolerances"] == {"abs": 0.001, "rel": 0.0}
    assert abs_report["metrics"][0]["verdict"] == "consistent"
    assert abs_report["metrics"][0]["tolerances"] == {"abs": 0.001, "rel": 0.0}

    # 小基数比率差 100%，仅给相对容差 0.5%：不通过，快照记录相对容差
    assert main(prefix + ["run", "--metrics", "ratio_metric", "--rel-tol", "0.005"]) == 0
    assert main(prefix + ["report", "--last", "--format", "json",
                          "--output", str(tmp_path / "rel.json")]) == 0
    rel_report = json.loads((tmp_path / "rel.json").read_text(encoding="utf-8"))
    assert rel_report["run"]["tolerances"]["rel"] == 0.005
    assert rel_report["metrics"][0]["verdict"] == "inconsistent"
    assert rel_report["metrics"][0]["tolerances"]["rel"] == 0.005


def test_last_and_run_id_exports_match_across_formats(tmp_path: Path, fixture_tree: Path, capsys):
    db_path = tmp_path / "match.db"
    prefix = base_args(db_path, fixture_tree)
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()
    assert main(prefix + ["run", "--metrics", "active_users,conversion_rate"]) == 0
    capsys.readouterr()

    outputs = {}
    for selector in ("--last", "--run-id",):
        args = prefix + ["report"]
        args += [selector, "1"] if selector == "--run-id" else [selector]
        for fmt in ("json", "md", "html"):
            target = tmp_path / f"{selector.strip('-')}_{fmt}.out"
            assert main(args + ["--format", fmt, "--output", str(target)]) == 0
            outputs[(selector, fmt)] = target.read_text(encoding="utf-8")

    last_json = json.loads(outputs[("--last", "json")])
    rid_json = json.loads(outputs[("--run-id", "json")])
    assert last_json["run"] == rid_json["run"]
    assert last_json["summary"] == rid_json["summary"]
    assert last_json["metrics"] == rid_json["metrics"]
    assert last_json["drift_events"] == rid_json["drift_events"]
    assert last_json["audit"] == rid_json["audit"]
    assert outputs[("--last", "md")] == outputs[("--run-id", "md")]
    assert outputs[("--last", "html")] == outputs[("--run-id", "html")]


def test_broken_dataset_is_skipped_without_aborting_run(tmp_path: Path, capsys):
    tree = _write_tolerance_tree(tmp_path / "fx2")
    (tree / "datasets" / "broken.csv").write_text("a,b,c\n1,2\n", encoding="utf-8")
    db_path = tmp_path / "broken.db"
    prefix = ["--db", str(db_path), "--fixtures-dir", str(tree)]
    assert main(prefix + ["import-metrics", "--dir", str(tree / "metrics")]) == 0
    capsys.readouterr()
    code = main(prefix + ["run", "--metrics", "amount_metric", "--abs-tol", "0.001"])
    assert code == 0
    err = capsys.readouterr().err
    assert "broken" in err and "加载失败" in err


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

