"""Run-trigger and run-history API."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, g, jsonify, request

from ..core.dataset_loader import load_catalog
from ..core.snapshot import get_run, list_runs, perform_run, snapshots_for_run
from ..errors import CaliberError

bp = Blueprint("api_runs", __name__, url_prefix="/api/runs")


def _metric_keys(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("metrics", [])
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.split(",") if item.strip()]
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise CaliberError("metrics 必须是字符串数组或逗号分隔字符串")
    return list(dict.fromkeys(raw))


def _tolerance(payload: dict[str, Any], name: str, default: float) -> float:
    nested = payload.get("tolerances") if isinstance(payload.get("tolerances"), dict) else {}
    value = payload.get(name, nested.get(name, default))
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise CaliberError(f"{name} 必须是数值") from exc
    if result < 0:
        raise CaliberError(f"{name} 必须是非负数")
    return result


def _baseline_options(payload: dict[str, Any]) -> tuple[int | None, bool]:
    raw_id = payload.get("baseline_run_id")
    baseline_run_id: int | None = None
    if raw_id is not None:
        try:
            baseline_run_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise CaliberError("baseline_run_id 必须是整数") from exc
    if "compare_baseline" in payload and not isinstance(payload["compare_baseline"], bool):
        raise CaliberError("compare_baseline 必须是布尔值")
    compare_baseline = bool(payload.get("compare_baseline", True))
    return baseline_run_id, compare_baseline


@bp.post("")
def create_run():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise CaliberError("请求体必须是 JSON 对象")
    config = current_app.config["CALIBER"]
    keys = _metric_keys(payload)
    baseline_run_id, compare_baseline = _baseline_options(payload)
    datasets_dir = Path(payload.get("datasets_dir") or config.datasets_dir).expanduser().resolve()
    catalog = load_catalog(datasets_dir)
    try:
        run_id = perform_run(
            g.db,
            keys,
            catalog,
            config.sql_roots,
            datasets_dir=datasets_dir,
            timeout_seconds=config.eval_timeout_seconds,
            max_rows=config.max_result_rows,
            abs_tolerance=_tolerance(payload, "abs", config.abs_tolerance),
            rel_tolerance=_tolerance(payload, "rel", config.rel_tolerance),
            baseline_run_id=baseline_run_id,
            compare_baseline=compare_baseline,
        )
    finally:
        catalog.close()
    run = get_run(g.db, run_id)
    return jsonify({"run": run, "snapshots": snapshots_for_run(g.db, run_id)}), 201


@bp.get("")
def get_runs():
    try:
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        return jsonify({"error": "invalid_query", "message": "limit 必须是整数"}), 400
    items = list_runs(g.db, limit)
    return jsonify({"runs": items, "count": len(items)})


@bp.get("/<int:run_id>")
def get_run_detail(run_id: int):
    run = get_run(g.db, run_id)
    return jsonify({"run": run, "snapshots": snapshots_for_run(g.db, run_id)})
