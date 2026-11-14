"""Flask application factory for the local 指标准星 workbench."""
from __future__ import annotations

import html
import sqlite3

from flask import Flask, g, jsonify, render_template, request

from .api.routes_datasets import bp as datasets_bp
from .api.routes_metrics import bp as metrics_bp
from .api.routes_reports import bp as reports_bp
from .api.routes_runs import bp as runs_bp
from .audit import record_error
from .config import Config
from .core import versioning
from .core.dataset_loader import load_catalog
from .core.reporter import build_report
from .core.snapshot import list_runs, list_snapshots, snapshots_for_run
from .db import connect, init_db, table_counts
from .errors import CaliberError
from .models import utc_now


def create_app(config: Config | None = None) -> Flask:
    cfg = config or Config.from_env()
    cfg.ensure_dirs()
    init_db(cfg.db_path, min_sqlite_version=cfg.min_sqlite_version).close()

    app = Flask(__name__)
    app.config["CALIBER"] = cfg
    app.json.ensure_ascii = False
    for blueprint in (metrics_bp, runs_bp, datasets_bp, reports_bp):
        app.register_blueprint(blueprint)

    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = connect(cfg.db_path, min_sqlite_version=cfg.min_sqlite_version)
        return g.db

    @app.before_request
    def open_db() -> None:
        get_db()

    @app.teardown_appcontext
    def close_db(_exc: BaseException | None) -> None:
        if (conn := g.pop("db", None)) is not None:
            conn.close()

    @app.get("/")
    def index():
        metrics = versioning.list_metrics(get_db())
        runs = list_runs(g.db, 5)
        datasets = []
        try:
            catalog = load_catalog(cfg.datasets_dir)
            try:
                datasets = [item.to_dict() for item in catalog.list_metadata()]
            finally:
                catalog.close()
        except CaliberError:
            datasets = []
        return render_template(
            "index.html",
            metrics=metrics,
            runs=runs,
            datasets=datasets,
            counts=table_counts(g.db),
            now=utc_now(),
        )

    @app.get("/metrics")
    def metrics_page():
        items = versioning.list_metrics(g.db)
        return render_template("metrics.html", metrics=items, metric=None, versions=[], snapshots=[])

    @app.get("/metrics/<key>")
    def metric_detail(key: str):
        latest = versioning.get_latest(g.db, key)
        versions = versioning.list_versions(g.db, key)
        snapshots = list_snapshots(g.db, key, 10)
        return render_template(
            "metrics.html", metrics=versioning.list_metrics(g.db),
            metric=latest, versions=versions, snapshots=snapshots,
        )

    @app.get("/runs")
    def runs_page():
        runs = []
        for run in list_runs(g.db, 30):
            run["snapshots"] = [
                {
                    "metric_key": item["metric_key"],
                    "verdict": item["verdict"],
                    "metric_version": item["metric_version"],
                }
                for item in snapshots_for_run(g.db, run["id"])
            ]
            runs.append(run)
        return render_template("runs.html", runs=runs, metrics=versioning.list_metrics(g.db))

    @app.get("/reports/<int:run_id>")
    def report_page(run_id: int):
        return render_template("report.html", report=build_report(g.db, run_id))

    def wants_json() -> bool:
        return request.path.startswith("/api/") or request.args.get("format") == "json"

    @app.errorhandler(CaliberError)
    def handle_caliber_error(exc: CaliberError):
        try:
            record_error(get_db(), f"http.{request.method}", request.path, exc)
        except Exception:
            pass
        if wants_json():
            return jsonify(exc.to_dict()), exc.http_status
        message = html.escape(exc.message)
        return (
            f"<!doctype html><meta charset='utf-8'><title>指标准星</title>"
            f"<h1>请求无法完成</h1><p>{message}</p><p><a href='/'>返回首页</a></p>",
            exc.http_status,
        )

    @app.errorhandler(404)
    def handle_404(_exc):
        if wants_json():
            return jsonify({"error": "not_found", "message": "资源不存在"}), 404
        return (
            "<!doctype html><meta charset='utf-8'><title>指标准星</title>"
            "<h1>404</h1><p>页面不存在。</p><p><a href='/'>返回首页</a></p>",
            404,
        )

    @app.errorhandler(500)
    def handle_500(exc):
        try:
            record_error(get_db(), f"http.{request.method}", request.path, exc)
        except Exception:
            pass
        if wants_json():
            return jsonify({"error": "internal_error", "message": "服务器内部错误"}), 500
        return (
            "<!doctype html><meta charset='utf-8'><title>指标准星</title>"
            "<h1>500</h1><p>服务器内部错误，详情见本地审计日志。</p>",
            500,
        )

    return app


def run_local(config: Config | None = None) -> None:
    cfg = config or Config.from_env()
    if cfg.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("只允许监听回环地址")
    app = create_app(cfg)
    app.run(host=cfg.host, port=cfg.port, debug=False, use_reloader=False)


__all__ = ["create_app", "run_local"]
