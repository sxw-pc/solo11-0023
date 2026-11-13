"""Merge-window API over existing runs/snapshots; no new tables."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from ..core.versioning import get_latest
from ..core.windowing import (
    all_metric_windows,
    execution_metadata,
    metric_window,
    normalize_policy,
    normalize_size,
)
from ..errors import CaliberError

bp = Blueprint("api_windows", __name__, url_prefix="/api/windows")


def _query_policy() -> str:
    config = current_app.config["CALIBER"]
    return normalize_policy(request.args.get("policy", config.window_policy))


def _query_size() -> int:
    config = current_app.config["CALIBER"]
    return normalize_size(request.args.get("size", config.window_size))


def _recompute_flag() -> bool:
    raw = str(request.args.get("recompute", "")).lower()
    if raw in {"", "0", "false", "no"}:
        return False
    if raw in {"1", "true", "yes"}:
        return True
    raise CaliberError("recompute 只接受 true/false")


@bp.get("")
def get_windows():
    policy, size = _query_policy(), _query_size()
    items = all_metric_windows(
        g.db, policy=policy, size=size, recompute=_recompute_flag()
    )
    return jsonify(
        {
            "policy": policy,
            "size": size,
            "unit": "runs",
            "count": len(items),
            "windows": items,
        }
    )


@bp.get("/meta")
def get_window_meta():
    config = current_app.config["CALIBER"]
    return jsonify(
        {
            "default": execution_metadata(
                config.window_policy, config.window_size, source="config"
            ),
            "policies": ["run_count"],
        }
    )


@bp.get("/<key>")
def get_metric_window(key: str):
    policy, size = _query_policy(), _query_size()
    item = metric_window(g.db, key, policy=policy, size=size,
                         recompute=_recompute_flag())
    if item.get("source") == "unavailable":
        get_latest(g.db, key)  # raises 404 for unknown metrics
        return jsonify({"policy": policy, "size": size,
                        "window": None, "message": "该指标尚无快照"})
    return jsonify({"policy": policy, "size": size, "window": item})
