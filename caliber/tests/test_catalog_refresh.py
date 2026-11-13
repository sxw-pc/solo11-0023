"""Incremental catalog refresh: snapshot generation, reuse, staleness, degradation."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core import dataset_loader
from app.core.dataset_loader import (
    MAX_READ_ATTEMPTS,
    SNAPSHOT_FILENAME,
    load_catalog,
)
from app.core.snapshot import get_run, perform_run, snapshots_for_run
from app.errors import DatasetError, NotFoundError
from cli.main import main


def _write_csv(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def _make_datasets(tmp_path: Path) -> Path:
    directory = tmp_path / "datasets"
    directory.mkdir()
    _write_csv(directory, "alpha.csv", "id,value\n1,10\n2,20\n3,30\n")
    _write_csv(directory, "beta.csv", "id,label\n1,x\n2,y\n")
    _write_csv(directory, "gamma.csv", "day,amount\n2026-01-01,1.5\n2026-01-02,2.5\n")
    return directory


def _snapshot_path(directory: Path) -> Path:
    return directory / SNAPSHOT_FILENAME


def _read_snapshot(directory: Path) -> dict:
    return json.loads(_snapshot_path(directory).read_text(encoding="utf-8"))


def _catalog_dump(catalog) -> dict:
    dump = {}
    for meta in catalog.list_metadata():
        dump[meta.key] = {
            "meta": meta.to_dict(),
            "rows": catalog.preview(meta.key, 100)["rows"],
        }
    return dump


def _run_outcome(conn, run_id: int) -> dict:
    outcome = {}
    for snapshot in snapshots_for_run(conn, run_id):
        outcome[snapshot["metric_key"]] = {
            "verdict": snapshot["verdict"],
            "values": {
                impl["impl_id"]: impl["values"]
                for impl in snapshot["result"]["implementations"]
            },
        }
    return outcome


def test_first_load_degrades_and_generates_snapshot(tmp_path: Path):
    directory = _make_datasets(tmp_path)
    catalog = load_catalog(directory)
    try:
        report = catalog.refresh_report
        assert report["mode"] == "full"
        assert report["degraded"] is True
        assert report["degrade_reason"] == "snapshot_missing"
        assert sorted(report["added"]) == ["alpha", "beta", "gamma"]
        assert report["reused"] == []
        assert report["snapshot_written"] is True
        assert report["snapshot_storage"] == "json_file"
        assert report["storage_rationale"]
    finally:
        catalog.close()
    payload = _read_snapshot(directory)
    assert payload["version"] == 1
    assert payload["directory"] == str(directory.resolve())
    assert set(payload["files"]) == {"alpha.csv", "beta.csv", "gamma.csv"}
    entry = payload["files"]["alpha.csv"]
    assert entry["status"] == "active"
    assert entry["row_count"] == 3
    assert [column["name"] for column in entry["columns"]] == ["id", "value"]


def test_second_load_reuses_everything_and_does_not_rewrite(tmp_path: Path):
    directory = _make_datasets(tmp_path)
    load_catalog(directory).close()
    snapshot_path = _snapshot_path(directory)
    first_bytes = snapshot_path.read_bytes()
    first_mtime_ns = snapshot_path.stat().st_mtime_ns

    catalog = load_catalog(directory)
    try:
        report = catalog.refresh_report
        assert report["mode"] == "incremental"
        assert report["degraded"] is False
        assert sorted(report["reused"]) == ["alpha", "beta", "gamma"]
        assert report["reparsed"] == []
        assert report["added"] == []
        assert report["snapshot_written"] is False
        # 复用的数据仍然可用且正确
        assert catalog.require("alpha").row_count == 3
        rows = catalog.preview("alpha", 10)["rows"]
        assert sum(row["value"] for row in rows) == 60
        gamma = catalog.require("gamma")
        assert {column.name: column.type for column in gamma.columns} == {
            "day": "date",
            "amount": "float",
        }
    finally:
        catalog.close()
    assert snapshot_path.read_bytes() == first_bytes
    assert snapshot_path.stat().st_mtime_ns == first_mtime_ns


def test_only_modified_file_is_reparsed_and_results_match_full_refresh(tmp_path: Path):
    directory = _make_datasets(tmp_path)
    load_catalog(directory).close()
    _write_csv(directory, "beta.csv", "id,label\n1,x\n2,y\n3,z\n")

    catalog = load_catalog(directory)
    try:
        report = catalog.refresh_report
        assert report["reparsed"] == ["beta"]
        assert sorted(report["reused"]) == ["alpha", "gamma"]
        assert report["snapshot_written"] is True
        assert catalog.require("beta").row_count == 3
        incremental_dump = _catalog_dump(catalog)
    finally:
        catalog.close()

    fresh = load_catalog(directory, use_snapshot=False)
    try:
        assert _catalog_dump(fresh) == incremental_dump
    finally:
        fresh.close()


def test_same_size_mtime_only_change_is_detected(tmp_path: Path):
    directory = _make_datasets(tmp_path)
    load_catalog(directory).close()
    alpha = directory / "alpha.csv"
    # 内容变化但字节数完全一致（30 -> 40），只有 mtime 不同
    alpha.write_text("id,value\n1,10\n2,20\n3,40\n", encoding="utf-8")

    catalog = load_catalog(directory)
    try:
        assert catalog.refresh_report["reparsed"] == ["alpha"]
        rows = catalog.preview("alpha", 10)["rows"]
        assert rows[2]["value"] == 40
    finally:
        catalog.close()


def test_pure_mtime_touch_still_triggers_reparse(tmp_path: Path):
    directory = _make_datasets(tmp_path)
    load_catalog(directory).close()
    alpha = directory / "alpha.csv"
    stat = alpha.stat()
    os.utime(alpha, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    catalog = load_catalog(directory)
    try:
        assert catalog.refresh_report["reparsed"] == ["alpha"]
        assert sorted(catalog.refresh_report["reused"]) == ["beta", "gamma"]
    finally:
        catalog.close()


def test_deleted_file_is_stale_excluded_and_recorded(tmp_path: Path):
    directory = _make_datasets(tmp_path)
    load_catalog(directory).close()
    (directory / "beta.csv").unlink()

    catalog = load_catalog(directory)
    try:
        report = catalog.refresh_report
        assert "beta" not in catalog.datasets
        with pytest.raises(NotFoundError):
            catalog.require("beta")
        assert sorted(report["reused"]) == ["alpha", "gamma"]
        assert len(report["stale"]) == 1
        stale = report["stale"][0]
        assert stale["key"] == "beta"
        assert stale["file"] == "beta.csv"
        assert stale["reason"] == "file_deleted"
        assert stale["action"] == "excluded_from_run"
        assert stale["recorded_at"]
        assert report["snapshot_written"] is True
    finally:
        catalog.close()

    payload = _read_snapshot(directory)
    entry = payload["files"]["beta.csv"]
    assert entry["status"] == "stale"
    assert entry["disposal"]["reason"] == "file_deleted"
    assert entry["disposal"]["action"] == "excluded_from_run"
    assert "rows" not in entry

    # 再跑一次：处置记录不重复、快照稳定不重写
    before = _snapshot_path(directory).read_bytes()
    catalog2 = load_catalog(directory)
    try:
        report2 = catalog2.refresh_report
        assert report2["snapshot_written"] is False
        assert [item["key"] for item in report2["stale"]] == ["beta"]
    finally:
        catalog2.close()
    assert _snapshot_path(directory).read_bytes() == before


def test_missing_snapshot_degrades_to_full_scan(tmp_path: Path):
    directory = _make_datasets(tmp_path)
    load_catalog(directory).close()
    _snapshot_path(directory).unlink()

    catalog = load_catalog(directory)
    try:
        report = catalog.refresh_report
        assert report["degraded"] is True
        assert report["degrade_reason"] == "snapshot_missing"
        assert report["mode"] == "full"
        assert sorted(report["added"]) == ["alpha", "beta", "gamma"]
        assert report["snapshot_written"] is True
        assert catalog.require("alpha").row_count == 3
    finally:
        catalog.close()
    assert _snapshot_path(directory).is_file()


def test_corrupt_snapshot_degrades_and_regenerates(tmp_path: Path):
    directory = _make_datasets(tmp_path)
    load_catalog(directory).close()
    _snapshot_path(directory).write_text("{ definitely not json", encoding="utf-8")

    catalog = load_catalog(directory)
    try:
        report = catalog.refresh_report
        assert report["degraded"] is True
        assert report["degrade_reason"] == "snapshot_corrupt"
        assert report["snapshot_written"] is True
        assert catalog.require("beta").row_count == 2
    finally:
        catalog.close()
    payload = _read_snapshot(directory)
    assert payload["version"] == 1


def test_snapshot_disabled_loads_full_without_writing(tmp_path: Path):
    directory = _make_datasets(tmp_path)
    catalog = load_catalog(directory, use_snapshot=False)
    try:
        report = catalog.refresh_report
        assert report["snapshot_enabled"] is False
        assert report["mode"] == "full"
        assert report["degraded"] is False
        assert report["snapshot_written"] is False
    finally:
        catalog.close()
    assert not _snapshot_path(directory).exists()


def test_file_replaced_during_read_is_reloaded_conservatively(
    tmp_path: Path, monkeypatch
):
    directory = _make_datasets(tmp_path)
    original = dataset_loader._read_csv
    bumped: list[str] = []

    def flaky_read(path: Path):
        result = original(path)
        if path.name == "alpha.csv" and not bumped:
            bumped.append(path.name)
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        return result

    monkeypatch.setattr(dataset_loader, "_read_csv", flaky_read)
    catalog = load_catalog(directory)
    try:
        report = catalog.refresh_report
        assert bumped == ["alpha.csv"]
        assert any("alpha.csv" in warning for warning in report["warnings"])
        assert catalog.require("alpha").row_count == 3
    finally:
        catalog.close()

    # 快照记录的是稳定后的签名，下一次运行正常命中复用
    payload = _read_snapshot(directory)
    entry = payload["files"]["alpha.csv"]
    stat = (directory / "alpha.csv").stat()
    assert (entry["size"], entry["mtime_ns"]) == (stat.st_size, stat.st_mtime_ns)
    catalog2 = load_catalog(directory)
    try:
        assert "alpha" in catalog2.refresh_report["reused"]
    finally:
        catalog2.close()


def test_perpetually_unstable_file_aborts_load(tmp_path: Path, monkeypatch):
    directory = _make_datasets(tmp_path)
    original = dataset_loader._read_csv
    counter = {"ns": 1_700_000_000_000_000_000}

    def always_bump(path: Path):
        result = original(path)
        if path.name == "alpha.csv":
            counter["ns"] += 1_000_000_000
            os.utime(path, ns=(counter["ns"], counter["ns"]))
        return result

    monkeypatch.setattr(dataset_loader, "_read_csv", always_bump)
    with pytest.raises(DatasetError, match="反复修改"):
        load_catalog(directory)
    assert counter["ns"] >= 1_700_000_000_000_000_000 + MAX_READ_ATTEMPTS * 1_000_000_000
    assert not _snapshot_path(directory).exists()


def test_snapshot_write_failure_is_non_fatal(tmp_path: Path, monkeypatch):
    directory = _make_datasets(tmp_path)

    def failing_replace(*args, **kwargs):
        raise OSError("read-only directory")

    monkeypatch.setattr(dataset_loader.os, "replace", failing_replace)
    catalog = load_catalog(directory)
    try:
        report = catalog.refresh_report
        assert catalog.require("alpha").row_count == 3
        assert report["snapshot_written"] is False
        assert any("快照写入失败" in warning for warning in report["warnings"])
    finally:
        catalog.close()


def test_run_summary_records_catalog_refresh(
    registry_conn, fixture_tree: Path, sql_roots
):
    datasets_dir = fixture_tree / "datasets"
    catalog = load_catalog(datasets_dir)
    try:
        run_id = perform_run(
            registry_conn,
            ["active_users"],
            catalog,
            sql_roots,
            datasets_dir=datasets_dir,
            timeout_seconds=2.0,
            max_rows=100,
        )
    finally:
        catalog.close()
    refresh = get_run(registry_conn, run_id)["summary"]["catalog_refresh"]
    assert refresh["snapshot_storage"] == "json_file"
    assert refresh["mode"] == "full"
    assert refresh["degraded"] is True
    assert "JSON" in refresh["storage_rationale"]

    catalog2 = load_catalog(datasets_dir)
    try:
        run_id2 = perform_run(
            registry_conn,
            ["active_users"],
            catalog2,
            sql_roots,
            datasets_dir=datasets_dir,
            timeout_seconds=2.0,
            max_rows=100,
        )
    finally:
        catalog2.close()
    refresh2 = get_run(registry_conn, run_id2)["summary"]["catalog_refresh"]
    assert refresh2["mode"] == "incremental"
    assert sorted(refresh2["reused"]) == ["orders_a", "orders_b", "users_a", "users_b"]
    assert refresh2["snapshot_written"] is False
    # 复用与全量结果一致
    assert _run_outcome(registry_conn, run_id2) == _run_outcome(registry_conn, run_id)


def test_incremental_run_after_change_matches_full_refresh(
    registry_conn, fixture_tree: Path, sql_roots
):
    datasets_dir = fixture_tree / "datasets"
    load_catalog(datasets_dir).close()
    target = datasets_dir / "users_a.csv"
    with target.open("a", encoding="utf-8") as handle:
        handle.write("1008,2026-01-08,2026-02-08,active\n")

    def _run(catalog) -> int:
        try:
            return perform_run(
                registry_conn,
                ["active_users"],
                catalog,
                sql_roots,
                datasets_dir=datasets_dir,
                timeout_seconds=2.0,
                max_rows=100,
            )
        finally:
            catalog.close()

    incremental_run = _run(load_catalog(datasets_dir))
    refresh = get_run(registry_conn, incremental_run)["summary"]["catalog_refresh"]
    assert refresh["reparsed"] == ["users_a"]
    assert sorted(refresh["reused"]) == ["orders_a", "orders_b", "users_b"]

    full_run = _run(load_catalog(datasets_dir, use_snapshot=False))
    assert _run_outcome(registry_conn, incremental_run) == _run_outcome(
        registry_conn, full_run
    )


def test_cli_run_prints_refresh_line_and_degrade_hint(
    tmp_path: Path, fixture_tree: Path, capsys
):
    db_path = tmp_path / "cli.db"
    prefix = ["--db", str(db_path), "--fixtures-dir", str(fixture_tree)]
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    capsys.readouterr()

    datasets = fixture_tree / "datasets"
    assert main(prefix + ["run", "--metrics", "active_users", "--datasets", str(datasets)]) == 0
    first = capsys.readouterr()
    assert "目录刷新" in first.out
    assert "新增 4" in first.out
    assert "snapshot_missing" in first.err
    assert "降级为全量扫描" in first.err

    assert main(prefix + ["run", "--metrics", "active_users", "--datasets", str(datasets)]) == 0
    second = capsys.readouterr()
    assert "复用 4" in second.out
    assert "未重写" in second.out
    assert "snapshot_missing" not in second.err


def test_cli_run_reports_deleted_dataset(tmp_path: Path, fixture_tree: Path, capsys):
    db_path = tmp_path / "cli-stale.db"
    prefix = ["--db", str(db_path), "--fixtures-dir", str(fixture_tree)]
    assert main(prefix + ["import-metrics", "--dir", str(fixture_tree / "metrics")]) == 0
    datasets = fixture_tree / "datasets"
    extra = datasets / "extra.csv"
    extra.write_text("id,note\n1,hello\n", encoding="utf-8")
    assert main(prefix + ["run", "--metrics", "active_users", "--datasets", str(datasets)]) == 0
    capsys.readouterr()

    extra.unlink()
    assert main(prefix + ["run", "--metrics", "active_users", "--datasets", str(datasets)]) == 0
    output = capsys.readouterr()
    assert "失效 1" in output.out
    assert "extra" in output.err
    assert "已排除" in output.err
