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
    keys: set[str] = set()
    for snapshot in snapshots:
        result = snapshot["result"]
        definition = result.get("metric", {})
        key = snapshot["metric_key"]
        keys.add(key)
        counts[snapshot["verdict"]] = counts.get(snapshot["verdict"], 0) + 1
        arbitration = result.get("arbitration")
        entry = {
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
        }
        # Old snapshots have neither field; do not synthesize them so old
        # reports export byte-for-byte the same structure.
        if isinstance(arbitration, dict):
            entry["arbitration"] = arbitration
        if result.get("impl_failures"):
            entry["impl_failures"] = result["impl_failures"]
        metrics.append(entry)
    arbitration_summary = _arbitration_report_summary(metrics)
    drift_events: list[dict[str, Any]] = []
    for key in sorted(keys):
        drift_events.extend(detect_drift(conn, key))
    summary_payload: dict[str, Any] = {
        "metric_count": len(metrics),
        **counts,
        "drift_count": len(drift_events),
    }
    if arbitration_summary.get("enabled"):
        summary_payload["arbitration"] = arbitration_summary
    return {
        "report_version": "1.0",
        "generated_at": utc_now(),
        "run": run,
        "summary": summary_payload,
        "metrics": metrics,
        "drift_events": drift_events,
        "audit": list_audit(conn, 50),
    }


def _arbitration_report_summary(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate arbitration presence without changing reports from old runs."""
    enabled = [item for item in metrics if isinstance(item.get("arbitration"), dict)]
    if not enabled:
        return {"enabled": False}
    statuses: dict[str, int] = {}
    non_arbitrable = 0
    metrics_with_dissent = 0
    for item in enabled:
        arbitration = item["arbitration"]
        status = str(arbitration.get("status", "degraded"))
        statuses[status] = statuses.get(status, 0) + 1
        non_arbitrable += len(arbitration.get("non_arbitrable", []))
        if arbitration.get("dissenters"):
            metrics_with_dissent += 1
    first = enabled[0]["arbitration"]
    return {
        "enabled": True,
        "strategy": first.get("strategy"),
        "threshold": first.get("threshold"),
        "metrics_arbitrated": len(enabled),
        "statuses": statuses,
        "metrics_with_dissent": metrics_with_dissent,
        "non_arbitrable_impls": non_arbitrable,
        "selection": first.get("selection", {}),
    }


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)


def _append_arbitration_markdown(item: dict[str, Any], lines: list[str]) -> None:
    arbitration = item.get("arbitration")
    if not isinstance(arbitration, dict):
        _append_impl_failures_markdown(item, lines)
        return
    status = arbitration.get("status", "degraded")
    consensus = arbitration.get("consensus_group") or []
    lines.extend(
        [
            "### 跨实现仲裁",
            "",
            f"- 状态：{status}；策略：{arbitration.get('strategy')}；"
            f"共识阈值：{arbitration.get('threshold')}",
            f"- 共识组：{', '.join(consensus) if consensus else '未形成'}",
        ]
    )
    dissenters = arbitration.get("dissenters") or []
    if dissenters:
        lines.append(
            "- 异议方："
            + ", ".join(
                f"{entry['impl_id']}（分歧维度: "
                f"{', '.join(entry.get('dissent_dimensions', [])) or '无'}）"
                for entry in dissenters
            )
        )
    if arbitration.get("dissent_dimensions"):
        lines.append(f"- 异议维度：{', '.join(arbitration['dissent_dimensions'])}")
    detail = arbitration.get("max_divergence_detail") or {}
    if detail:
        lines.append(
            f"- 最大分歧：{_cell(detail.get('delta'))}（{detail.get('left')} / "
            f"{detail.get('right')} @ {detail.get('dimension')}）"
        )
    for entry in dissenters:
        lines.extend(
            [
                "",
                f"异议实现 `{entry['impl_id']}`：{entry.get('reason', '')}",
                "",
                "| 维度 | 类型 | 实现值 | 共识值 | 差值 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for row in entry.get("dissent_details", []):
            lines.append(
                f"| {_cell(row.get('dimension'))} | {_cell(row.get('kind'))} | "
                f"{_cell(row.get('impl_value'))} | {_cell(row.get('consensus_value'))} | "
                f"{_cell(row.get('delta'))} |"
            )
        lines.append("")
    minority = arbitration.get("minority") or []
    if minority:
        lines.append(
            "- 共识组内轻微偏离："
            + ", ".join(
                f"{entry['impl_id']}（{', '.join(entry.get('minority_dimensions', []))}）"
                for entry in minority
            )
        )
    degraded = arbitration.get("degraded")
    if degraded:
        lines.append(
            f"- 降级原因：{degraded.get('reason')}"
            + (f"（{degraded['detail']}）" if degraded.get("detail") else "")
        )
    non_arbitrable = arbitration.get("non_arbitrable") or []
    if non_arbitrable:
        lines.extend(
            ["", "| 不可仲裁实现 | 阶段 | 原因 |", "| --- | --- | --- |"]
        )
        for failure in non_arbitrable:
            lines.append(
                f"| {_cell(failure.get('impl_id'))} | {_cell(failure.get('stage'))} | "
                f"{_cell(failure.get('reason'))} |"
            )
    lines.append("")


def _append_impl_failures_markdown(item: dict[str, Any], lines: list[str]) -> None:
    if not item.get("impl_failures"):
        return
    lines.extend(["**不可仲裁实现**", "", "| 实现 | 阶段 | 原因 |", "| --- | --- | --- |"])
    for failure in item["impl_failures"]:
        lines.append(
            f"| {_cell(failure.get('impl_id'))} | {_cell(failure.get('stage'))} | "
            f"{_cell(failure.get('reason'))} |"
        )
    lines.append("")


def _append_arbitration_html(item: dict[str, Any], parts: list[str]) -> None:
    arbitration = item.get("arbitration")
    if not isinstance(arbitration, dict):
        return
    status = html.escape(str(arbitration.get("status", "degraded")))
    consensus = ", ".join(arbitration.get("consensus_group") or []) or "未形成"
    parts.append(
        f"<h3>跨实现仲裁 <span class=\"verdict "
        f"{'consistent' if arbitration.get('status') == 'ok' else 'partial'}\">"
        f"{status}</span></h3>"
    )
    parts.append(
        f"<p class=\"meta\">策略 {html.escape(str(arbitration.get('strategy')))} · "
        f"共识阈值 {html.escape(str(arbitration.get('threshold')))} · "
        f"共识组 {html.escape(consensus)}</p>"
    )
    dissenters = arbitration.get("dissenters") or []
    if dissenters:
        parts.append(
            "<p>异议方："
            + "；".join(
                f"<strong>{html.escape(str(entry['impl_id']))}</strong>"
                f"（分歧维度 {html.escape(', '.join(entry.get('dissent_dimensions', [])) or '无')}）"
                for entry in dissenters
            )
            + "</p>"
        )
        for entry in dissenters:
            parts.append("<table><tr><th>维度</th><th>类型</th><th>实现值</th><th>共识值</th><th>差值</th></tr>")
            for row in entry.get("dissent_details", []):
                parts.append(
                    f"<tr><td>{html.escape(str(row.get('dimension')))}</td>"
                    f"<td>{html.escape(str(row.get('kind')))}</td>"
                    f"<td>{html.escape(_cell(row.get('impl_value')))}</td>"
                    f"<td>{html.escape(_cell(row.get('consensus_value')))}</td>"
                    f"<td>{html.escape(_cell(row.get('delta')))}</td></tr>"
                )
            parts.append("</table>")
    detail = arbitration.get("max_divergence_detail") or {}
    if detail:
        parts.append(
            f"<p class=\"meta\">最大分歧 {html.escape(_cell(detail.get('delta')))}"
            f"（{html.escape(str(detail.get('left')))} / {html.escape(str(detail.get('right')))}"
            f" @ {html.escape(str(detail.get('dimension')))}）</p>"
        )
    minority = arbitration.get("minority") or []
    if minority:
        parts.append(
            "<p>共识组内轻微偏离："
            + "；".join(
                f"{html.escape(str(entry['impl_id']))}（"
                f"{html.escape(', '.join(entry.get('minority_dimensions', [])))}）"
                for entry in minority
            )
            + "</p>"
        )
    degraded = arbitration.get("degraded")
    if degraded:
        parts.append(
            f"<p class=\"meta\">降级原因：{html.escape(str(degraded.get('reason')))}</p>"
        )
    non_arbitrable = arbitration.get("non_arbitrable") or []
    if non_arbitrable:
        parts.append("<table><tr><th>不可仲裁实现</th><th>阶段</th><th>原因</th></tr>")
        for failure in non_arbitrable:
            parts.append(
                f"<tr><td>{html.escape(str(failure.get('impl_id')))}</td>"
                f"<td>{html.escape(str(failure.get('stage')))}</td>"
                f"<td>{html.escape(str(failure.get('reason')))}</td></tr>"
            )
        parts.append("</table>")


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
        _append_arbitration_markdown(item, lines)
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
        _append_arbitration_html(item, parts)
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
