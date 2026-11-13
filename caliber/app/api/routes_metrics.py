"""Metric repository API: read-only queries plus audited rollback."""
from __future__ import annotations

from typing import Any

from flask import Blueprint, g, jsonify, request

from ..core import versioning
from ..errors import CaliberError

bp = Blueprint("api_metrics", __name__, url_prefix="/api/metrics")


@bp.get("")
def list_metrics():
    items = versioning.list_metrics(g.db)
    return jsonify({"metrics": items, "count": len(items)})


@bp.get("/<key>")
def get_metric(key: str):
    latest = versioning.get_latest(g.db, key)
    active = versioning.latest_active_rollback(g.db, key)
    return jsonify({"metric": latest, "active_rollback": active})


@bp.get("/<key>/versions")
def get_versions(key: str):
    versioning.get_latest(g.db, key)
    items = versioning.list_versions(g.db, key)
    return jsonify({"key": key, "versions": items, "count": len(items)})


@bp.get("/<key>/versions/<int:version>")
def get_version(key: str, version: int):
    item = versioning.get_version(g.db, key, version)
    return jsonify({"metric": item})


@bp.get("/<key>/diff")
def diff_metric(key: str):
    try:
        from_version = int(request.args.get("from", "1"))
        to_version = int(request.args.get("to", "0"))
    except ValueError:
        return jsonify({"error": "invalid_query", "message": "from/to 必须是整数"}), 400
    if to_version <= 0:
        to_version = int(versioning.get_latest(g.db, key)["version"])
    result = versioning.diff_versions(g.db, key, from_version, to_version)
    return jsonify({"diff": result})


def _rollback_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise CaliberError("请求体必须是 JSON 对象")
    return payload


@bp.post("/<key>/rollback")
def rollback_metric(key: str):
    payload = _rollback_payload()
    raw_version = payload.get("version")
    if raw_version is None:
        raise CaliberError("缺少必填字段 version（目标历史版本号）")
    try:
        target = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise CaliberError("version 必须是整数") from exc
    reason = str(payload.get("reason") or "")
    result = versioning.rollback_to_version(g.db, key, target, reason)
    return jsonify(result), 200


@bp.post("/<key>/rollback/undo")
def undo_metric_rollback(key: str):
    payload = _rollback_payload()
    reason = str(payload.get("reason") or "")
    result = versioning.undo_rollback(g.db, key, reason)
    return jsonify(result), 200


@bp.get("/<key>/rollbacks")
def list_metric_rollbacks(key: str):
    versioning.get_latest(g.db, key)
    items = versioning.list_rollbacks(g.db, key)
    return jsonify({"key": key, "rollbacks": items, "count": len(items)})
