"""Merge-window aggregation over existing runs/snapshots rows."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.core.dataset_loader import DatasetCatalog
from app.core.reporter import build_report, export_report
from app.core.snapshot import (
    create_run,
    finish_run,
    get_run,
    save_snapshot,
    snapshots_for_run,
)
from app.core.windowing import (
    DEFAULT_WINDOW_SIZE,
    STATUS_CONSISTENT,
    STATUS_FIRST_DEVIATION,
    STATUS_ONGOING_DEVIATION,
    STATUS_RECOVERED,
    compute_window,
    metric_window,
    normalize_size,
    normalize_window,
    recompute_metric_window,
)
from app.errors import CaliberError
from app.models import (
    ComparisonResult,
    DimensionDiff,
    ImplementDef,
    MetricDefinition,
)


def _definition(key: str) -> MetricDefinition:
    return MetricDefinition(
        key=key,
        name=f"窗口指标 {key}",
        owner="qa-team",
        description="合成窗口测试指标",
        unit="人",
        grain="day",
        impls=[
            ImplementDef(id="impl_a", source="users_a", sql_file="a.sql"),
            ImplementDef(id="impl_b", source="users_b", sql_file="b.sql"),
        ],
    )


def _comparison(metric_key: str, verdict: str) -> ComparisonResult:
    diffs: list[DimensionDiff] = []
    if verdict == "inconsistent":
        diffs = [
            DimensionDiff(
                dimension="2026-02-01",
                kind="value_mismatch",
                values={"impl_a": 10.0, "impl_b": 12.0},
                max_delta=2.0,
                detail="数值超出容差",
            )
        ]
    elif verdict == "partial":
        diffs = [
            DimensionDiff(
                dimension="2026-02-01",
                kind="missing_dimension",
                values={"impl_a": 10.0, "impl_b": None},
                detail="以下实现缺少该维度: impl_b",
            )
        ]
    summary = {
        "impl_count": 2,
        "dimension_count": 1,
        "consistent_dimensions": 0 if diffs else 1,
        "value_mismatches": 1 if verdict == "inconsistent" else 0,
        "missing_dimensions": 1 if verdict == "partial" else 0,
        "diff_count": len(diffs),
    }
    return ComparisonResult(
        metric_key=metric_key,
        metric_version=1,
        impl_ids=["impl_a", "impl_b"],
        verdict=verdict,
        dimensions=["2026-02-01"],
        diffs=diffs,
        summary=summary,
        abs_tolerance=1e-6,
        rel_tolerance=0.0,
    )


def _append_run(
    conn: sqlite3.Connection,
    metric_key: str,
    verdict: str,
    *,
    result_mode: str = "full",
    window_size: int = DEFAULT_WINDOW_SIZE,
) -> int:
    """Create one run with one snapshot; returns the snapshot id."""
    run_id = create_run(conn, [metric_key], "fixtures/datasets", {"abs": 1e-6, "rel": 0})
    snapshot_id = save_snapshot(
        conn, run_id, _definition(metric_key), _comparison(metric_key, verdict), {},
        window_size=window_size,
    )
    finish_run(conn, run_id, status="completed", summary={"metrics": 1, "run_id": run_id})
    if result_mode != "full":
        # Simulate a pre-window snapshot with no result detail at all.
        conn.execute(
            "UPDATE snapshots SET result_json = ?, summary_json = ? WHERE id = ?",
            ("{}" if result_mode == "empty" else "not-json", "{}", snapshot_id),
        )
        conn.commit()
    return snapshot_id


def _window_of(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    row = conn.execute(
        "SELECT summary_json FROM snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    return json.loads(row["summary_json"])["window"]


def test_window_state_transitions_match_manual_annotation(conn):
    """无偏离 → 首次偏离 → 持续偏离 → 恢复 → 再次首次偏离."""
    never = "w_never"
    episode = "w_episode"

    # never-deviated: five consistent runs
    never_ids = [_append_run(conn, never, "consistent") for _ in range(5)]
    # episode: consistent, consistent, inconsistent x2, consistent
    sequence = ["consistent", "consistent", "inconsistent", "inconsistent", "consistent"]
    episode_ids = [_append_run(conn, episode, verdict) for verdict in sequence]

    never_window = _window_of(conn, never_ids[-1])
    assert never_window["status"] == STATUS_CONSISTENT
    assert never_window["deviation_start_run_id"] is None
    assert never_window["deviation_end_run_id"] is None

    assert _window_of(conn, episode_ids[1])["status"] == STATUS_CONSISTENT
    first = _window_of(conn, episode_ids[2])
    third_run = conn.execute(
        "SELECT run_id FROM snapshots WHERE id = ?", (episode_ids[2],)
    ).fetchone()[0]
    assert first["status"] == STATUS_FIRST_DEVIATION
    assert first["deviation_start_run_id"] == third_run
    assert first["deviation_end_run_id"] is None

    ongoing = _window_of(conn, episode_ids[3])
    assert ongoing["status"] == STATUS_ONGOING_DEVIATION
    assert ongoing["deviation_start_run_id"] == third_run
    assert ongoing["deviation_end_run_id"] is None

    recovered = _window_of(conn, episode_ids[4])
    fifth_run = conn.execute(
        "SELECT run_id FROM snapshots WHERE id = ?", (episode_ids[4],)
    ).fetchone()[0]
    assert recovered["status"] == STATUS_RECOVERED
    assert recovered["deviation_start_run_id"] == third_run
    assert recovered["deviation_end_run_id"] == fifth_run
    assert [
        (t["type"], t["run_id"]) for t in recovered["transitions"]
    ] == [
        ("deviation_start", third_run),
        ("deviation_end", fifth_run),
    ]


def test_window_respects_size_and_lookback(conn):
    key = "w_size"
    verdicts = ["consistent", "inconsistent", "inconsistent", "consistent",
                "inconsistent", "consistent"]
    ids = [_append_run(conn, key, verdict, window_size=3) for verdict in verdicts]
    # Window covers the last three runs only; the run before them is look-back.
    window = _window_of(conn, ids[-1])
    assert window["size"] == 3
    assert window["entry_count"] == 3
    assert window["window_run_ids"] == [
        conn.execute("SELECT run_id FROM snapshots WHERE id = ?", (item,)).fetchone()[0]
        for item in ids[-3:]
    ]
    assert window["lookback"]["run_id"] == conn.execute(
        "SELECT run_id FROM snapshots WHERE id = ?", (ids[-4],)
    ).fetchone()[0]
    # Deviation began in the look-back run, so the latest episode is ongoing
    # even though that run is outside the window.
    latest = _window_of(conn, ids[-1])
    assert latest["lookback"]["deviating"] is True


def test_partial_and_inconsistent_both_count_as_deviation(conn):
    key = "w_partial"
    ids = [
        _append_run(conn, key, "consistent"),
        _append_run(conn, key, "partial"),
        _append_run(conn, key, "inconsistent"),
        _append_run(conn, key, "consistent"),
    ]
    window = _window_of(conn, ids[-1])
    assert window["status"] == STATUS_RECOVERED
    assert window["deviation_count"] == 2
    assert window["consistent_count"] == 2
    # The episode keeps the first deviating run and the recovering run.
    assert window["deviation_start_run_id"] == conn.execute(
        "SELECT run_id FROM snapshots WHERE id = ?", (ids[1],)
    ).fetchone()[0]
    assert window["deviation_end_run_id"] == conn.execute(
        "SELECT run_id FROM snapshots WHERE id = ?", (ids[3],)
    ).fetchone()[0]


@pytest.mark.parametrize("mode", ["empty", "corrupt"])
def test_legacy_snapshot_degrades_to_verdict_only(conn, mode):
    key = "w_legacy"
    first = _append_run(conn, key, "consistent")
    legacy = _append_run(conn, key, "inconsistent", result_mode=mode)
    live = _append_run(conn, key, "consistent")

    # Recompute anchored at the legacy snapshot itself: detail missing, so the
    # window degrades but still classifies by verdict.
    result = recompute_metric_window(conn, key, anchor_snapshot_id=legacy)
    window = result["window"]
    assert window["degraded"] is True
    assert window["degraded_snapshot_ids"] == [legacy]
    assert "verdict" in window["degraded_notice"]
    assert window["entries"][-1]["detail_available"] is False
    assert window["entries"][-1]["verdict"] == "inconsistent"
    assert window["status"] == STATUS_FIRST_DEVIATION

    # Newer snapshots still carry full detail; the legacy one stays degraded.
    newest = _window_of(conn, live)
    assert newest["degraded"] is True
    assert legacy in newest["degraded_snapshot_ids"]
    assert newest["status"] == STATUS_RECOVERED

    # Report export must not break and must surface the notice.
    run_id = conn.execute(
        "SELECT run_id FROM snapshots WHERE id = ?", (live,)
    ).fetchone()[0]
    report = build_report(conn, run_id)
    legacy_window = next(
        item for item in report["windows"] if item["metric_key"] == key
    )
    assert legacy_window["degraded"] is True
    assert report["summary"]["window_degraded"] == 1
    _, markdown = export_report(report, "md")
    assert "归并窗口" in markdown and "降级" in markdown
    _, rendered = export_report(report, "html")
    assert "降级" in rendered
    assert first  # legacy rows remain readable


def test_recompute_is_stable_and_writes_back_once(conn):
    key = "w_idempotent"
    ids = [
        _append_run(conn, key, verdict)
        for verdict in ["consistent", "inconsistent", "consistent"]
    ]
    snapshot_id = ids[-1]
    # save_snapshot already wrote the block; clear it to force a write.
    conn.execute(
        "UPDATE snapshots SET summary_json = '{}' WHERE id = ?", (snapshot_id,)
    )
    conn.commit()

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        first = recompute_metric_window(conn, key)
        second = recompute_metric_window(conn, key)
    finally:
        conn.set_trace_callback(None)

    assert first["changed"] is True
    assert second["changed"] is False
    assert first["window"] == second["window"]
    updates = [sql for sql in statements if sql.lstrip().upper().startswith("UPDATE")]
    assert len(updates) == 1
    assert "snapshots" in updates[0]
    stored = _window_of(conn, snapshot_id)
    assert stored == first["window"]
    # Recompute did not create duplicate snapshots or extra writes.
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 3
    assert json.dumps(stored, sort_keys=True) == json.dumps(second["window"], sort_keys=True)


def test_recompute_reads_only_metric_window_rows(conn):
    """Adding unrelated metrics must not increase the rows read per recompute."""
    key = "w_incremental"
    for verdict in ["consistent", "inconsistent", "consistent"]:
        _append_run(conn, key, verdict, window_size=2)
    baseline = recompute_metric_window(conn, key)["rows_scanned"]
    assert baseline <= 3  # size + 1 look-back

    for index in range(40):
        _append_run(conn, f"w_other_{index:02d}", "consistent", window_size=2)

    assert recompute_metric_window(conn, key)["rows_scanned"] == baseline

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        recompute_metric_window(conn, key)
    finally:
        conn.set_trace_callback(None)
    selects = [
        sql for sql in statements
        if sql.lstrip().upper().startswith("SELECT") and "snapshots" in sql
    ]
    assert selects
    assert all("LIMIT" in sql and "WHERE metric_key =" in sql for sql in selects)


def test_compute_window_is_pure_and_deterministic(conn):
    key = "w_pure"
    for verdict in ["consistent", "inconsistent", "inconsistent", "consistent"]:
        _append_run(conn, key, verdict)
    rows = conn.execute(
        "SELECT id, run_id, metric_key, metric_version, verdict, summary_json, "
        "result_json, created_at FROM snapshots WHERE metric_key = ? "
        "ORDER BY id DESC LIMIT 5",
        (key,),
    ).fetchall()
    first = compute_window(rows, key)
    second = compute_window(rows, key)
    assert first["digest"] == second["digest"]
    assert first == second
    # The digest ignores timestamps, so a re-read of the same rows is stable.
    assert first["window_run_ids"] == sorted(first["window_run_ids"])


def test_metric_window_reads_and_derives_legacy_without_writing(conn):
    key = "w_derive"
    _append_run(conn, key, "consistent")
    legacy = _append_run(conn, key, "inconsistent", result_mode="empty")
    before = conn.execute(
        "SELECT summary_json FROM snapshots WHERE id = ?", (legacy,)
    ).fetchone()[0]

    derived = metric_window(conn, key)
    assert derived["source"] == "derived_on_read"
    assert derived["status"] == STATUS_FIRST_DEVIATION
    assert derived["degraded"] is True
    after = conn.execute(
        "SELECT summary_json FROM snapshots WHERE id = ?", (legacy,)
    ).fetchone()[0]
    assert before == after  # read-only derivation

    recomputed = metric_window(conn, key, recompute=True)
    assert recomputed["source"] == "recomputed"
    assert recomputed["changed"] is True
    assert metric_window(conn, key, recompute=True)["changed"] is False
    assert metric_window(conn, "missing_metric")["source"] == "unavailable"


def test_window_policy_and_size_validation():
    assert normalize_window(None, None) == ("run_count", DEFAULT_WINDOW_SIZE)
    with pytest.raises(CaliberError) as unsupported:
        normalize_window("time_span", 5)
    assert "WINDOW_POLICIES" in unsupported.value.message
    with pytest.raises(CaliberError):
        normalize_size(0)
    with pytest.raises(CaliberError):
        normalize_size("abc")


def test_run_metadata_records_policy_choice_and_switch_point(
    registry_conn, catalog: DatasetCatalog, sql_roots, fixture_tree: Path
):
    from app.core.snapshot import perform_run

    run_id = perform_run(
        registry_conn,
        ["active_users"],
        catalog,
        sql_roots,
        datasets_dir=fixture_tree / "datasets",
        window_size=3,
    )
    run = get_run(registry_conn, run_id)
    meta = run["summary"]["window"]
    assert meta["policy"] == "run_count"
    assert meta["size"] == 3
    assert "固定 run 数" in meta["selection_reason"]
    assert "WINDOW_POLICIES" in meta["switch_point"]
    snapshot = snapshots_for_run(registry_conn, run_id)[0]
    window = snapshot["summary"]["window"]
    assert window["policy"] == "run_count" and window["size"] == 3
    assert window["entry_count"] == 1
    assert window["status"] == STATUS_CONSISTENT

    report = build_report(registry_conn, run_id)
    assert report["window_policy"]["policy"] == "run_count"
    assert report["window_policy"]["size"] == 3
    assert report["window_policy"]["switch_point"]


def test_perform_run_rejects_unknown_policy(
    registry_conn, catalog: DatasetCatalog, sql_roots, fixture_tree: Path
):
    from app.core.snapshot import perform_run

    with pytest.raises(CaliberError):
        perform_run(
            registry_conn,
            ["active_users"],
            catalog,
            sql_roots,
            datasets_dir=fixture_tree / "datasets",
            window_policy="time_span",
        )
    # No half-created run is left behind.
    assert registry_conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_end_to_end_fixture_transitions_recovery_and_never_deviated(
    registry_conn, fixture_tree: Path, sql_roots
):
    """Acceptance shape: deviation, recovery and never-deviated over real runs."""
    from app.core.dataset_loader import DatasetCatalog
    from app.core.snapshot import perform_run

    users_b = fixture_tree / "datasets" / "users_b.csv"
    original = users_b.read_text(encoding="utf-8")
    keys = ["active_users", "conversion_rate", "avg_order_value"]

    def run_once() -> int:
        # A fresh catalog is required: CSV data is loaded into memory on load.
        catalog = DatasetCatalog(fixture_tree / "datasets")
        try:
            return perform_run(
                registry_conn,
                keys,
                catalog,
                sql_roots,
                datasets_dir=fixture_tree / "datasets",
                abs_tolerance=1e-6,
                rel_tolerance=0.0,
            )
        finally:
            catalog.close()

    # Run 1: all three fixture metrics are at their baseline verdicts.
    run1 = run_once()

    # Run 2: active_users deviates.
    users_b.write_text(original + "2008,2026-01-20,2026-02-01,active\n", encoding="utf-8")
    run2 = run_once()

    # Run 3: still deviating.
    run3 = run_once()

    # Run 4: active_users recovers.
    users_b.write_text(original, encoding="utf-8")
    run4 = run_once()

    report = build_report(registry_conn, run4)
    windows = {item["metric_key"]: item for item in report["windows"]}

    # Manually annotated expectation for active_users: deviation at run 2,
    # recovery at run 4; window covers runs 1..4 because size defaults to 5.
    assert windows["active_users"]["status"] == STATUS_RECOVERED
    assert windows["active_users"]["deviation_start_run_id"] == run2
    assert windows["active_users"]["deviation_end_run_id"] == run4
    assert windows["active_users"]["window_run_ids"] == [run1, run2, run3, run4]
    assert windows["active_users"]["degraded"] is False

    # conversion_rate never deviated.
    assert windows["conversion_rate"]["status"] == STATUS_CONSISTENT
    assert windows["conversion_rate"]["deviation_start_run_id"] is None

    # avg_order_value deviated from the very first run, so at the anchor it is
    # an ongoing deviation whose start is still traceable to run 1.
    assert windows["avg_order_value"]["status"] == STATUS_ONGOING_DEVIATION
    assert windows["avg_order_value"]["deviation_start_run_id"] == run1

    # At run 2, active_users' anchor is the first deviating run of its episode.
    second = build_report(registry_conn, run2)
    second_active = next(
        item for item in second["windows"] if item["metric_key"] == "active_users"
    )
    assert second_active["status"] == STATUS_FIRST_DEVIATION
    assert second_active["deviation_start_run_id"] == run2

    # Determinism: recomputing the same window twice must not change anything.
    before = registry_conn.execute(
        "SELECT id, summary_json FROM snapshots ORDER BY id"
    ).fetchall()
    first = recompute_metric_window(registry_conn, "active_users")
    second = recompute_metric_window(registry_conn, "active_users")
    after = registry_conn.execute(
        "SELECT id, summary_json FROM snapshots ORDER BY id"
    ).fetchall()
    assert first["changed"] is False and second["changed"] is False
    assert [tuple(row) for row in before] == [tuple(row) for row in after]

    # Legacy snapshot in the middle of the series degrades to verdict-only.
    legacy_snapshot = registry_conn.execute(
        "SELECT id FROM snapshots WHERE run_id = ? AND metric_key = 'active_users'",
        (run3,),
    ).fetchone()[0]
    registry_conn.execute(
        "UPDATE snapshots SET result_json = '{}' WHERE id = ?", (legacy_snapshot,)
    )
    registry_conn.commit()
    degraded = recompute_metric_window(registry_conn, "active_users")["window"]
    assert degraded["degraded"] is True
    assert degraded["degraded_snapshot_ids"] == [legacy_snapshot]
    assert degraded["status"] == STATUS_RECOVERED
    assert degraded["deviation_start_run_id"] == run2
    assert degraded["deviation_end_run_id"] == run4

    for fmt in ("json", "md", "html"):
        _, content = export_report(build_report(registry_conn, run4), fmt)
        assert content
