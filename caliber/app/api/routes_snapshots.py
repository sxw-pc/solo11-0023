"""Snapshot traceability API."""
from __future__ import annotations

from flask import Blueprint, g, jsonify

from ..core.snapshot import get_snapshot, trace_snapshot

bp = Blueprint("api_snapshots", __name__, url_prefix="/api/snapshots")


@bp.get("/<int:snapshot_id>")
def snapshot_detail(snapshot_id: int):
    return jsonify({"snapshot": get_snapshot(g.db, snapshot_id)})


@bp.get("/<int:snapshot_id>/trace")
def snapshot_trace(snapshot_id: int):
    """Resolve which definition/SQL the snapshot actually executed.

    Legacy snapshots degrade to a version-number lookup with explicit
    notices instead of failing.
    """
    return jsonify({"trace": trace_snapshot(g.db, snapshot_id)})
