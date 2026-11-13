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
from .windowing import (
    DEFAULT_POLICY,
    DEFAULT_WINDOW_SIZE,
    STATUS_LABELS,
    execution_metadata,
    resolve_snapshot_window,
)

FORMATS = ("json", "md", "html")


def build_report(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = get_run(conn, run_id)
    snapshots = snapshots_for_run(conn, run_id)
    window_meta = run["summary"].get("window")
    if not isinstance(window_meta, dict):
        window_meta = execution_metadata(DEFAULT_POLICY, DEFAULT_WINDOW_SIZE,
                                         source="report_default")
    metrics: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    counts = {"consistent": 0, "inconsistent": 0, "partial": 0}
    window_counts = {status: 0 for status in STATUS_LABELS}
    degraded_count = 0
    keys: set[str] = set()
    for snapshot in snapshots:
        result = snapshot["result"]
        definition = result.get("metric", {})
        key = snapshot["metric_key"]
        keys.add(key)
        counts[snapshot["verdict"]] = counts.get(snapshot["verdict"], 0) + 1
        window, window_source = resolve_snapshot_window(
            conn,
            snapshot,
            policy=window_meta.get("policy", DEFAULT_POLICY),
            size=window_meta.get("size", DEFAULT_WINDOW_SIZE),
        )
        if window:
            window_counts[window["status"]] = window_counts.get(window["status"], 0) + 1
            if window["degraded"]:
                degraded_count += 1
            windows.append(
                {
                    "metric_key": key,
                    "name": definition.get("name", key),
                    **window,
                    "source": window_source,
                }
            )
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
                "window": window,
                "window_source": window_source,
            }
        )
    drift_events: list[dict[str, Any]] = []
    for key in sorted(keys):
        drift_events.extend(detect_drift(conn, key))
    return {
        "report_version": "1.0",
        "generated_at": utc_now(),
        "run": run,
        "window_policy": {
            "policy": window_meta.get("policy", DEFAULT_POLICY),
            "size": window_meta.get("size", DEFAULT_WINDOW_SIZE),
            "unit": window_meta.get("unit", "runs"),
            "selection_reason": window_meta.get("selection_reason"),
            "switch_point": window_meta.get("switch_point"),
            "source": window_meta.get("source"),
        },
        "summary": {
            "metric_count": len(metrics),
            **counts,
            "drift_count": len(drift_events),
            "window_count": len(windows),
            **{f"window_{status}": count for status, count in window_counts.items()},
            "window_degraded": degraded_count,
        },
        "metrics": metrics,
        "windows": windows,
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
        "| 指标数 | consistent | inconsistent | partial | 漂移事件 |",
        "| --- | --- | --- | --- | --- |",
        f"| {summary['metric_count']} | {summary['consistent']} | "
        f"{summary['inconsistent']} | {summary['partial']} | {summary['drift_count']} |",
        "",
        "## 指标明细",
        "",
    ]
    for item in report["metrics"]:
        lines.extend(
            [
                f"### {item['name']} (`{item['key']}` v{item['version']})",
                "",
                f"- 判定：**{item['verdict']}**",
                f"- 负责人：{item['owner']}",
                f"- 单位 / 粒度：{item['unit']} / {item['grain']}",
                f"- 数据集：{', '.join(item['datasets'])}",
                f"- 维度数：{item['summary'].get('dimension_count', 0)}",
                "",
                "| 实现 | 数据集 | 行数 | 耗时(ms) |",
                "| --- | --- | --- | --- |",
            ]
        )
        for impl in item["implementations"]:
            lines.append(
                f"| {_cell(impl['impl_id'])} | {_cell(impl['source'])} | "
                f"{impl['row_count']} | {impl['duration_ms']} |"
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
    lines.extend(["## 归并窗口", ""])
    policy = report.get("window_policy", {})
    lines.extend(
        [
            f"- 策略：`{policy.get('policy')}`，窗口大小 {policy.get('size')} 个 run",
            f"- 选择理由：{policy.get('selection_reason') or '-'}",
            f"- 可切换点：{policy.get('switch_point') or '-'}",
            "",
        ]
    )
    if report["windows"]:
        lines.extend(
            [
                "| 指标 | 窗口状态 | 窗口 run 范围 | 偏离开始 run | 偏离结束 run | 窗口内判定 | 明细降级 |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for window in report["windows"]:
            start = window["deviation_start_run_id"]
            if start is None and window["deviation_start_before_window"]:
                start = "窗口前（已偏离）"
            elif start is None:
                start = "-"
            lines.append(
                f"| {_cell(window['metric_key'])} | {window['status_label']} | "
                f"{_cell(window['window_start_run_id'])} → {_cell(window['window_end_run_id'])} | "
                f"{_cell(start)} | {_cell(window['deviation_end_run_id'])} | "
                f"{window['consistent_count']} 一致 / {window['deviation_count']} 偏离 | "
                f"{'是' if window['degraded'] else '否'} |"
            )
        degraded = [window for window in report["windows"] if window["degraded"]]
        if degraded:
            lines.extend(["", f"> 提示：{degraded[0]['degraded_notice']}"])
            for window in degraded:
                lines.append(
                    f"> - `{window['metric_key']}`：快照 "
                    f"{', '.join(f'#{item}' for item in window['degraded_snapshot_ids'])}"
                )
        lines.append("")
    else:
        lines.extend(["无窗口数据。", ""])
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
        "th{background:#eaf2f8}.verdict{font-weight:700;padding:2px 8px;border-radius:4px}.consistent{background:#d9f2e2}.inconsistent{background:#f8d7da}.partial{background:#fff0c2}.meta{color:#52606d}code{background:#edf2f7;padding:1px 4px}",
        ".first_deviation,.ongoing_deviation{background:#f8d7da}.recovered{background:#d9f2e2}.warning-note{background:#fff4cc;color:#735c00;border-radius:6px;padding:9px 12px}",
        "</style></head><body>",
        "<h1>指标准星一致性报告</h1>",
        f"<p class=\"meta\">运行 #{report['run']['id']} · {html.escape(report['generated_at'])}</p>",
        f"<p>指标 {summary['metric_count']} 个；consistent {summary['consistent']}，"
        f"inconsistent {summary['inconsistent']}，partial {summary['partial']}，"
        f"漂移 {summary['drift_count']}。</p>",
        "<h2>指标明细</h2>",
    ]
    for item in report["metrics"]:
        verdict = item["verdict"]
        parts.append(
            f"<h3>{html.escape(item['name'])} <code>{html.escape(item['key'])}</code> "
            f"v{item['version']} <span class=\"verdict {verdict}\">{verdict}</span></h3>"
        )
        parts.append(
            f"<p class=\"meta\">负责人 {html.escape(item['owner'])} · 单位 "
            f"{html.escape(item['unit'])} · 粒度 {html.escape(item['grain'])} · 数据集 "
            f"{html.escape(', '.join(item['datasets']))}</p>"
        )
        parts.append("<table><tr><th>实现</th><th>数据集</th><th>行数</th><th>耗时(ms)</th></tr>")
        for impl in item["implementations"]:
            parts.append(
                f"<tr><td>{html.escape(str(impl['impl_id']))}</td>"
                f"<td>{html.escape(str(impl['source']))}</td><td>{impl['row_count']}</td>"
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
    parts.append("<h2>归并窗口</h2>")
    policy = report.get("window_policy", {})
    parts.append(
        f"<p class=\"meta\">策略 <code>{html.escape(str(policy.get('policy')))}</code> · "
        f"窗口 {html.escape(str(policy.get('size')))} 个 run · "
        f"{html.escape(str(policy.get('selection_reason') or ''))}</p>"
    )
    if report["windows"]:
        parts.append(
            "<table><tr><th>指标</th><th>窗口状态</th><th>窗口 run 范围</th>"
            "<th>偏离开始 run</th><th>偏离结束 run</th><th>窗口内判定</th><th>明细降级</th></tr>"
        )
        for window in report["windows"]:
            start = window["deviation_start_run_id"]
            if start is None and window["deviation_start_before_window"]:
                start = "窗口前（已偏离）"
            elif start is None:
                start = "-"
            parts.append(
                f"<tr><td><code>{html.escape(window['metric_key'])}</code></td>"
                f"<td><span class=\"verdict {window['status']}\">"
                f"{html.escape(window['status_label'])}</span></td>"
                f"<td>{html.escape(_cell(window['window_start_run_id']))} → "
                f"{html.escape(_cell(window['window_end_run_id']))}</td>"
                f"<td>{html.escape(_cell(start))}</td>"
                f"<td>{html.escape(_cell(window['deviation_end_run_id']))}</td>"
                f"<td>{window['consistent_count']} 一致 / {window['deviation_count']} 偏离</td>"
                f"<td>{'是' if window['degraded'] else '否'}</td></tr>"
            )
        parts.append("</table>")
        degraded = [window for window in report["windows"] if window["degraded"]]
        if degraded:
            parts.append(
                f"<p class=\"warning-note\">{html.escape(degraded[0]['degraded_notice'])}</p>"
            )
    else:
        parts.append("<p>无窗口数据。</p>")
    parts.append("<h2>漂移事件</h2>")
    if report["drift_events"]:
        parts.append("<ul>")
        for event in report["drift_events"]:
            parts.append(
                f"<li>{html.escape(event['metric_key'])}：快照 {event['from_snapshot_id']} → "
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
