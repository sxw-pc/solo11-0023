"""Deterministic JSON and Markdown rendering for power assessments.

The JSON output contains no timestamps and is serialized with sorted keys, so
identical inputs and settings produce byte-identical files.  This is what the
reproducibility acceptance check relies on.
"""
from __future__ import annotations

import json
from typing import Any

PATH_LABELS = {
    "exact": "精确检验",
    "normal": "正态近似",
    "descriptive": "无对照",
}
VERDICT_LABELS = {
    "difference": "显著差异",
    "no_significant_difference": "未见显著差异",
    "no_control": "无对照",
}


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _number(value: Any, digits: int = 6) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    settings = report["settings"]
    inputs = report["inputs"]
    degraded_count = sum(1 for item in report["rules"] if item["test"]["degraded"])
    lines = [
        "# 指标准星统计功效评估",
        "",
        f"- 产物数量：{inputs['artifact_count']}",
        f"- 数据集：{', '.join(inputs['datasets']) if inputs['datasets'] else '（无）'}",
        f"- 评估规则数：{inputs['rule_count']}",
        f"- alpha / 功效 / 最小可检测差异：{settings['alpha']} / "
        f"{settings['power']} / {settings['min_effect']}",
        f"- 多重比较校正：{settings['correction']}",
        "",
        "## 总览",
        "",
        "| 显著差异 | 未见显著差异 | 无对照 | 精确路径 | 正态路径 | 输入错误 | 提示 | 降级 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        f"| {summary['difference']} | {summary['no_significant_difference']} | "
        f"{summary['no_control']} | {summary['path_exact']} | "
        f"{summary['path_normal']} | {summary['error_count']} | "
        f"{summary['warning_count']} | {degraded_count} |",
        "",
    ]

    issues = report["issues"]
    if issues:
        lines.extend(["## 输入与降级提示", ""])
        for issue in issues:
            marker = "错误" if issue["severity"] == "error" else "提示"
            scope = "；".join(
                f"{key}={issue[key]}"
                for key in ("artifact", "dataset", "rule")
                if key in issue
            )
            suffix = f"（{scope}）" if scope else ""
            lines.append(f"- **[{marker}] {issue['code']}**：{issue['message']}{suffix}")
        lines.append("")

    lines.extend(["## 规则明细", ""])
    for rule in report["rules"]:
        test = rule["test"]
        path = PATH_LABELS.get(test["path"], test["path"])
        verdict = VERDICT_LABELS.get(rule["verdict"], rule["verdict"])
        lines.extend(
            [
                f"### {rule['name']} (`{rule['rule_key']}`)",
                "",
                f"- 判定：**{verdict}**（{rule['dataset_count']} 个数据集）",
                f"- 检验路径：**{path}** / {test['method']}（标记 `{rule['evidence']['path_marker']}`）",
                f"- 选择理由：{test['reason']}"
                + (f"（降级：{test['degradation']}）" if test["degraded"] else ""),
                f"- p 值：{_number(test['p_value'])}；校正后：{_number(test['adjusted_p_value'])}",
                f"- 建议每个数据集样本量：**{rule['recommendation']['recommended_total_per_dataset']}**"
                + ("（当前存在样本不足的数据集）" if rule["recommendation"]["underpowered"] else ""),
                "",
                "| 数据集 | 命中 | 通过 | 总数 | 命中率 | "
                + f"{int(float(settings['confidence_level']) * 100)}% 置信区间 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for dataset in rule["datasets"]:
            interval = dataset["confidence_interval"]
            lines.append(
                f"| {_cell(dataset['dataset'])} | {dataset['hits']} | {dataset['passes']} | "
                f"{dataset['total']} | {_number(dataset['hit_rate'])} | "
                f"[{_number(interval['low'])}, {_number(interval['high'])}] |"
            )
        lines.append("")
        if test["pairwise"] and test["path"] == "exact":
            lines.extend(
                [
                    "精确检验两两比较：",
                    "",
                    "| 数据集对 | p 值 | 校正后 p | 显著 | 枚举状态数 |",
                    "| --- | --- | --- | --- | --- |",
                ]
            )
            for pair in test["pairwise"]:
                lines.append(
                    f"| {_cell(' vs '.join(pair['datasets']))} | {_number(pair['p_value'])} | "
                    f"{_number(pair.get('adjusted_p_value'))} | "
                    f"{_number(pair.get('significant'))} | {pair.get('enumerated_states')} |"
                )
            lines.append("")
        if rule["coverage"]["absent_datasets"]:
            lines.append(
                "- 该规则未出现在数据集："
                + ", ".join(rule["coverage"]["absent_datasets"])
            )
            lines.append("")
        lines.append(rule["interpretation"])
        lines.append("")
    lines.append("")
    return "\n".join(lines)


def render(report: dict[str, Any], fmt: str) -> str:
    normalized = fmt.lower().lstrip(".")
    if normalized == "json":
        return render_json(report)
    if normalized in {"md", "markdown"}:
        return render_markdown(report)
    from .errors import PowerConfigError

    raise PowerConfigError(f"不支持的输出格式: {fmt}（可选 json / md）")
