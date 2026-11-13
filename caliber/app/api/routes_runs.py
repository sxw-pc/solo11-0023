"""Run-trigger and run-history API."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, g, jsonify, request

from ..core.dataset_loader import load_catalog
from ..core.snapshot import get_run, list_runs, perform_run, snapshots_for_run
from ..core.windowing import normalize_policy, normalize_size
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


def _policy(payload: dict[str, Any], default: str) -> str:
    nested = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    value = payload.get("window_policy", nested.get("policy", default))
    if not isinstance(value, str):
        raise CaliberError("window_policy 必须是字符串")
    return normalize_policy(value)


def _window_size(payload: dict[str, Any], default: int) -> int:
    nested = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    value = payload.get("window_size", nested.get("size", default))
    return normalize_size(value)


@bp.post("")
def create_run():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise CaliberError("请求体必须是 JSON 对象")
    config = current_app.config["CALIBER"]
    keys = _metric_keys(payload)
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
            window_policy=_policy(payload, config.window_policy),
            window_size=_window_size(payload, config.window_size),
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
