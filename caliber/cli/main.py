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
    imp.add_argument(
        "--strict",
        action="store_true",
        help="严格模式：高风险变更（impls 缩减或 SQL 内容指纹变化）拒绝落库",
    )
    imp.set_defaults(handler=_import_metrics)

    preview = sub.add_parser(
        "preview-metrics", help="发布前影响预览（只读，不写库）"
    )
    preview.add_argument("--dir", type=Path, required=True, help="指标 YAML 目录")
    preview.add_argument("--impls-dir", type=Path, action="append", default=[], help="额外受信任 SQL 目录，可重复")
    preview.add_argument("--json", action="store_true", help="以 JSON 输出完整预览")
    preview.set_defaults(handler=_preview_metrics)

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


def _resolve_sql_roots(args: argparse.Namespace, config: Config, directory: Path):
    extra: list[Path] = [item.expanduser().resolve() for item in args.impls_dir]
    sibling = directory.parent / "impls"
    if sibling.is_dir():
        extra.append(sibling)
    return config.merged_sql_roots(*extra)


def _print_preview_human(preview: dict) -> None:
    key = preview.get("key") or "?"
    if preview.get("error"):
        print(f"  [错误] {key}: {preview['error'].get('message', '')}")
        return
    if not preview.get("exists"):
        print(
            f"  [新增] {key}: 新指标，新增 impl {', '.join(preview.get('added_impls', []))}"
        )
        return
    tag = "高风险" if preview.get("high_risk") else ("有变更" if preview.get("changed") else "无变化")
    print(
        f"  [{tag}] {key} v{preview.get('current_version')} -> "
        f"v{preview.get('candidate_version')}"
    )
    if preview.get("added_impls"):
        print(f"    新增 impl: {', '.join(preview['added_impls'])}")
    if preview.get("removed_impls"):
        print(f"    移除 impl: {', '.join(preview['removed_impls'])}")
    if preview.get("sql_fingerprint_changes"):
        ids = ", ".join(item["impl_id"] for item in preview["sql_fingerprint_changes"])
        print(f"    SQL 内容指纹变化: {ids}")
    if preview.get("grain_changed"):
        print(f"    grain 变化: {preview.get('grain_before')} -> {preview.get('grain_after')}")
    if preview.get("unit_changed"):
        print(f"    unit 变化: {preview.get('unit_before')} -> {preview.get('unit_after')}")
    print(
        f"    波及历史 run {len(preview.get('affected_run_ids', []))} 个、"
        f"快照 {preview.get('affected_snapshot_count', 0)} 条"
        f"（run_id={preview.get('affected_run_ids', [])}）"
    )
    if preview.get("risk_reasons"):
        print(f"    高风险原因: {', '.join(preview['risk_reasons'])}")
    if preview.get("degraded"):
        print("    已降级为按 key 与 impl id 集合判断")
    for warning in preview.get("warnings", []):
        print(f"    提示: {warning}")


def _import_metrics(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    directory = args.dir.expanduser().resolve()
    roots = _resolve_sql_roots(args, config, directory)
    registry = MetricRegistry(conn, roots, strict=bool(getattr(args, "strict", False)))
    summary = registry.import_directory(directory)
    for item in summary.imported:
        line = f"导入 {item.key} v{item.version}（{item.name}）"
        if item.preview and item.preview.get("high_risk"):
            line += "  [预览提示:高风险 " + ",".join(item.preview.get("risk_reasons", [])) + "]"
        elif item.preview and item.preview.get("changed"):
            line += "  [预览提示:有变更]"
        print(line)
    for error in summary.errors:
        print(f"失败 {error['file']}: [{error['error']}] {error['message']}", file=sys.stderr)
    if summary.previews:
        print("—— 发布前影响预览 ——")
        for preview in summary.previews:
            _print_preview_human(preview)
        high = summary.high_risk_count
        print(
            f"预览：{len(summary.previews)} 个文件，高风险 {high} 个"
            + ("（严格模式已拦截）" if high and registry.strict else "")
        )
    print(f"完成：成功 {len(summary.imported)}，失败 {len(summary.errors)}")
    return 0 if summary.ok else 1


def _preview_metrics(args: argparse.Namespace, config: Config, conn: sqlite3.Connection) -> int:
    directory = args.dir.expanduser().resolve()
    roots = _resolve_sql_roots(args, config, directory)
    previews = MetricRegistry(conn, roots).preview_directory(directory)
    if getattr(args, "json", False):
        print(json.dumps(previews, ensure_ascii=False, indent=2))
    else:
        print(f"发布前影响预览（只读，未写库）：{directory}")
        for preview in previews:
            _print_preview_human(preview)
        high = sum(1 for item in previews if item.get("high_risk"))
        errors = sum(1 for item in previews if item.get("error"))
        print(f"完成：预览 {len(previews)} 个文件，高风险 {high} 个，错误 {errors} 个")
    # Read-only command: never a non-zero exit merely because changes are risky.
    return 0


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
    report = build_report(conn, run_id)
    fmt, content = export_report(report, args.format)
    log_audit(conn, "report.export", f"run:{run_id}", "ok", {"format": fmt, "via": "cli"})
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
