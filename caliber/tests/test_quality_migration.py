"""dqkit v1 → v2 迁移验收测试。

覆盖四条验收主线：

1. 现有 fixture 的旧格式规则在新版本（兼容模式）下执行，结果与 v1 基线
   完全一致，且执行元信息携带语义差异标记字段；
2. 手工构造触发语义差异的规则（阈值压线 / 空值），差异被显式记录且可
   通过快照、报告、审计追溯；
3. 关闭兼容层（v2 模式）执行仍然正常，证明适配层可插拔；legacy 模式
   作为回退路径同样复现基线；
4. 版本不匹配（引擎 API、规则版本钉、未知格式/模式）给出可读错误，
   而不是崩栈。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

import dqkit
from app.audit import list_audit
from app.core.dataset_loader import DatasetCatalog
from app.core.dsl_parser import parse_metric_text
from app.core.quality import MODES, get_semantics
from app.core.registry import MetricRegistry
from app.core.reporter import build_report
from app.core.snapshot import (
    create_run,
    get_run,
    get_snapshot,
    list_runs,
    perform_run,
    snapshots_for_run,
)
from app.core.versioning import get_latest
from app.db import init_db
from app.errors import (
    CaliberError,
    DslParseError,
    EvaluationError,
    QualityVersionMismatch,
)
from cli.main import main as cli_main

BASELINE_PATH = Path(__file__).parent / "baselines" / "quality_v1_baseline.json"

BOUNDARY_YAML = """
key: boundary_probe
name: 边界探针
owner: qa-migration
description: 手工构造：两实现差值恰好等于绝对容差边界。
unit: 元
grain: day
impls:
  - id: impl_a
    source: probe_a
    sql_file: probe_a.sql
  - id: impl_b
    source: probe_b
    sql_file: probe_b.sql
"""

NULL_YAML = """
key: null_probe
name: 空值探针
owner: qa-migration
description: 手工构造：impl_a 的指标值为 NULL。
unit: 元
grain: day
impls:
  - id: impl_a
    source: null_a
    sql_file: null_a.sql
  - id: impl_b
    source: null_b
    sql_file: null_b.sql
"""

V2_YAML = """
schema: 2
key: v2_probe
name: 新格式探针
owner: qa-migration
description: schema 2 参数形态（dataset/query + quality 版本钉）。
unit: 元
grain: day
quality:
  engine: dqkit
  api: 2
impls:
  - id: impl_a
    dataset: probe_a
    query: probe_a.sql
  - id: impl_b
    dataset: probe_b
    query: probe_b.sql
"""

V2_PIN_MISMATCH_YAML = """
schema: 2
key: v2_probe
name: 新格式探针
owner: qa-migration
description: 钉住了一个不存在的 dqkit API 版本。
unit: 元
grain: day
quality:
  engine: dqkit
  api: 99
impls:
  - id: impl_a
    dataset: probe_a
    query: probe_a.sql
  - id: impl_b
    dataset: probe_b
    query: probe_b.sql
"""


@pytest.fixture(scope="module")
def v1_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _craft_project(
    tmp_path: Path,
    *,
    metric_yaml: str,
    datasets: dict[str, str],
    sql: dict[str, str],
):
    """在临时目录构造一套最小可运行的指标工程（不动 fixtures/）。"""
    datasets_dir = tmp_path / "datasets"
    impls_dir = tmp_path / "impls"
    metrics_dir = tmp_path / "metrics"
    for directory in (datasets_dir, impls_dir, metrics_dir):
        directory.mkdir(parents=True, exist_ok=True)
    for name, content in datasets.items():
        (datasets_dir / name).write_text(content, encoding="utf-8")
    for name, content in sql.items():
        (impls_dir / name).write_text(content, encoding="utf-8")
    (metrics_dir / "probe.yaml").write_text(metric_yaml, encoding="utf-8")
    conn = init_db(tmp_path / "crafted.sqlite3")
    MetricRegistry(conn, (impls_dir,)).import_directory(metrics_dir)
    catalog = DatasetCatalog(datasets_dir)
    return conn, catalog, impls_dir, datasets_dir


def _boundary_project(tmp_path: Path):
    return _craft_project(
        tmp_path,
        metric_yaml=BOUNDARY_YAML,
        datasets={
            "probe_a.csv": "day,amount\n2026-02-01,100.0\n",
            "probe_b.csv": "day,amount\n2026-02-01,100.5\n",
        },
        sql={
            "probe_a.sql": "SELECT day, amount FROM probe_a",
            "probe_b.sql": "SELECT day, amount FROM probe_b",
        },
    )


def _null_project(tmp_path: Path):
    return _craft_project(
        tmp_path,
        metric_yaml=NULL_YAML,
        datasets={
            "null_a.csv": "day,amount\n2026-02-01,\n",
            "null_b.csv": "day,amount\n2026-02-01,100.5\n",
        },
        sql={
            "null_a.sql": "SELECT day, amount FROM null_a",
            "null_b.sql": "SELECT day, amount FROM null_b",
        },
    )


def _assert_run_matches_baseline(
    conn, run_id: int, baseline: dict, *, mode: str, engine_api: int
) -> None:
    snapshots = snapshots_for_run(conn, run_id)
    assert len(snapshots) == len(baseline["metrics"])
    by_key = {item["metric_key"]: item for item in snapshots}
    for expected in baseline["metrics"]:
        actual = by_key[expected["metric_key"]]
        result = actual["result"]
        assert actual["verdict"] == expected["verdict"]
        actual_summary = dict(actual["summary"])
        if mode == "v2":
            # v2 原生模式合法地多出一个 null_values 统计键
            assert actual_summary.pop("null_values") == 0
        assert actual_summary == expected["summary"]
        assert result["dimensions"] == expected["dimensions"]
        actual_diffs = result["diffs"]
        if mode == "v2":
            # v2 原生模式的 detail 文案带有引擎标记，比较时只核对结构字段
            actual_diffs = [
                {key: value for key, value in diff.items() if key != "detail"}
                for diff in actual_diffs
            ]
            expected_diffs = [
                {key: value for key, value in diff.items() if key != "detail"}
                for diff in expected["diffs"]
            ]
        else:
            expected_diffs = expected["diffs"]
        assert actual_diffs == expected_diffs
        assert result["abs_tolerance"] == expected["abs_tolerance"]
        assert result["rel_tolerance"] == expected["rel_tolerance"]
        impl_values = {
            item["impl_id"]: item["values"] for item in result["implementations"]
        }
        assert impl_values == expected["impl_values"]
        meta = result["execution_meta"]
        assert meta["semantics_mode"] == mode
        assert meta["engine"]["api"] == engine_api
        assert meta["semantic_differences"] == []
        assert set(meta["semantics_applied"]) == {
            "null_handling",
            "threshold_boundary",
        }


# ---------------------------------------------------------------------------
# 验收 1：旧格式 fixture 在新版本下复现 v1 基线，元信息可见
# ---------------------------------------------------------------------------


def test_compat_mode_reproduces_v1_baseline(
    registry_conn, catalog, sql_roots, fixture_tree, v1_baseline
):
    run_id = perform_run(
        registry_conn,
        None,
        catalog,
        sql_roots,
        datasets_dir=fixture_tree / "datasets",
        abs_tolerance=1e-6,
        rel_tolerance=0.0,
        quality_mode="compat",
    )
    _assert_run_matches_baseline(
        registry_conn, run_id, v1_baseline, mode="compat", engine_api=2
    )
    run = get_run(registry_conn, run_id)
    assert run["summary"]["quality"]["mode"] == "compat"
    assert run["summary"]["quality"]["engine"]["version"] == dqkit.__version__
    assert run["summary"]["quality"]["semantic_differences"] == 0


def test_legacy_mode_reproduces_v1_baseline(
    registry_conn, catalog, sql_roots, fixture_tree, v1_baseline
):
    """回退路径：冻结的 v1 引擎同样复现基线。"""
    run_id = perform_run(
        registry_conn,
        None,
        catalog,
        sql_roots,
        datasets_dir=fixture_tree / "datasets",
        abs_tolerance=1e-6,
        rel_tolerance=0.0,
        quality_mode="legacy",
    )
    _assert_run_matches_baseline(
        registry_conn, run_id, v1_baseline, mode="legacy", engine_api=1
    )


# ---------------------------------------------------------------------------
# 验收 3：关闭兼容层（v2 纯新语义）执行仍然正常 —— 适配层可插拔
# ---------------------------------------------------------------------------


def test_v2_mode_runs_fixtures_without_compat(
    registry_conn, catalog, sql_roots, fixture_tree, v1_baseline
):
    run_id = perform_run(
        registry_conn,
        None,
        catalog,
        sql_roots,
        datasets_dir=fixture_tree / "datasets",
        abs_tolerance=1e-6,
        rel_tolerance=0.0,
        quality_mode="v2",
    )
    # fixture 未触及语义分歧点，v2 原生结论与基线一致
    _assert_run_matches_baseline(
        registry_conn, run_id, v1_baseline, mode="v2", engine_api=2
    )
    run = get_run(registry_conn, run_id)
    assert run["summary"]["quality"]["mode"] == "v2"


# ---------------------------------------------------------------------------
# 验收 2a：阈值压线触发语义差异 —— 被记录且可追溯
# ---------------------------------------------------------------------------


def test_threshold_boundary_difference_is_recorded_and_traceable(tmp_path):
    conn, catalog, impls_dir, datasets_dir = _boundary_project(tmp_path)
    try:
        # compat：v2 开区间判不一致 → 适配层按 v1 闭区间调和为一致，并记录
        run_id = perform_run(
            conn,
            ["boundary_probe"],
            catalog,
            (impls_dir,),
            datasets_dir=datasets_dir,
            abs_tolerance=0.5,
            rel_tolerance=0.0,
            quality_mode="compat",
        )
        snapshot = snapshots_for_run(conn, run_id)[0]
        assert snapshot["verdict"] == "consistent"  # 与 v1 基线口径一致
        meta = snapshot["result"]["execution_meta"]
        assert meta["semantics_mode"] == "compat"
        differences = meta["semantic_differences"]
        assert len(differences) == 1
        difference = differences[0]
        assert difference["type"] == "threshold_boundary"
        assert difference["dimension"] == "2026-02-01"
        assert difference["v1_outcome"] == "match"
        assert difference["v2_outcome"] == "mismatch"
        assert difference["resolution"] == "kept_v1"
        assert difference["pairs"] == [
            {"left": "impl_a", "right": "impl_b", "delta": 0.5, "threshold": 0.5}
        ]
        # 运行级汇总可见
        run = get_run(conn, run_id)
        assert run["summary"]["quality"]["semantic_differences"] == 1
        # 报告链路可追溯（JSON 与 Markdown）
        report = build_report(conn, run_id)
        report_meta = report["metrics"][0]["execution_meta"]
        assert report_meta["semantic_differences"][0]["type"] == "threshold_boundary"
        from app.core.reporter import export_report

        _, markdown = export_report(report, "md")
        assert "语义差异" in markdown

        # 同一条规则在 v2 原生语义下判定不同 —— 差异是真实存在的
        run_v2 = perform_run(
            conn,
            ["boundary_probe"],
            catalog,
            (impls_dir,),
            datasets_dir=datasets_dir,
            abs_tolerance=0.5,
            rel_tolerance=0.0,
            quality_mode="v2",
        )
        snapshot_v2 = snapshots_for_run(conn, run_v2)[0]
        assert snapshot_v2["verdict"] == "inconsistent"
        assert snapshot_v2["result"]["execution_meta"]["semantic_differences"] == []

        # legacy 回退：一致，且不产生差异记录
        run_legacy = perform_run(
            conn,
            ["boundary_probe"],
            catalog,
            (impls_dir,),
            datasets_dir=datasets_dir,
            abs_tolerance=0.5,
            rel_tolerance=0.0,
            quality_mode="legacy",
        )
        snapshot_legacy = snapshots_for_run(conn, run_legacy)[0]
        assert snapshot_legacy["verdict"] == "consistent"
        assert snapshot_legacy["result"]["execution_meta"]["engine"]["api"] == 1
    finally:
        catalog.close()
        conn.close()


# ---------------------------------------------------------------------------
# 验收 2b：空值触发语义差异 —— 被记录且可追溯
# ---------------------------------------------------------------------------


def test_null_value_difference_is_recorded(tmp_path):
    conn, catalog, impls_dir, datasets_dir = _null_project(tmp_path)
    try:
        # compat：v1 口径是求值错误；差异记录在错误详情与审计日志中
        with pytest.raises(EvaluationError) as caught:
            perform_run(
                conn,
                ["null_probe"],
                catalog,
                (impls_dir,),
                datasets_dir=datasets_dir,
                quality_mode="compat",
            )
        details = caught.value.details
        assert details["semantics_mode"] == "compat"
        difference = details["semantic_differences"][0]
        assert difference["type"] == "null_value"
        assert difference["impl_id"] == "impl_a"
        assert difference["resolution"] == "kept_v1"
        failed_run = list_runs(conn, 1)[0]
        assert failed_run["status"] == "failed"
        assert "不是数值" in failed_run["error"]
        audit_entries = [
            item for item in list_audit(conn, 20) if item["action"] == "run.failed"
        ]
        assert audit_entries
        audit_detail = json.dumps(audit_entries[0]["detail"], ensure_ascii=False)
        assert "null_value" in audit_detail

        # v2 原生语义：空值被标记为 null_value，运行正常完成，判定 partial
        run_v2 = perform_run(
            conn,
            ["null_probe"],
            catalog,
            (impls_dir,),
            datasets_dir=datasets_dir,
            quality_mode="v2",
        )
        snapshot_v2 = snapshots_for_run(conn, run_v2)[0]
        assert snapshot_v2["verdict"] == "partial"
        kinds = {diff["kind"] for diff in snapshot_v2["result"]["diffs"]}
        assert kinds == {"null_value"}
        assert snapshot_v2["result"]["summary"]["null_values"] == 1
        assert snapshot_v2["result"]["execution_meta"]["semantics_mode"] == "v2"

        # legacy 回退：与 compat 一样按 v1 口径报错
        with pytest.raises(EvaluationError):
            perform_run(
                conn,
                ["null_probe"],
                catalog,
                (impls_dir,),
                datasets_dir=datasets_dir,
                quality_mode="legacy",
            )
    finally:
        catalog.close()
        conn.close()


# ---------------------------------------------------------------------------
# 老产物读取：迁移前的快照在新代码下仍可解析
# ---------------------------------------------------------------------------


def test_pre_migration_snapshot_remains_readable(registry_conn):
    run_id = create_run(
        registry_conn, ["active_users"], "fixtures/datasets", {"abs": 1e-6, "rel": 0.0}
    )
    legacy_result = {
        "metric_key": "active_users",
        "metric_version": 1,
        "impl_ids": ["impl_a", "impl_b"],
        "verdict": "consistent",
        "dimensions": ["2026-02-01"],
        "diffs": [],
        "summary": {
            "impl_count": 2,
            "dimension_count": 1,
            "consistent_dimensions": 1,
            "value_mismatches": 0,
            "missing_dimensions": 0,
            "diff_count": 0,
        },
        "abs_tolerance": 1e-06,
        "rel_tolerance": 0.0,
        "metric": {"key": "active_users", "name": "活跃用户数"},
        "implementations": [],
    }
    cursor = registry_conn.execute(
        "INSERT INTO snapshots(run_id, metric_key, metric_version, datasets_json,"
        " impl_ids_json, verdict, summary_json, result_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            "active_users",
            1,
            json.dumps(["users_a", "users_b"]),
            json.dumps(["impl_a", "impl_b"]),
            "consistent",
            json.dumps(legacy_result["summary"]),
            json.dumps(legacy_result),
            "2026-01-01T00:00:00+00:00",
        ),
    )
    registry_conn.commit()
    snapshot_id = int(cursor.lastrowid)

    decoded = get_snapshot(registry_conn, snapshot_id)
    meta = decoded["result"]["execution_meta"]
    assert meta["semantics_mode"] == "legacy"
    assert meta["engine"]["api"] == 1
    assert meta["semantic_differences"] == []
    assert "回填" in meta["artifact_note"]

    report = build_report(registry_conn, run_id)
    assert report["metrics"][0]["execution_meta"]["semantics_mode"] == "legacy"


# ---------------------------------------------------------------------------
# 版本不匹配：可读错误而非崩栈
# ---------------------------------------------------------------------------


def test_engine_version_mismatch_is_readable(
    monkeypatch, registry_conn, catalog, sql_roots, fixture_tree
):
    monkeypatch.setattr(dqkit, "API_VERSION", 3)
    with pytest.raises(QualityVersionMismatch) as caught:
        get_semantics("compat")
    message = caught.value.message
    assert "版本不匹配" in message
    assert "API v3" in message and "API v2" in message
    assert caught.value.details["installed_api"] == 3

    # 执行器路径同样抛出可读错误，而不是 AttributeError/栈溢出
    with pytest.raises(QualityVersionMismatch):
        perform_run(
            registry_conn,
            None,
            catalog,
            sql_roots,
            datasets_dir=fixture_tree / "datasets",
            quality_mode="compat",
        )
    # 失败前没有产生半截运行记录
    assert list_runs(registry_conn, 1) == []

    # 回退路径不依赖 dqkit 2.x，升级失败时仍可用
    assert get_semantics("legacy").mode == "legacy"


def test_cli_surfaces_version_mismatch(monkeypatch, tmp_path, fixture_tree, capsys):
    monkeypatch.setattr(dqkit, "API_VERSION", 99)
    db_path = tmp_path / "cli-mismatch.db"
    prefix = ["--db", str(db_path), "--fixtures-dir", str(fixture_tree)]
    assert cli_main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()
    code = cli_main(prefix + ["run", "--datasets", str(fixture_tree / "datasets")])
    assert code == 1
    err = capsys.readouterr().err
    assert "quality_version_mismatch" in err
    assert "版本不匹配" in err
    assert "Traceback" not in err


def test_rule_version_pin_mismatch_is_readable(tmp_path):
    datasets_dir = tmp_path / "datasets"
    impls_dir = tmp_path / "impls"
    metrics_dir = tmp_path / "metrics"
    for directory in (datasets_dir, impls_dir, metrics_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (datasets_dir / "probe_a.csv").write_text("day,amount\n2026-02-01,1.0\n", encoding="utf-8")
    (datasets_dir / "probe_b.csv").write_text("day,amount\n2026-02-01,1.0\n", encoding="utf-8")
    (impls_dir / "probe_a.sql").write_text("SELECT day, amount FROM probe_a", encoding="utf-8")
    (impls_dir / "probe_b.sql").write_text("SELECT day, amount FROM probe_b", encoding="utf-8")
    (metrics_dir / "probe.yaml").write_text(V2_PIN_MISMATCH_YAML, encoding="utf-8")
    conn = init_db(tmp_path / "pin.sqlite3")
    try:
        summary = MetricRegistry(conn, (impls_dir,)).import_directory(metrics_dir)
        assert not summary.ok
        error = summary.errors[0]
        assert error["error"] == "quality_version_mismatch"
        assert "API v99" in error["message"]
        assert "dqkit" in error["message"]
    finally:
        conn.close()


def test_unknown_schema_version_is_readable():
    with pytest.raises(DslParseError) as caught:
        parse_metric_text(BOUNDARY_YAML.replace("impls:", "schema: 3\nimpls:", 1))
    assert caught.value.code == "unsupported_schema_version"
    assert "schema=3" in caught.value.message


def test_unknown_quality_mode_rejected(imported_client):
    with pytest.raises(CaliberError) as caught:
        get_semantics("nope")
    assert caught.value.code == "unknown_quality_mode"
    response = imported_client.post(
        "/api/runs", json={"metrics": ["active_users"], "quality_mode": "nope"}
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "unknown_quality_mode"


# ---------------------------------------------------------------------------
# 新格式规则与模式切换
# ---------------------------------------------------------------------------


def test_v2_rule_format_end_to_end(tmp_path):
    conn, catalog, impls_dir, datasets_dir = _craft_project(
        tmp_path,
        metric_yaml=V2_YAML,
        datasets={
            "probe_a.csv": "day,amount\n2026-02-01,100.0\n",
            "probe_b.csv": "day,amount\n2026-02-01,100.1\n",
        },
        sql={
            "probe_a.sql": "SELECT day, amount FROM probe_a",
            "probe_b.sql": "SELECT day, amount FROM probe_b",
        },
    )
    try:
        latest = get_latest(conn, "v2_probe")
        assert latest["definition"]["schema_version"] == 2
        assert latest["definition"]["quality"] == {"engine": "dqkit", "api": 2}
        assert latest["definition"]["impls"][0]["source"] == "probe_a"
        assert latest["definition"]["impls"][0]["sql_file"] == "probe_a.sql"
        for mode in MODES:
            run_id = perform_run(
                conn,
                ["v2_probe"],
                catalog,
                (impls_dir,),
                datasets_dir=datasets_dir,
                abs_tolerance=0.5,
                quality_mode=mode,
            )
            snapshot = snapshots_for_run(conn, run_id)[0]
            assert snapshot["verdict"] == "consistent"
            assert snapshot["result"]["execution_meta"]["semantics_mode"] == mode
    finally:
        catalog.close()
        conn.close()


def test_env_var_selects_mode(monkeypatch):
    monkeypatch.setenv("CALIBER_QUALITY_MODE", "v2")
    assert get_semantics().mode == "v2"
    monkeypatch.setenv("CALIBER_QUALITY_MODE", "legacy")
    assert get_semantics().mode == "legacy"
    monkeypatch.delenv("CALIBER_QUALITY_MODE")
    assert get_semantics().mode == "compat"


def test_cli_run_with_quality_mode_flag(tmp_path, fixture_tree, capsys):
    db_path = tmp_path / "cli-qm.db"
    prefix = ["--db", str(db_path), "--fixtures-dir", str(fixture_tree)]
    assert cli_main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()
    code = cli_main(
        prefix
        + [
            "run",
            "--datasets",
            str(fixture_tree / "datasets"),
            "--quality-mode",
            "v2",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "质量语义模式: v2" in out
    assert "inconsistent" in out  # avg_order_value 依旧不一致


# ---------------------------------------------------------------------------
# 适配层与 v1 黄金主版本的一致性（超出 fixture 的生成用例）
# ---------------------------------------------------------------------------

_GENERATED_CASES = [
    (
        "exact",
        {"a": {"d": 1.0}, "b": {"d": 1.0}},
        {"abs": 1e-6, "rel": 0.0},
    ),
    (
        "boundary_only",
        {"a": {"d": 100.0}, "b": {"d": 100.5}},
        {"abs": 0.5, "rel": 0.0},
    ),
    (
        "boundary_three_impls",
        {"a": {"d": 0.0}, "b": {"d": 0.5}, "c": {"d": 0.25}},
        {"abs": 0.5, "rel": 0.0},
    ),
    (
        "boundary_and_real_gap",
        {"a": {"d": 0.0}, "b": {"d": 0.5}, "c": {"d": 2.0}},
        {"abs": 0.5, "rel": 0.0},
    ),
    (
        "real_gap",
        {"a": {"d": 1.0}, "b": {"d": 2.0}},
        {"abs": 1e-6, "rel": 0.0},
    ),
    (
        "missing_dimension",
        {"a": {"d1": 1.0, "d2": 2.0}, "b": {"d1": 1.0}},
        {"abs": 1e-6, "rel": 0.0},
    ),
    (
        "explicit_null_behaves_like_v1_missing",
        {"a": {"d": None}, "b": {"d": 1.0}},
        {"abs": 1e-6, "rel": 0.0},
    ),
    (
        "rel_boundary",
        {"a": {"d": 50.0}, "b": {"d": 100.0}},
        {"abs": 0.0, "rel": 0.5},
    ),
]


#: 结果相对 v1 发生翻转、必须记录 threshold_boundary 差异的用例；
#: boundary_and_real_gap 的维度结论由真实超差决定，两版一致，不应记录。
_BOUNDARY_FLIP_CASES = {"boundary_only", "boundary_three_impls", "rel_boundary"}


@pytest.mark.parametrize(
    ("name", "results", "tolerances"),
    _GENERATED_CASES,
    ids=[case[0] for case in _GENERATED_CASES],
)
def test_compat_matches_legacy_golden_master(name, results, tolerances):
    from app.core.comparator import compare_impl_results

    compat = compare_impl_results(
        results,
        abs_tolerance=tolerances["abs"],
        rel_tolerance=tolerances["rel"],
        semantics=get_semantics("compat"),
    )
    legacy = compare_impl_results(
        results,
        abs_tolerance=tolerances["abs"],
        rel_tolerance=tolerances["rel"],
        semantics=get_semantics("legacy"),
    )
    assert compat.verdict == legacy.verdict
    assert compat.dimensions == legacy.dimensions
    assert compat.summary == legacy.summary
    assert [asdict(diff) for diff in compat.diffs] == [
        asdict(diff) for diff in legacy.diffs
    ]
    differences = compat.execution_meta["semantic_differences"]
    if name in _BOUNDARY_FLIP_CASES:
        assert any(item["type"] == "threshold_boundary" for item in differences)
    else:
        assert not any(item["type"] == "threshold_boundary" for item in differences)
    if "null" in name:
        assert any(item["type"] == "null_value" for item in differences)
