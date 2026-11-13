"""Integration tests for concurrent, failure-isolated, resumable batch runs."""
from __future__ import annotations

import threading
import time

import pytest

from app.core import snapshot as snapshot_module
from app.core.dataset_loader import DatasetCatalog
from app.errors import EvaluationError
from app.core.snapshot import (
    RUN_COMPLETED,
    RUN_COMPLETED_WITH_ERRORS,
    perform_run,
    run_items_for_run,
    snapshots_for_run,
)


def _run(catalog, registry_conn, sql_roots, fixture_tree, **kwargs):
    defaults = dict(
        datasets_dir=fixture_tree / "datasets",
        timeout_seconds=2.0,
        max_rows=100,
    )
    defaults.update(kwargs)
    return perform_run(registry_conn, None, catalog, sql_roots, **defaults)


def _items_by_key(conn, run_id: int) -> dict[str, dict]:
    return {item["metric_key"]: item for item in run_items_for_run(conn, run_id)}


def test_fixture_run_completes_under_concurrency_and_matches_serial_verdicts(
    registry_conn, catalog, sql_roots, fixture_tree
):
    run_id = _run(catalog, registry_conn, sql_roots, fixture_tree, max_workers=3)
    items = _items_by_key(registry_conn, run_id)
    assert set(items) == {"active_users", "avg_order_value", "conversion_rate"}
    assert all(item["status"] == "success" for item in items.values())
    assert items["active_users"]["verdict"] == "consistent"
    assert items["avg_order_value"]["verdict"] == "inconsistent"
    assert items["conversion_rate"]["verdict"] == "consistent"
    # One snapshot per successful rule, same data the serial path would store.
    snapshots = snapshots_for_run(registry_conn, run_id)
    assert len(snapshots) == 3
    # Peak concurrency is bounded by the configured ceiling.
    run = snapshot_module.get_run(registry_conn, run_id)
    assert run["status"] == RUN_COMPLETED
    assert run["summary"]["peak_workers"] <= 3
    assert run["summary"]["max_workers"] == 3


def test_rule_failure_is_isolated_and_successful_results_are_retained(
    registry_conn, catalog, sql_roots, fixture_tree, monkeypatch
):
    real_run_metric = snapshot_module.run_metric

    def flaky_run_metric(metric, version, catalog_, roots, **kwargs):
        if metric.key == "avg_order_value":
            raise EvaluationError("注入失败：该规则必须独立挂掉")
        return real_run_metric(metric, version, catalog_, roots, **kwargs)

    monkeypatch.setattr(snapshot_module, "run_metric", flaky_run_metric)
    run_id = _run(catalog, registry_conn, sql_roots, fixture_tree, max_workers=2)

    items = _items_by_key(registry_conn, run_id)
    assert items["avg_order_value"]["status"] == "failed"
    assert "注入失败" in items["avg_order_value"]["error"]
    assert items["active_users"]["status"] == "success"
    assert items["conversion_rate"]["status"] == "success"
    run = snapshot_module.get_run(registry_conn, run_id)
    assert run["status"] == RUN_COMPLETED_WITH_ERRORS
    summary = run["summary"]
    assert summary["succeeded"] == 2
    assert summary["failed"] == 1
    assert summary["skipped"] == 0
    # The two successful rules still have durable snapshots.
    snapshots = snapshots_for_run(registry_conn, run_id)
    assert {row["metric_key"] for row in snapshots} == {
        "active_users",
        "conversion_rate",
    }


def test_resume_second_pass_only_reruns_failed_and_pending_rules(
    registry_conn, catalog, sql_roots, fixture_tree, monkeypatch
):
    real_run_metric = snapshot_module.run_metric
    failed_once = {"avg_order_value": False}

    def flaky_run_metric(metric, version, catalog_, roots, **kwargs):
        if metric.key == "avg_order_value" and not failed_once["avg_order_value"]:
            failed_once["avg_order_value"] = True
            raise EvaluationError("第一次故意失败")
        return real_run_metric(metric, version, catalog_, roots, **kwargs)

    monkeypatch.setattr(snapshot_module, "run_metric", flaky_run_metric)

    first_id = _run(catalog, registry_conn, sql_roots, fixture_tree, max_workers=3)
    first_items = _items_by_key(registry_conn, first_id)
    assert first_items["avg_order_value"]["status"] == "failed"
    first_snapshot_ids = {
        key: item["snapshot_id"]
        for key, item in first_items.items()
        if item["status"] == "success"
    }
    assert first_snapshot_ids  # successes were retained from pass one

    # Pass two with resume: the flaky rule now succeeds, prior successes skip.
    second_id = _run(
        catalog, registry_conn, sql_roots, fixture_tree, max_workers=3, resume=True
    )
    second_items = _items_by_key(registry_conn, second_id)
    assert second_items["avg_order_value"]["status"] == "success"
    assert second_items["active_users"]["status"] == "skipped"
    assert second_items["conversion_rate"]["status"] == "skipped"
    # Skipped rules point at the exact snapshots from the first run.
    for key, snapshot_id in first_snapshot_ids.items():
        assert second_items[key]["snapshot_id"] == snapshot_id
    # No duplicate snapshots were created for skipped rules.
    second_snapshots = snapshots_for_run(registry_conn, second_id)
    assert [row["metric_key"] for row in second_snapshots] == ["avg_order_value"]

    run = snapshot_module.get_run(registry_conn, second_id)
    assert run["status"] == RUN_COMPLETED
    assert run["summary"]["succeeded"] == 1
    assert run["summary"]["failed"] == 0
    assert run["summary"]["skipped"] == 2

    # Without resume, a fresh run recomputes everything (nothing is skipped).
    third_id = _run(catalog, registry_conn, sql_roots, fixture_tree, max_workers=3)
    third_items = _items_by_key(registry_conn, third_id)
    assert all(item["status"] == "success" for item in third_items.values())
    assert len(snapshots_for_run(registry_conn, third_id)) == 3


def test_peak_concurrency_never_exceeds_configured_limit(
    registry_conn, catalog, sql_roots, fixture_tree, monkeypatch
):
    limit = 2
    live = 0
    peak = 0
    lock = threading.Lock()
    real_run_metric = snapshot_module.run_metric

    def tracked_run_metric(metric, version, catalog_, roots, **kwargs):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
            assert live <= limit
        time.sleep(0.05)
        try:
            return real_run_metric(metric, version, catalog_, roots, **kwargs)
        finally:
            with lock:
                live -= 1

    monkeypatch.setattr(snapshot_module, "run_metric", tracked_run_metric)
    run_id = _run(
        catalog, registry_conn, sql_roots, fixture_tree, max_workers=limit
    )
    assert peak == limit  # the workload actually saturated the pool
    run = snapshot_module.get_run(registry_conn, run_id)
    assert run["summary"]["peak_workers"] == limit
    assert run["summary"]["max_workers"] == limit


def test_worker_connections_are_reclaimed_after_success_and_failure(
    registry_conn, fixture_tree, sql_roots, monkeypatch
):
    # A fresh catalog so we own its whole lifecycle in this test.
    catalog = DatasetCatalog(fixture_tree / "datasets")
    real_run_metric = snapshot_module.run_metric

    def flaky_run_metric(metric, version, catalog_, roots, **kwargs):
        if metric.key == "conversion_rate":
            raise EvaluationError("失败也要回收连接")
        return real_run_metric(metric, version, catalog_, roots, **kwargs)

    monkeypatch.setattr(snapshot_module, "run_metric", flaky_run_metric)
    try:
        run_id = _run(catalog, registry_conn, sql_roots, fixture_tree, max_workers=3)
        items = _items_by_key(registry_conn, run_id)
        assert items["conversion_rate"]["status"] == "failed"
        # After the run every worker connection has been returned, and no
        # worker thread survived.
        assert catalog.active_worker_connections == 0
        assert catalog.total_worker_connections == 3
        leftovers = [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("caliber-rule") and thread.is_alive()
        ]
        assert leftovers == []
    finally:
        catalog.close()
    # Closing the catalog closes every provisioned connection for good.
    assert catalog.total_worker_connections == 0


def test_serial_mode_max_workers_one_behaves_like_the_old_path(
    registry_conn, catalog, sql_roots, fixture_tree
):
    run_id = _run(catalog, registry_conn, sql_roots, fixture_tree, max_workers=1)
    run = snapshot_module.get_run(registry_conn, run_id)
    assert run["summary"]["peak_workers"] == 1
    items = run_items_for_run(registry_conn, run_id)
    # With one worker, every rule ran on the same worker thread and positions
    # follow the input ordering deterministically.
    assert [item["position"] for item in items] == [0, 1, 2]
    assert len({item["worker"] for item in items}) == 1


def test_resume_when_everything_already_succeeded_creates_no_workers(
    registry_conn, catalog, sql_roots, fixture_tree
):
    first_id = _run(catalog, registry_conn, sql_roots, fixture_tree, max_workers=3)
    assert snapshot_module.get_run(registry_conn, first_id)["status"] == RUN_COMPLETED
    before = catalog.total_worker_connections
    second_id = _run(
        catalog, registry_conn, sql_roots, fixture_tree, max_workers=3, resume=True
    )
    items = run_items_for_run(registry_conn, second_id)
    assert items and all(item["status"] == "skipped" for item in items)
    # No evaluation resources were provisioned for an all-skipped run.
    assert catalog.total_worker_connections == before
    assert catalog.active_worker_connections == 0


def test_invalid_max_workers_is_rejected(
    registry_conn, catalog, sql_roots, fixture_tree
):
    for bad in (0, -2):
        with pytest.raises(ValueError):
            _run(
                catalog,
                registry_conn,
                sql_roots,
                fixture_tree,
                max_workers=bad,
            )


def test_cancellation_finalizes_inflight_work_and_reclaims_resources(
    registry_conn, fixture_tree, sql_roots, monkeypatch
):
    """The KeyboardInterrupt handler delegates to this same cooperative-cancel
    path: in-flight rules stop, the pool drains and every connection returns."""
    from app.core.executor import ConcurrentRuleExecutor

    catalog = DatasetCatalog(fixture_tree / "datasets")
    entered = threading.Event()
    release = threading.Event()
    real_run_metric = snapshot_module.run_metric

    def blocking_run_metric(metric, version, catalog_, roots, **kwargs):
        if metric.key == "active_users":
            entered.set()
            release.wait(timeout=5)
        return real_run_metric(metric, version, catalog_, roots, **kwargs)

    monkeypatch.setattr(snapshot_module, "run_metric", blocking_run_metric)

    original_init = ConcurrentRuleExecutor.__init__
    captured: dict[str, ConcurrentRuleExecutor] = {}

    def capturing_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captured["executor"] = self

    monkeypatch.setattr(ConcurrentRuleExecutor, "__init__", capturing_init)

    def driver():
        try:
            _run(catalog, registry_conn, sql_roots, fixture_tree, max_workers=2)
        except BaseException as exc:  # the run surfaces a failed/cancelled state
            captured["error"] = exc

    thread = threading.Thread(target=driver)
    thread.start()
    try:
        assert entered.wait(timeout=3), f"driver never entered task; captured={captured!r}"
        # This is exactly what the KeyboardInterrupt branch in perform_run does.
        captured["executor"].cancel()
    finally:
        release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    # Every worker connection came back and no pool thread survived.
    assert catalog.active_worker_connections == 0
    leftovers = [
        item.name
        for item in threading.enumerate()
        if item.name.startswith("caliber-rule") and item.is_alive()
    ]
    assert leftovers == []
    catalog.close()
    assert catalog.total_worker_connections == 0


def test_unknown_rule_mixed_with_valid_rules_fails_alone(
    registry_conn, catalog, sql_roots, fixture_tree
):
    run_id = perform_run(
        registry_conn,
        ["active_users", "missing_metric", "conversion_rate"],
        catalog,
        sql_roots,
        datasets_dir=fixture_tree / "datasets",
        timeout_seconds=2.0,
        max_rows=100,
        max_workers=2,
    )
    items = _items_by_key(registry_conn, run_id)
    assert items["missing_metric"]["status"] == "failed"
    assert items["active_users"]["status"] == "success"
    assert items["conversion_rate"]["status"] == "success"
    assert snapshot_module.get_run(registry_conn, run_id)["status"] == (
        RUN_COMPLETED_WITH_ERRORS
    )


def test_keyboard_interrupt_finalizes_run_as_failed_and_reclaims_resources(
    registry_conn, fixture_tree, sql_roots, monkeypatch
):
    catalog = DatasetCatalog(fixture_tree / "datasets")
    from app.core.executor import ConcurrentRuleExecutor

    real_init = ConcurrentRuleExecutor.__init__
    holder: dict[str, ConcurrentRuleExecutor] = {}

    def capturing(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        holder["executor"] = self

    monkeypatch.setattr(ConcurrentRuleExecutor, "__init__", capturing)

    # Make the stream raise KeyboardInterrupt on its first yielded outcome,
    # mirroring Ctrl-C arriving while the main loop drains results.  A plain
    # function (not a generator wrapper) keeps the raise immediate rather than
    # deferring it until generator close.
    real_stream = ConcurrentRuleExecutor.run_rules_stream

    def interrupting_stream(self, rules, task):
        inner = real_stream(self, rules, task)
        first = next(inner)
        raise KeyboardInterrupt()
        yield first  # unreachable; makes this a generator function
        yield from inner

    monkeypatch.setattr(ConcurrentRuleExecutor, "run_rules_stream", interrupting_stream)

    with pytest.raises(KeyboardInterrupt):
        _run(catalog, registry_conn, sql_roots, fixture_tree, max_workers=2)

    # The run row was finalized as failed and every resource was given back.
    latest = snapshot_module.list_runs(registry_conn, 1)[0]
    assert latest["status"] == "failed"
    assert "中断" in (latest["error"] or "")
    assert catalog.active_worker_connections == 0
    leftovers = [
        item.name
        for item in threading.enumerate()
        if item.name.startswith("caliber-rule") and item.is_alive()
    ]
    assert leftovers == []
    catalog.close()


def test_unexpected_stream_error_finalizes_run_as_failed(
    registry_conn, fixture_tree, sql_roots, monkeypatch
):
    """A non-KeyboardInterrupt defect while draining outcomes must still
    finalize the run row and surface a RunError instead of leaking resources."""
    from app.core.executor import ConcurrentRuleExecutor
    from app.errors import RunError

    catalog = DatasetCatalog(fixture_tree / "datasets")

    def broken_stream(self, rules, task):
        raise RuntimeError("流处理内部故障")
        yield  # make it a generator

    monkeypatch.setattr(ConcurrentRuleExecutor, "run_rules_stream", broken_stream)
    try:
        with pytest.raises(RunError):
            _run(catalog, registry_conn, sql_roots, fixture_tree, max_workers=2)
        latest = snapshot_module.list_runs(registry_conn, 1)[0]
        assert latest["status"] == "failed"
        assert "流处理内部故障" in (latest["error"] or "")
        assert catalog.active_worker_connections == 0
    finally:
        catalog.close()


def test_all_unknown_metrics_still_raise_before_creating_work(
    registry_conn, catalog, sql_roots, fixture_tree
):
    from app.errors import NotFoundError

    with pytest.raises(NotFoundError):
        perform_run(
            registry_conn,
            ["nope-1", "nope-2"],
            catalog,
            sql_roots,
            datasets_dir=fixture_tree / "datasets",
        )
    # No run row was created for a hard pre-flight failure.
    assert snapshot_module.list_runs(registry_conn, 10) == []
