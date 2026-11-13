"""JSON, Markdown and standalone HTML report rendering."""
from __future__ import annotations

import html
import json
import sqlite3
from typing import Any

from ..audit import list_audit
from ..errors import ReportError
from ..models import utc_now
from .snapshot import detect_drift, get_run, snapshots_for_run

FORMATS = ("json", "md", "html")


def build_report(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = get_run(conn, run_id)
    snapshots = snapshots_for_run(conn, run_id)
    metrics: list[dict[str, Any]] = []
    counts = {"consistent": 0, "inconsistent": 0, "partial": 0}
    degraded_counts = {"timeout": 0, "row_limit": 0}
    keys: set[str] = set()
    for snapshot in snapshots:
        result = snapshot["result"]
        definition = result.get("metric", {})
        key = snapshot["metric_key"]
        keys.add(key)
        degraded = snapshot["verdict"] == "degraded" or bool(
            snapshot["summary"].get("degraded")
        )
        reason = snapshot["summary"].get("degraded_reason")
        if snapshot["verdict"] in counts:
            counts[snapshot["verdict"]] += 1
        elif degraded:
            # Reserved non-comparison verdict: counted separately so it can
            # never be read as a consistency conclusion.
            if reason in degraded_counts:
                degraded_counts[reason] += 1
        metrics.append(
            {
                "key": key,
                "name": definition.get("name", key),
                "owner": definition.get("owner", ""),
                "description": definition.get("description", ""),
                "unit": definition.get("unit", ""),
                "grain": definition.get("grain", ""),
                "version": snapshot["metric_version"],
                "verdict": snapshot["verdict"],
                "status": "degraded" if degraded else "completed",
                "degraded": degraded,
                "degraded_reason": reason,
                "degradation": result.get("degraded") if degraded else None,
                "datasets": snapshot["datasets"],
                "impl_ids": snapshot["impl_ids"],
                "summary": snapshot["summary"],
                "dimensions": result.get("dimensions", []),
                "diffs": result.get("diffs", []),
                "implementations": result.get("implementations", []),
                "tolerances": {
                    "abs": result.get("abs_tolerance"),
                    "rel": result.get("rel_tolerance"),
                },
                "snapshot_id": snapshot["id"],
            }
        )
    drift_events: list[dict[str, Any]] = []
    for key in sorted(keys):
        drift_events.extend(detect_drift(conn, key))
    run_summary = run.get("summary") or {}
    return {
        "report_version": "1.0",
        "generated_at": utc_now(),
        "run": run,
        "summary": {
            "metric_count": len(metrics),
            **counts,
            "degraded": degraded_counts["timeout"] + degraded_counts["row_limit"],
            "degraded_timeout": degraded_counts["timeout"],
            "degraded_row_limit": degraded_counts["row_limit"],
            "drift_count": len(drift_events),
            # Mirrors the run's own record so report consumers get the limit
            # scope and degradation markers without a second query.
            "execution": run_summary.get("execution", {}),
        },
        "metrics": metrics,
        "drift_events": drift_events,
        "audit": list_audit(conn, 50),
    }


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)


def _cell(value: Any) -> str:
    if value is None:
        return "缺失"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    summary = report["summary"]
    lines = [
        "# 指标准星一致性报告",
        "",
        f"- 运行 ID：{run['id']}",
        f"- 状态：{run['status']}",
        f"- 开始时间：{run['started_at']}",
        f"- 结束时间：{run.get('finished_at') or '-'}",
        f"- 数据集目录：`{run['datasets_dir']}`",
        f"- 容差：绝对 {run['tolerances'].get('abs')} / 相对 {run['tolerances'].get('rel')}",
        "",
        "## 总览",
        "",
        "| 指标数 | consistent | inconsistent | partial | 超时降级 | 截断降级 | 漂移事件 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        f"| {summary['metric_count']} | {summary['consistent']} | "
        f"{summary['inconsistent']} | {summary['partial']} | "
        f"{summary['degraded_timeout']} | {summary['degraded_row_limit']} | "
        f"{summary['drift_count']} |",
        "",
        "## 指标明细",
        "",
    ]
    for item in report["metrics"]:
        if item.get("degraded"):
            degradation = item.get("degradation") or {}
            impl_id = degradation.get("failed_impl_id") or "-"
            reason_text = (
                "超时" if item["degraded_reason"] == "timeout" else "行数截断"
            )
            lines.extend(
                [
                    f"### {item['name']} (`{item['key']}` v{item['version']})",
                    "",
                    f"- 判定：**degraded**（{reason_text}，实现 `{impl_id}`）",
                    f"- 原因：{degradation.get('message', '')}",
                ]
            )
            if item["degraded_reason"] == "row_limit":
                lines.append(
                    f"- 行数：超过上限 {degradation.get('max_rows')}，"
                    f"截断前实际读取 {degradation.get('observed_rows')} 行"
                    + ("" if degradation.get("row_count_exact") else "（下界）")
                )
            elif item["degraded_reason"] == "timeout":
                lines.append(f"- 超时阈值：{degradation.get('timeout_seconds')}s")
            lines.extend(
                [
                    "- 说明：该指标未产生比对结论，下游汇总必须按 `degraded` 标记排除。",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    f"### {item['name']} (`{item['key']}` v{item['version']})",
                    "",
                    f"- 判定：**{item['verdict']}**",
                ]
            )
        lines.extend(
            [
                f"- 负责人：{item['owner']}",
                f"- 单位 / 粒度：{item['unit']} / {item['grain']}",
                f"- 数据集：{', '.join(item['datasets'])}",
                f"- 维度数：{item['summary'].get('dimension_count', 0)}",
                "",
                "| 实现 | 数据集 | 状态 | 行数 | 耗时(ms) |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for impl in item["implementations"]:
            lines.append(
                f"| {_cell(impl['impl_id'])} | {_cell(impl['source'])} | "
                f"{_cell(impl.get('status', 'ok'))} | {impl['row_count']} | "
                f"{impl['duration_ms']} |"
            )
        lines.append("")
        if item["diffs"]:
            lines.extend(
                [
                    "**不一致维度**",
                    "",
                    "| 维度 | 类型 | " + " | ".join(item["impl_ids"]) + " | 最大差异 |",
                    "| --- | --- | " + " | ".join("---" for _ in item["impl_ids"]) + " | --- |",
                ]
            )
            for diff in item["diffs"]:
                values = " | ".join(
                    _cell(diff["values"].get(impl_id)) for impl_id in item["impl_ids"]
                )
                lines.append(
                    f"| {_cell(diff['dimension'])} | {diff['kind']} | {values} | "
                    f"{_cell(diff.get('max_delta'))} |"
                )
            lines.append("")
    lines.extend(["## 漂移事件", ""])
    if report["drift_events"]:
        for event in report["drift_events"]:
            lines.append(
                f"- `{event['metric_key']}`：快照 {event['from_snapshot_id']} → "
                f"{event['to_snapshot_id']}，{event['message']}（{event['detected_at']}）"
            )
    else:
        lines.append("未检测到 consistent → inconsistent 漂移事件。")
    lines.extend(["", "## 审计信息", "", "| 时间 | 动作 | 对象 | 状态 |", "| --- | --- | --- | --- |"])
    for audit in report["audit"]:
        lines.append(
            f"| {_cell(audit['created_at'])} | {_cell(audit['action'])} | "
            f"{_cell(audit['entity'])} | {_cell(audit['status'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_html(report: dict[str, Any]) -> str:
    summary = report["summary"]
    parts = [
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        "<title>指标准星一致性报告</title><style>",
        "body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;max-width:1080px;margin:32px auto;padding:0 20px;color:#17212b;background:#f7f9fc;line-height:1.6}",
        "h1,h2,h3{color:#0f3d5c}table{border-collapse:collapse;width:100%;background:#fff;margin:12px 0}th,td{border:1px solid #d7e0e8;padding:7px 9px;text-align:left}",
        "th{background:#eaf2f8}.verdict{font-weight:700;padding:2px 8px;border-radius:4px}.consistent{background:#d9f2e2}.inconsistent{background:#f8d7da}.partial{background:#fff0c2}.degraded{background:#e3e8ef}.meta{color:#52606d}.warn-note{background:#fff4cc;border-radius:8px;padding:9px 12px;color:#9a6700}code{background:#edf2f7;padding:1px 4px}",
        "</style></head><body>",
        "<h1>指标准星一致性报告</h1>",
        f"<p class=\"meta\">运行 #{report['run']['id']} · {html.escape(report['generated_at'])}</p>",
        f"<p>指标 {summary['metric_count']} 个；consistent {summary['consistent']}，"
        f"inconsistent {summary['inconsistent']}，partial {summary['partial']}，"
        f"超时降级 {summary['degraded_timeout']}，截断降级 {summary['degraded_row_limit']}，"
        f"漂移 {summary['drift_count']}。</p>",
        "<h2>指标明细</h2>",
    ]
    for item in report["metrics"]:
        verdict = item["verdict"]
        parts.append(
            f"<h3>{html.escape(item['name'])} <code>{html.escape(item['key'])}</code> "
            f"v{item['version']} <span class=\"verdict {verdict}\">{verdict}</span></h3>"
        )
        if item.get("degraded"):
            degradation = item.get("degradation") or {}
            reason_text = (
                "超时" if item["degraded_reason"] == "timeout" else "行数截断"
            )
            detail = (
                f"超时阈值 {degradation.get('timeout_seconds')}s"
                if item["degraded_reason"] == "timeout"
                else f"上限 {degradation.get('max_rows')} 行，截断前实际读取 "
                     f"{degradation.get('observed_rows')} 行"
            )
            parts.append(
                f"<p class=\"warn-note\">降级（{reason_text}）："
                f"{html.escape(str(degradation.get('message', '')))}；"
                f"{html.escape(str(detail))}。该指标未产生比对结论。</p>"
            )
        parts.append(
            f"<p class=\"meta\">负责人 {html.escape(item['owner'])} · 单位 "
            f"{html.escape(item['unit'])} · 粒度 {html.escape(item['grain'])} · 数据集 "
            f"{html.escape(', '.join(item['datasets']))}</p>"
        )
        parts.append(
            "<table><tr><th>实现</th><th>数据集</th><th>状态</th><th>行数</th>"
            "<th>耗时(ms)</th></tr>"
        )
        for impl in item["implementations"]:
            parts.append(
                f"<tr><td>{html.escape(str(impl['impl_id']))}</td>"
                f"<td>{html.escape(str(impl['source']))}</td>"
                f"<td>{html.escape(str(impl.get('status', 'ok')))}</td>"
                f"<td>{impl['row_count']}</td>"
                f"<td>{impl['duration_ms']}</td></tr>"
            )
        parts.append("</table>")
        if item["diffs"]:
            parts.append("<table><tr><th>维度</th><th>类型</th>")
            for impl_id in item["impl_ids"]:
                parts.append(f"<th>{html.escape(impl_id)}</th>")
            parts.append("<th>最大差异</th></tr>")
            for diff in item["diffs"]:
                parts.append(
                    f"<tr><td>{html.escape(str(diff['dimension']))}</td>"
                    f"<td>{html.escape(diff['kind'])}</td>"
                )
                for impl_id in item["impl_ids"]:
                    parts.append(f"<td>{html.escape(_cell(diff['values'].get(impl_id)))}</td>")
                parts.append(f"<td>{html.escape(_cell(diff.get('max_delta')))}</td></tr>")
            parts.append("</table>")
    parts.append("<h2>漂移事件</h2>")
    if report["drift_events"]:
        parts.append("<ul>")
        for event in report["drift_events"]:
            parts.append(
                f"<li>{html.escape(event['metric_key'])}："
                f"快照 {event['from_snapshot_id']} → "
                f"{event['to_snapshot_id']}，{html.escape(event['message'])}</li>"
            )
        parts.append("</ul>")
    else:
        parts.append("<p>未检测到 consistent → inconsistent 漂移事件。</p>")
    parts.append("<h2>审计信息</h2><table><tr><th>时间</th><th>动作</th><th>对象</th><th>状态</th></tr>")
    for audit in report["audit"]:
        parts.append(
            f"<tr><td>{html.escape(audit['created_at'])}</td>"
            f"<td>{html.escape(audit['action'])}</td><td>{html.escape(audit['entity'])}</td>"
            f"<td>{html.escape(audit['status'])}</td></tr>"
        )
    parts.append("</table></body></html>")
    return "".join(parts)


def export_report(report: dict[str, Any], fmt: str) -> tuple[str, str]:
    normalized = fmt.lower().lstrip(".")
    if normalized not in FORMATS:
        raise ReportError(f"不支持的报告格式: {fmt}")
    renderers = {"json": render_json, "md": render_markdown, "html": render_html}
    return normalized, renderers[normalized](report)
