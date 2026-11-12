"""Read-only dataset discovery API."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..core.dataset_loader import load_catalog

bp = Blueprint("api_datasets", __name__, url_prefix="/api/datasets")


def _catalog():
    config = current_app.config["CALIBER"]
    return load_catalog(config.datasets_dir)


@bp.get("")
def list_datasets():
    catalog = _catalog()
    try:
        items = [item.to_dict() for item in catalog.list_metadata()]
    finally:
        catalog.close()
    return jsonify({"datasets": items, "count": len(items)})


@bp.get("/<key>")
def get_dataset(key: str):
    catalog = _catalog()
    try:
        meta = catalog.require(key).to_dict()
    finally:
        catalog.close()
    return jsonify({"dataset": meta})


@bp.get("/<key>/preview")
def preview_dataset(key: str):
    try:
        limit = int(request.args.get("limit", "10"))
    except ValueError:
        return jsonify({"error": "invalid_query", "message": "limit 必须是整数"}), 400
    catalog = _catalog()
    try:
        payload = catalog.preview(key, limit)
    finally:
        catalog.close()
    return jsonify(payload)
