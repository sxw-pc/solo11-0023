"""Drill-down and failure attribution API."""
from __future__ import annotations

from flask import Blueprint, Response, g, request

from ..audit import log_audit
from ..core.drilldown import build_drilldown, export_drilldown

bp = Blueprint("api_drilldown", __name__, url_prefix="/api/drilldown")

MIME = {
    "json": "application/json; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
}


@bp.get("/")
def get_drilldown():
    run_id = request.args.get("run_id", type=int)
    chunk_size = request.args.get("chunk_size", default=500, type=int)
    report = build_drilldown(g.db, run_id=run_id, chunk_size=chunk_size)
    fmt, payload = export_drilldown(report, request.args.get("format", "json"))
    scope = f"run:{run_id}" if run_id is not None else "run:all"
    log_audit(g.db, "drilldown.export", scope, "ok", {"format": fmt, "via": "api"})
    return Response(payload, mimetype=MIME[fmt])
