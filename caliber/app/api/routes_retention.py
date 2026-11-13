"""Local retention-window API: dry-run statistics and batched pruning."""
from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, g, jsonify, request

from ..audit import log_audit
from ..core import retention
from ..errors import CaliberError

bp = Blueprint("api_retention", __name__, url_prefix="/api/retention")


def _settings(payload: dict[str, Any]) -> tuple[int, int]:
    config = current_app.config["CALIBER"]
    raw_days = payload.get("retention_days", config.retention_days)
    if raw_days is None:
        raise CaliberError(
            "未配置保留窗口：请在请求中传入 retention_days，"
            "或设置 CALIBER_RETENTION_DAYS；默认不裁剪"
        )
    days = retention.validate_retention_days(raw_days)
    raw_batch = payload.get("batch_size", config.prune_batch_size)
    return days, retention.validate_batch_size(raw_batch)


@bp.post("/plan")
def plan():
    """Read-only dry run: report what a prune would delete, delete nothing."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        raise CaliberError("请求体必须是 JSON 对象")
    days, batch_size = _settings(payload)
    stats = retention.plan_pruning(g.db, days, batch_size=batch_size)
    return jsonify({"plan": stats.to_dict()})


@bp.post("/prune")
def prune():
    """Execute the batched deletion behind a retention window."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        raise CaliberError("请求体必须是 JSON 对象")
    days, batch_size = _settings(payload)
    stats = retention.prune(g.db, days, batch_size=batch_size)
    log_audit(
        g.db,
        "retention.prune",
        "retention",
        "ok",
        {
            "retention_days": days,
            "cutoff": stats.cutoff,
            "runs": len(stats.pruned_run_ids),
            "snapshots": stats.snapshots,
            "audit_records": stats.audit_records,
            "batches": stats.batches,
            "run_ids": stats.pruned_run_ids,
        },
    )
    return jsonify({"result": stats.to_dict()})
