"""Read-only metric repository API."""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from ..core import versioning

bp = Blueprint("api_metrics", __name__, url_prefix="/api/metrics")


@bp.get("")
def list_metrics():
    items = versioning.list_metrics(g.db)
    return jsonify({"metrics": items, "count": len(items)})


@bp.get("/<key>")
def get_metric(key: str):
    latest = versioning.get_latest(g.db, key)
    return jsonify({"metric": latest})


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
