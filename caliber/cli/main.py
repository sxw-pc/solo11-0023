"""Command-line interface for 指标准星."""
from __future__ import annotations

import argparse
import json
import sys
import sqlite3
from pathlib import Path

from app import run_local
from app.audit import log_audit, record_error
from app.config import Config
from app.core.dataset_loader import load_catalog
from app.core.registry import MetricRegistry
from app.core.reporter import build_report, export_report
from app.core.snapshot import get_run, list_runs, perform_run, snapshots_for_run
from app.db import init_db
from app.errors import CaliberError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="caliber", description="指标准星：离线指标定义与一致性校验工作台")
    parser.add_argument("--db", type=Path, help="SQLite 仓库路径")
    parser.add_argument("--fixtures-dir", type=Path, help="fixture 根目录")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动本地 HTTP 服务")
    serve.add_argument("--port", type=int, default=None, help="监听端口（仅回环地址）")
    serve.set_defaults(handler=_serve)

    imp = sub.add_parser("import-metrics", help="导入指标 YAML")
    imp.add_argument("--dir", type=Path, required=True, help="指标 YAML 目录")
    imp.add_argument("--impls-dir", type=Path, action="append", default=[], help="额外受信任 SQL 目录，可重复")
    imp.set_defaults(handler=_import_metrics)

    run = sub.add_parser("run", help="执行多实现一致性比对")
    run.add_argument("--metrics", help="逗号分隔的指标 key；省略表示全部")
    run.add_argument("--datasets", type=Path, help="CSV 数据集目录")
    run.add_argument("--abs-tol", type=float, default=None, help="绝对容差")
    run.add_argument("--rel-tol", type=float, default=None, help="相对容差")
    run.set_defaults(handler=_run)

    report = sub.add_parser("report", help="导出比对报告")
    report.add_argument("--last", action="store_true", help="使用最近一次运行")
    report.add_argument("--run-id", type=int, help="指定运行 ID")
    report.add_argument("--format", choices=("json", "md", "html"), default="md")
    report.add_argument("--view", choices=("all", "top", "dimension"), default="all",
                        help="维度裁剪视图：全部 / 前 N 个 / 指定维度")
    report.add_argument("--top", type=int, default=None,
                        help="top 视图展示的差异维度数（默认 10）")
    report.add_argument("--dimension", help="dimension 视图的目标维度名")
    report.add_argument("--output", type=Path, help="输出文件；省略则打印到标准输出")
    report.set_defaults(handler=_report)
    return parser


def _config(args: argparse.Namespace) -> Config:
    config = Config.from_env()
    if args.fixtures_dir:
        fixtures = args.fixtures_dir.expanduser().resolve()
        config.fixtures_dir = fixtures
        config.datasets_dir = fixtures / "datasets"
        config.metrics_dir = fixtures / "metrics"
        config.sql_roots = (fixtures / "impls",)
    if args.db:
        config.db_path = args.db.expanduser().resolve()
        config.data_dir = config.db_path.parent
    config.ensure_dirs()
    return config


def _serve(args: argparse.Namespace, config: Config, _conn: sqlite3.Connection) -> int:
    if args.port is not None:
        config.port = int(args.port)
    print(f"指标准星本地服务: http://{config.host}:{config.port}/", flush=True)
    run_local(config)
    return 0


def _import_metrics(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    directory = args.dir.expanduser().resolve()
    extra: list[Path] = [item.expanduser().resolve() for item in args.impls_dir]
    sibling = directory.parent / "impls"
    if sibling.is_dir():
        extra.append(sibling)
    roots = config.merged_sql_roots(*extra)
    summary = MetricRegistry(conn, roots).import_directory(directory)
    for item in summary.imported:
        print(f"导入 {item.key} v{item.version}（{item.name}）")
    for error in summary.errors:
        print(f"失败 {error['file']}: [{error['error']}] {error['message']}", file=sys.stderr)
    print(f"完成：成功 {len(summary.imported)}，失败 {len(summary.errors)}")
    return 0 if summary.ok else 1


def _run(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    datasets_dir = (args.datasets or config.datasets_dir).expanduser().resolve()
    keys = [item.strip() for item in args.metrics.split(",") if item.strip()] if args.metrics else None
    catalog = load_catalog(datasets_dir)
    try:
        run_id = perform_run(
            conn,
            keys,
            catalog,
            config.sql_roots,
            datasets_dir=datasets_dir,
            timeout_seconds=config.eval_timeout_seconds,
            max_rows=config.max_result_rows,
            abs_tolerance=config.abs_tolerance if args.abs_tol is None else args.abs_tol,
            rel_tolerance=config.rel_tolerance if args.rel_tol is None else args.rel_tol,
        )
    finally:
        catalog.close()
    run = get_run(conn, run_id)
    print(f"运行 #{run_id} 完成：{json.dumps(run['summary'], ensure_ascii=False)}")
    for snapshot in snapshots_for_run(conn, run_id):
        print(f"- {snapshot['metric_key']} v{snapshot['metric_version']}: {snapshot['verdict']} (diff={snapshot['summary'].get('diff_count', 0)})")
    print(f"导出报告: python -m app report --run-id {run_id} --format md")
    return 0


def _report(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    run_id = args.run_id
    if args.last or run_id is None:
        runs = list_runs(conn, 1)
        if not runs:
            raise CaliberError("还没有可导出的运行记录")
        run_id = int(runs[0]["id"])
    report = build_report(
        conn, run_id, view=args.view, top=args.top, dimension=args.dimension
    )
    fmt, content = export_report(report, args.format)
    log_audit(
        conn,
        "report.export",
        f"run:{run_id}",
        "ok",
        {"format": fmt, "via": "cli", "view": report["view"]["mode"]},
    )
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        print(f"报告已写入 {output}")
    else:
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def _audit_failure(conn: sqlite3.Connection | None, command: str, exc: Exception) -> None:
    if conn is None:
        return
    try:
        record_error(conn, f"cli.{command}", command, exc)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        config = _config(args)
        conn = init_db(config.db_path)
        return int(args.handler(args, config, conn))
    except CaliberError as exc:
        print(f"错误 [{exc.code}]: {exc.message}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False, indent=2), file=sys.stderr)
        _audit_failure(conn, str(args.command), exc)
        return 1
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # defensive boundary: never fail silently
        print(f"内部错误: {exc}", file=sys.stderr)
        _audit_failure(conn, str(args.command), exc)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
