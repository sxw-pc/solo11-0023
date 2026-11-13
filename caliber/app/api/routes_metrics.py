"""Read-only metric repository API."""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request

from ..core import versioning
from ..core.registry import MetricRegistry
from ..errors import CaliberError

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


@bp.post("/preview")
def preview_metrics():
    """Read-only pre-publish impact preview for a directory of metric YAML.

    Never writes metric versions. High-risk verdicts are reported in the
    payload; the HTTP layer does not enforce strict mode here.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise CaliberError("请求体必须是 JSON 对象")
    raw_dir = payload.get("metrics_dir")
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        raise CaliberError("metrics_dir 必须是非空字符串")
    config = current_app.config["CALIBER"]
    directory = Path(raw_dir).expanduser().resolve()
    extra = [
        Path(item).expanduser().resolve()
        for item in payload.get("impls_dirs", [])
        if isinstance(item, str)
    ]
    roots = config.merged_sql_roots(*extra)
    previews = MetricRegistry(g.db, roots).preview_directory(directory)
    high_risk = [item["key"] for item in previews if item.get("high_risk")]
    return jsonify(
        {
            "previews": previews,
            "count": len(previews),
            "high_risk_count": len(high_risk),
            "high_risk_keys": high_risk,
            "read_only": True,
        }
    )
