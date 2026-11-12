"""Report export API."""
from __future__ import annotations

from flask import Blueprint, Response, g, request

from ..audit import log_audit
from ..core.reporter import build_report, export_report

bp = Blueprint("api_reports", __name__, url_prefix="/api/reports")

MIME = {
    "json": "application/json; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}


@bp.get("/<int:run_id>")
def get_report(run_id: int):
    report = build_report(g.db, run_id)
    fmt, payload = export_report(report, request.args.get("format", "json"))
    log_audit(g.db, "report.export", f"run:{run_id}", "ok", {"format": fmt})
    response = Response(payload, mimetype=MIME[fmt])
    disposition = "inline" if fmt == "html" else "attachment"
    response.headers["Content-Disposition"] = f'{disposition}; filename="caliber-run-{run_id}.{fmt}"'
    return response
