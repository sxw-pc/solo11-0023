"""只读安全门禁（sql_gate）专项测试。

覆盖验收场景：
1. 含 SELECT 的合法指标正常导入与运行；
2. 含 DELETE 实现的指标在导入摘要中精确定位到文件/key/impl id/语句类型，
   同批其他文件照常入库；
3. 导入后 SQL 被替换成 DROP，运行前被门禁拦下、写入审计，同指标其他实现
   继续执行，连跑两遍拒绝结果稳定、合法实现正常产出快照。
另外覆盖：EXPLAIN 字节码判定、sqlite_master 越权、访问范围、显式白名单、
老 YAML 无 access 降级全量校验。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.audit import list_audit
from app.core.dataset_loader import DatasetCatalog
from app.core.dsl_parser import parse_metric_text
from app.core.registry import MetricRegistry
from app.core.snapshot import get_run, perform_run, snapshots_for_run
from app.core.sql_gate import (
    GATE_META,
    AccessScope,
    SqlGate,
    classify_statement,
    outer_statement_keyword,
)
from app.db import init_db
from app.errors import UnsafeSQLError


# --------------------------------------------------------------------- #
# 纯函数：语句结构归类（降级模式也依赖它）
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", "SELECT"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "SELECT"),
        ("WITH x AS (SELECT 1) DELETE FROM users_a", "DELETE"),
        ("  -- 注释\n INSERT INTO users_a VALUES (1)", "INSERT"),
        ("UPDATE users_a SET status='x'", "UPDATE"),
        ("DROP TABLE users_a", "DROP"),
        ("PRAGMA table_info(users_a)", "PRAGMA"),
        ("ATTACH DATABASE 'x.db' AS other", "ATTACH"),
        ("BEGIN", "BEGIN"),
        ("VACUUM", "VACUUM"),
    ],
)
def test_outer_statement_keyword_classification(sql: str, expected: str):
    assert classify_statement(sql) == expected
    if expected != "UNKNOWN":
        assert outer_statement_keyword(sql) == expected


def test_unknown_statement_normalizes_to_unknown_label():
    assert outer_statement_keyword("nothing") == "NOTHING"
    assert classify_statement("nothing") == "UNKNOWN"


def test_quoted_and_commented_keywords_are_not_statement_types():
    # 字符串字面量与注释里的 DELETE 不影响外层语句判定。
    sql = "SELECT 'DELETE FROM x' AS note -- DROP TABLE y\n"
    assert outer_statement_keyword(sql) == "SELECT"


# --------------------------------------------------------------------- #
# 完整模式门禁：EXPLAIN 字节码写操作识别
# --------------------------------------------------------------------- #
def _gate_on_catalog(catalog: DatasetCatalog) -> SqlGate:
    return SqlGate.from_catalog(catalog)


def test_full_mode_allows_safe_selects(catalog: DatasetCatalog):
    gate = _gate_on_catalog(catalog)
    decision = gate.inspect_text(
        "SELECT last_active_date, COUNT(*) FROM users_a GROUP BY last_active_date"
    )
    try:
        assert decision.allowed
        assert decision.mode == "full"
        assert decision.read_tables == ["users_a"]
        assert decision.write_tables == []
        assert not decision.master_accessed
    finally:
        gate.close()


def test_full_mode_detects_each_non_readonly_statement(catalog: DatasetCatalog):
    gate = _gate_on_catalog(catalog)
    try:
        cases = {
            "INSERT INTO users_a(user_id, status, last_active_date) VALUES (9,'x','2026-01-01')": "INSERT",
            "UPDATE users_a SET status='x'": "UPDATE",
            "DELETE FROM users_a": "DELETE",
            "DROP TABLE users_a": "DROP",
            "CREATE TABLE t(x)": "CREATE",
            "WITH c AS (SELECT 1) DELETE FROM users_a": "DELETE",
            "PRAGMA table_info(users_a)": "PRAGMA",
            "ATTACH DATABASE ':memory:' AS ext": "ATTACH",
        }
        for sql, statement_type in cases.items():
            decision = gate.inspect_text(sql)
            assert not decision.allowed, sql
            reason = decision.rejection_reason()
            assert reason is not None
            assert reason["statement_type"] == statement_type
            assert reason["rule"] in {
                "non_readonly_statement", "write_operation",
            }
    finally:
        gate.close()


def test_window_function_and_recursive_cte_are_not_false_positives(catalog: DatasetCatalog):
    gate = _gate_on_catalog(catalog)
    try:
        recursive = (
            "WITH RECURSIVE cnt(x) AS (VALUES(1) UNION ALL "
            "SELECT x+1 FROM cnt WHERE x<3) SELECT x FROM cnt"
        )
        assert gate.inspect_text(recursive).allowed
        window = (
            "SELECT user_id, ROW_NUMBER() OVER (ORDER BY user_id) AS rn FROM users_a"
        )
        assert gate.inspect_text(window).allowed
    finally:
        gate.close()


def test_multi_statement_and_empty_sql_are_rejected_even_with_allowlist(catalog: DatasetCatalog):
    gate = _gate_on_catalog(catalog)
    try:
        decision = gate.inspect_text(
            "SELECT 1; DELETE FROM users_a",
            allowlist=[{"rule": "unsafe_multistatement", "reason": "尝试豁免"}],
        )
        assert not decision.allowed
        assert decision.primary_violation.rule == "unsafe_multistatement"

        decision = gate.inspect_text(
            "DELETE FROM users_a",
            allowlist=[{"rule": "explain_error", "reason": "无法豁免"}],
        )
        assert not decision.allowed

        with pytest.raises(UnsafeSQLError):
            gate.inspect_text("")
        with pytest.raises(UnsafeSQLError):
            gate.inspect_text("   ")
    finally:
        gate.close()


def test_sqlite_master_access_is_flagged(catalog: DatasetCatalog):
    gate = _gate_on_catalog(catalog)
    try:
        decision = gate.inspect_text("SELECT name FROM sqlite_master WHERE type='table'")
        assert not decision.allowed
        assert decision.master_accessed
        assert decision.primary_violation.rule == "sqlite_master_access"

        # 显式声明允许访问 sqlite_master 后放行。
        allowed = gate.inspect_text(
            "SELECT name FROM sqlite_master WHERE type='table'",
            access_scope={"sqlite_master": True},
        )
        assert allowed.allowed
        assert allowed.master_accessed
    finally:
        gate.close()


def test_table_scope_enforcement_and_legacy_full_validation(catalog: DatasetCatalog):
    gate = _gate_on_catalog(catalog)
    try:
        # 声明只允许 users_a，但 SQL 读了 orders_a → 越权。
        decision = gate.inspect_text(
            "SELECT COUNT(*) FROM orders_a",
            access_scope=AccessScope(tables=frozenset({"users_a"})),
        )
        assert not decision.allowed
        assert decision.primary_violation.rule == "table_out_of_scope"
        assert decision.primary_violation.table == "orders_a"

        # 声明范围内 → 放行。
        ok = gate.inspect_text(
            "SELECT COUNT(*) AS c FROM users_a",
            access_scope={"tables": ["users_a"]},
        )
        assert ok.allowed

        # 老 YAML 未声明 access（None）→ 全量校验，只读 SELECT 不报错。
        legacy = gate.inspect_text(
            "SELECT COUNT(*) AS c FROM orders_a JOIN users_a USING(user_id)"
        )
        assert legacy.allowed
    finally:
        gate.close()


def test_explicit_allowlist_passes_and_is_recorded(catalog: DatasetCatalog):
    gate = _gate_on_catalog(catalog)
    try:
        decision = gate.inspect_text(
            "SELECT name FROM sqlite_master",
            allowlist=[
                {"rule": "sqlite_master_access", "reason": "迁移核对，负责人已审批"}
            ],
        )
        assert decision.allowed
        assert len(decision.whitelisted) == 1
        entry = decision.whitelisted[0]
        assert entry.whitelisted
        assert entry.whitelist_entry["reason"] == "迁移核对，负责人已审批"

        # 不匹配的白名单条目不放行。
        blocked = gate.inspect_text(
            "SELECT name FROM sqlite_master",
            allowlist=[{"rule": "table_out_of_scope", "reason": "规则不匹配"}],
        )
        assert not blocked.allowed
    finally:
        gate.close()


def test_content_hash_cache_detects_replacement(catalog: DatasetCatalog):
    gate = _gate_on_catalog(catalog)
    try:
        safe = "SELECT COUNT(*) AS c FROM users_a"
        first = gate.inspect_text(safe)
        second = gate.inspect_text(safe)
        assert first.sql_sha256 == second.sql_sha256
        assert second.allowed

        dropped = "DROP TABLE users_a"
        replaced = gate.inspect_text(dropped)
        assert replaced.sql_sha256 != first.sql_sha256
        assert not replaced.allowed
    finally:
        gate.close()


def test_degraded_mode_still_blocks_writes_but_skips_table_checks():
    gate = SqlGate(None)
    assert gate.mode == "degraded"
    blocked = gate.inspect_text("DELETE FROM users_a")
    assert not blocked.allowed
    assert blocked.mode == "degraded"
    assert blocked.degraded_reason
    safe = gate.inspect_text("SELECT * FROM users_a")
    assert safe.allowed
    assert safe.read_tables == []  # 无 schema 连接，无法列表


def test_inspect_file_attaches_path_to_context(catalog: DatasetCatalog, tmp_path: Path):
    sql_file = tmp_path / "impl.sql"
    sql_file.write_text("SELECT COUNT(*) AS c FROM users_a", encoding="utf-8")
    gate = SqlGate.from_catalog(catalog)
    try:
        decision = gate.inspect_file(sql_file, context={"stage": "import"})
        assert decision.allowed
        assert decision.context["sql_file"] == str(sql_file)
        assert decision.context["stage"] == "import"
    finally:
        gate.close()


# --------------------------------------------------------------------- #
# 导入侧：批次隔离与精确定位
# --------------------------------------------------------------------- #
def _write_metric(directory: Path, key: str, impls: list[dict]) -> Path:
    lines = [
        f"key: {key}",
        f"name: 指标{key}",
        "owner: data-platform",
        "description: 测试",
        "unit: 人",
        "grain: day",
        "impls:",
    ]
    for impl in impls:
        lines.append(f"  - id: {impl['id']}")
        lines.append(f"    source: {impl['source']}")
        lines.append(f"    sql_file: {impl['sql_file']}")
        if "access" in impl:
            lines.append("    access:")
            if "tables" in impl["access"]:
                lines.append("      tables:")
                for table in impl["access"]["tables"]:
                    lines.append(f"        - {table}")
            if "sqlite_master" in impl["access"]:
                lines.append(f"      sqlite_master: {str(impl['access']['sqlite_master']).lower()}")
        for entry in impl.get("allowlist", []):
            lines.append("    allowlist:")
            lines.append(f"      - rule: {entry['rule']}")
            lines.append(f"        reason: {entry['reason']}")
    path = directory / f"{key}.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_sql(directory: Path, name: str, sql: str) -> Path:
    path = directory / name
    path.write_text(sql + "\n", encoding="utf-8")
    return path


@pytest.fixture
def security_tree(tmp_path: Path):
    impls = tmp_path / "impls"
    metrics = tmp_path / "metrics"
    datasets = tmp_path / "datasets"
    impls.mkdir()
    metrics.mkdir()
    datasets.mkdir()
    # 拷贝真实 CSV，保证 catalog 有 users_a/users_b 表。
    fixture_root = Path(__file__).resolve().parents[1] / "fixtures" / "datasets"
    for csv_name in ("users_a.csv", "users_b.csv"):
        (datasets / csv_name).write_text(
            (fixture_root / csv_name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    return tmp_path, metrics, impls, datasets


def test_import_summary_locates_unsafe_impl_and_keeps_other_files(security_tree):
    tmp_path, metrics, impls, datasets = security_tree
    safe_sql = _write_sql(
        impls, "safe_impl_a.sql",
        "SELECT last_active_date AS day, COUNT(*) AS v FROM users_a GROUP BY last_active_date",
    )
    _write_sql(
        impls, "safe_impl_b.sql",
        "SELECT last_active_date AS day, COUNT(*) AS v FROM users_b GROUP BY last_active_date",
    )
    _write_sql(impls, "delete_impl.sql", "DELETE FROM users_a")

    _write_metric(
        metrics, "safe_metric",
        [
            {"id": "impl_a", "source": "users_a", "sql_file": "safe_impl_a.sql"},
            {"id": "impl_b", "source": "users_b", "sql_file": "safe_impl_b.sql"},
        ],
    )
    dangerous_path = _write_metric(
        metrics, "dangerous_metric",
        [
            {"id": "impl_a", "source": "users_a", "sql_file": "safe_impl_a.sql"},
            {"id": "impl_b", "source": "users_a", "sql_file": "delete_impl.sql"},
        ],
    )

    catalog = DatasetCatalog(datasets)
    conn = init_db(tmp_path / "data" / "repo.sqlite3")
    try:
        gate = SqlGate.from_catalog(catalog)
        catalog.close()
        registry = MetricRegistry(conn, (impls,), gate=gate)
        summary = registry.import_directory(metrics)
        gate.close()

        # 合法文件照常入库，危险文件被单独拒绝。
        assert [item.key for item in summary.imported] == ["safe_metric"]
        assert len(summary.errors) == 1
        error = summary.errors[0]
        assert error["file"] == str(dangerous_path)
        assert error["error"] == "validation_error"
        detail = next(
            item for item in error["details"] if item["code"] == "unsafe_sql"
        )
        assert "impl_b" in detail["message"]
        assert "DELETE" in detail["message"]
        assert detail["field"] == "impls[1].sql_file"
        assert summary.gate_stats["rejected"] == 1
        assert summary.gate_stats["inspected"] == 4

        # 已导入的合法版本不被标记失效。
        rows = conn.execute("SELECT key, current_version FROM metrics").fetchall()
        assert {row[0] for row in rows} == {"safe_metric"}
    finally:
        catalog.close()
        conn.close()


def test_legacy_yaml_without_access_degrades_without_error(security_tree):
    tmp_path, metrics, impls, _datasets = security_tree
    _write_sql(impls, "legacy_a.sql", "SELECT COUNT(*) AS v FROM users_a")
    _write_sql(impls, "legacy_b.sql", "SELECT COUNT(*) AS v FROM users_b")
    path = _write_metric(
        metrics, "legacy_metric",
        [
            {"id": "impl_a", "source": "users_a", "sql_file": "legacy_a.sql"},
            {"id": "impl_b", "source": "users_b", "sql_file": "legacy_b.sql"},
        ],
    )
    # 无 catalog → degraded 模式，老 YAML 没有 access 也能导入。
    conn = init_db(tmp_path / "data" / "legacy.sqlite3")
    try:
        summary = MetricRegistry(conn, (impls,), gate=SqlGate(None)).import_directory(metrics)
        assert summary.ok
        assert summary.gate_stats["mode"] == "degraded"
        assert summary.gate_stats["degraded"] == 2
    finally:
        conn.close()


def test_allowlist_entry_in_yaml_is_audited_on_import(security_tree):
    tmp_path, metrics, impls, datasets = security_tree
    _write_sql(impls, "master.sql", "SELECT name FROM sqlite_master")
    _write_sql(impls, "plain.sql", "SELECT COUNT(*) AS v FROM users_b")
    _write_metric(
        metrics, "audited_metric",
        [
            {
                "id": "impl_a", "source": "users_a", "sql_file": "master.sql",
                "allowlist": [
                    {"rule": "sqlite_master_access", "reason": "一次性迁移核对"}
                ],
            },
            {"id": "impl_b", "source": "users_b", "sql_file": "plain.sql"},
        ],
    )
    catalog = DatasetCatalog(datasets)
    conn = init_db(tmp_path / "data" / "audit.sqlite3")
    try:
        gate = SqlGate.from_catalog(catalog)
        catalog.close()
        summary = MetricRegistry(conn, (impls,), gate=gate).import_directory(metrics)
        gate.close()
        assert summary.ok
        assert summary.gate_stats["whitelisted"] == 1
        actions = [row["action"] for row in list_audit(conn, 100)]
        assert "sql_gate.whitelist" in actions
    finally:
        catalog.close()
        conn.close()


# --------------------------------------------------------------------- #
# 运行侧：单实现拒绝、文件替换 DROP、两遍稳定性
# --------------------------------------------------------------------- #
def test_run_rejects_dangerous_impl_without_affecting_others(security_tree):
    """导入后单条实现 SQL 被替换成 DELETE：运行前只拒绝该实现，其他照常。"""
    tmp_path, metrics, impls, datasets = security_tree
    target = _write_sql(
        impls, "run_a.sql",
        "SELECT last_active_date AS day, COUNT(*) AS v FROM users_a GROUP BY last_active_date",
    )
    _write_sql(
        impls, "run_b.sql",
        "SELECT last_active_date AS day, COUNT(*) AS v FROM users_b GROUP BY last_active_date",
    )
    _write_metric(
        metrics, "run_metric",
        [
            {"id": "impl_a", "source": "users_a", "sql_file": "run_a.sql"},
            {"id": "impl_b", "source": "users_b", "sql_file": "run_b.sql"},
        ],
    )
    catalog = DatasetCatalog(datasets)
    conn = init_db(tmp_path / "data" / "run.sqlite3")
    try:
        import_gate = SqlGate.from_catalog(catalog)
        summary = MetricRegistry(conn, (impls,), gate=import_gate).import_directory(metrics)
        import_gate.close()
        assert summary.ok

        # 导入后把其中一条实现替换成危险语句。
        target.write_text("DELETE FROM users_a\n", encoding="utf-8")

        run_id = perform_run(
            conn, ["run_metric"], catalog, (impls,), datasets_dir=datasets
        )
        snapshots = snapshots_for_run(conn, run_id)
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        implementations = snapshot["result"]["implementations"]
        by_id = {item["impl_id"]: item for item in implementations}

        # 危险实现单独拒绝；另一条正常产出。
        assert by_id["impl_a"]["rejected"] is True
        reason = by_id["impl_a"]["rejection_reason"]
        assert reason["statement_type"] == "DELETE"
        assert by_id["impl_b"]["rejected"] is False
        assert by_id["impl_b"]["row_count"] > 0

        # 运行整体完成而不是 failed；审计里有拒绝记录。
        assert get_run(conn, run_id)["status"] == "completed"
        audit_actions = [row["action"] for row in list_audit(conn, 100)]
        assert "sql_gate.rejected" in audit_actions
    finally:
        catalog.close()
        conn.close()


def test_metric_with_all_impls_rejected_gets_rejected_verdict(security_tree):
    tmp_path, metrics, impls, datasets = security_tree
    first = _write_sql(
        impls, "all_a.sql",
        "SELECT last_active_date AS day, COUNT(*) AS v FROM users_a GROUP BY last_active_date",
    )
    second = _write_sql(
        impls, "all_b.sql",
        "SELECT last_active_date AS day, COUNT(*) AS v FROM users_b GROUP BY last_active_date",
    )
    _write_metric(
        metrics, "all_rejected_metric",
        [
            {"id": "impl_a", "source": "users_a", "sql_file": "all_a.sql"},
            {"id": "impl_b", "source": "users_b", "sql_file": "all_b.sql"},
        ],
    )
    catalog = DatasetCatalog(datasets)
    conn = init_db(tmp_path / "data" / "all.sqlite3")
    try:
        import_gate = SqlGate.from_catalog(catalog)
        assert MetricRegistry(conn, (impls,), gate=import_gate).import_directory(metrics).ok
        import_gate.close()

        first.write_text("DROP TABLE users_a\n", encoding="utf-8")
        second.write_text("DELETE FROM users_b\n", encoding="utf-8")

        run_id = perform_run(
            conn, ["all_rejected_metric"], catalog, (impls,), datasets_dir=datasets
        )
        snapshot = snapshots_for_run(conn, run_id)[0]
        assert snapshot["verdict"] == "rejected"
        assert all(
            item["rejected"]
            for item in snapshot["result"]["implementations"]
        )
        assert get_run(conn, run_id)["summary"]["rejected"] == 1
    finally:
        catalog.close()
        conn.close()


def test_sql_replaced_with_drop_after_import_is_blocked_before_execution(security_tree):
    tmp_path, metrics, impls, datasets = security_tree
    target = _write_sql(
        impls, "swap.sql",
        "SELECT last_active_date AS day, COUNT(*) AS v FROM users_a GROUP BY last_active_date",
    )
    _write_sql(
        impls, "stable.sql",
        "SELECT last_active_date AS day, COUNT(*) AS v FROM users_b GROUP BY last_active_date",
    )
    _write_metric(
        metrics, "swap_metric",
        [
            {"id": "impl_a", "source": "users_a", "sql_file": "swap.sql"},
            {"id": "impl_b", "source": "users_b", "sql_file": "stable.sql"},
        ],
    )
    catalog = DatasetCatalog(datasets)
    conn = init_db(tmp_path / "data" / "swap.sqlite3")
    try:
        import_gate = SqlGate.from_catalog(catalog)
        summary = MetricRegistry(conn, (impls,), gate=import_gate).import_directory(metrics)
        import_gate.close()
        assert summary.ok

        # 导入后把 SQL 文件替换成 DROP。
        target.write_text("DROP TABLE users_a\n", encoding="utf-8")

        run_id = perform_run(
            conn, ["swap_metric"], catalog, (impls,), datasets_dir=datasets
        )
        snapshot = snapshots_for_run(conn, run_id)[0]
        by_id = {
            item["impl_id"]: item
            for item in snapshot["result"]["implementations"]
        }
        rejected = by_id["impl_a"]
        assert rejected["rejected"] is True
        assert rejected["rejection_reason"]["statement_type"] == "DROP"
        assert rejected["rejection_reason"]["rule"] in {
            "non_readonly_statement", "write_operation",
        }
        # 同指标另一条实现继续执行并产出快照。
        assert by_id["impl_b"]["rejected"] is False
        assert by_id["impl_b"]["row_count"] > 0

        # 被替换的表仍然存在：DROP 在执行前就被拦下。
        names = {
            row[0]
            for row in catalog.readonly_connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "users_a" in names

        # 审计落盘，且含 impl id 与 DROP 类型。
        rejection_audits = [
            row for row in list_audit(conn, 100)
            if row["action"] == "sql_gate.rejected"
        ]
        assert rejection_audits
        detail = rejection_audits[0]["detail"]
        assert detail["impl_id"] == "impl_a"
        assert detail["reason"]["statement_type"] == "DROP"

        # 再跑一遍：拒绝结果稳定，合法实现仍正常产出。
        second_id = perform_run(
            conn, ["swap_metric"], catalog, (impls,), datasets_dir=datasets
        )
        second = snapshots_for_run(conn, second_id)[0]
        second_by_id = {
            item["impl_id"]: item
            for item in second["result"]["implementations"]
        }
        assert second_by_id["impl_a"]["rejected"] is True
        assert second_by_id["impl_a"]["rejection_reason"]["statement_type"] == "DROP"
        assert second_by_id["impl_b"]["rejected"] is False
        assert second_by_id["impl_b"]["row_count"] > 0

        # 两遍哈希一致（文件内容未再变化），拒绝规则一致。
        assert (
            second_by_id["impl_a"]["sql_sha256"]
            == rejected["sql_sha256"]
        )
    finally:
        catalog.close()
        conn.close()


def test_gate_meta_records_architecture_and_degradation():
    assert GATE_META["architecture"] == "independent_module"
    assert GATE_META["cache_key"] == "sha256(sql_text)+mode"
    assert "degraded" in GATE_META["degradation"]


def test_runtime_evaluation_error_is_isolated_per_impl(security_tree):
    """门禁通过但执行期出错（如返回非数值）的实现单独标记，不中断其他实现。"""
    tmp_path, metrics, impls, datasets = security_tree
    _write_sql(
        impls, "good.sql",
        "SELECT last_active_date AS day, COUNT(*) AS v FROM users_a GROUP BY last_active_date",
    )
    _write_sql(impls, "bad_text.sql", "SELECT 'not-a-number' AS v")
    _write_metric(
        metrics, "runtime_fail_metric",
        [
            {"id": "impl_a", "source": "users_a", "sql_file": "good.sql"},
            {"id": "impl_b", "source": "users_a", "sql_file": "bad_text.sql"},
        ],
    )
    catalog = DatasetCatalog(datasets)
    conn = init_db(tmp_path / "data" / "runtime.sqlite3")
    try:
        gate = SqlGate.from_catalog(catalog)
        assert MetricRegistry(conn, (impls,), gate=gate).import_directory(metrics).ok
        gate.close()
        run_id = perform_run(
            conn, ["runtime_fail_metric"], catalog, (impls,), datasets_dir=datasets
        )
        snapshot = snapshots_for_run(conn, run_id)[0]
        by_id = {
            item["impl_id"]: item
            for item in snapshot["result"]["implementations"]
        }
        assert by_id["impl_a"]["rejected"] is False
        assert by_id["impl_b"]["rejected"] is True
        assert by_id["impl_b"]["rejection_reason"]["rule"] == "evaluation_error"
        # 运行整体仍完成。
        assert get_run(conn, run_id)["status"] == "completed"
        actions = [row["action"] for row in list_audit(conn, 100)]
        assert "sql_gate.evaluation_failed" in actions
    finally:
        catalog.close()
        conn.close()
