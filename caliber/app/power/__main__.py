"""Independent CLI entry point for statistical power assessment.

Run ``python -m app.power --help`` for usage.  This module does not initialise
the SQLite repository and never touches existing run/snapshot/report formats.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import assess, load_artifacts, render
from .errors import PowerArtifactError, PowerConfigError, PowerError, PowerInputError
from .models import AssessmentSettings

EXIT_OK = 0
EXIT_INPUT_ERROR = 1
EXIT_ARTIFACT_ERROR = 2


def _fraction(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"不是数字: {value!r}") from exc
    if not 0.0 < parsed < 1.0:
        raise argparse.ArgumentTypeError(f"必须严格位于 (0, 1) 内: {value!r}")
    return parsed


def _effect(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"不是数字: {value!r}") from exc
    if not 0.0 < parsed <= 1.0:
        raise argparse.ArgumentTypeError(f"必须位于 (0, 1] 内: {value!r}")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"不是整数: {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"必须大于 0: {value!r}")
    return parsed


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"不是数字: {value!r}") from exc
    if parsed < 0.0:
        raise argparse.ArgumentTypeError(f"不能为负数: {value!r}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.power",
        description=(
            "统计功效评估：读取一份或多份规则执行产物，判断规则命中率在数据集间"
            "的差异是否显著，并给出最小样本量建议。"
        ),
    )
    parser.add_argument(
        "artifacts",
        nargs="+",
        type=Path,
        metavar="ARTIFACT",
        help="规则执行产物（.json / .jsonl），可一次传入多份做跨数据集汇总",
    )
    parser.add_argument("--alpha", type=_fraction, default=0.05, help="显著性水平，默认 0.05")
    parser.add_argument("--power", type=_fraction, default=0.80, help="目标统计功效，默认 0.80")
    parser.add_argument(
        "--min-effect",
        type=_effect,
        default=0.05,
        help="最小可检测命中率差异，默认 0.05",
    )
    parser.add_argument(
        "--min-expected-cell",
        type=_nonnegative_float,
        default=5.0,
        help="正态近似的预期格下限，低于此值倾向精确检验，默认 5",
    )
    parser.add_argument(
        "--min-group-total",
        type=_positive_int,
        default=30,
        help="样本量建议的下限，默认 30",
    )
    parser.add_argument(
        "--exact-state-budget",
        type=_positive_int,
        default=500_000,
        help="精确检验允许枚举的最大状态数，超出则显式降级，默认 500000",
    )
    parser.add_argument(
        "--no-correction",
        action="store_true",
        help="关闭跨规则 Holm 多重比较校正（默认开启）",
    )
    parser.add_argument(
        "--confidence",
        type=_fraction,
        default=0.95,
        help="单数据集命中率置信区间水平，默认 0.95",
    )
    parser.add_argument(
        "--format",
        choices=("json", "md"),
        default="md",
        help="输出格式，默认 md",
    )
    parser.add_argument("--output", type=Path, help="输出文件；省略则打印到标准输出")
    parser.add_argument(
        "--allow-errors",
        action="store_true",
        help="存在致命输入错误时仍然输出报告（报告内保留错误明细）",
    )
    return parser


def _print_issues(payload: dict, stream) -> None:
    for issue in payload.get("issues", []):
        marker = "错误" if issue.get("severity") == "error" else "提示"
        scope = "；".join(
            f"{key}={issue[key]}" for key in ("artifact", "dataset", "rule") if key in issue
        )
        suffix = f"（{scope}）" if scope else ""
        print(f"[{marker}] {issue['code']}: {issue['message']}{suffix}", file=stream)


def run(args: argparse.Namespace) -> int:
    try:
        settings = AssessmentSettings(
            alpha=args.alpha,
            power=args.power,
            min_effect=args.min_effect,
            min_expected_cell=args.min_expected_cell,
            min_group_total=args.min_group_total,
            correction="none" if args.no_correction else "holm",
            exact_state_budget=args.exact_state_budget,
            confidence_level=args.confidence,
        )
    except ValueError as exc:
        raise PowerConfigError(str(exc)) from exc
    loaded = load_artifacts(args.artifacts)
    report = assess(loaded, settings)

    errors = [issue for issue in report["issues"] if issue["severity"] == "error"]
    if errors and not args.allow_errors:
        _print_issues(report, sys.stderr)
        raise PowerArtifactError(
            f"产物存在 {len(errors)} 个致命错误，未生成评估报告；"
            "修复后重跑，或使用 --allow-errors 输出带错误明细的报告。"
        )
    if not loaded.observations:
        _print_issues(report, sys.stderr)
        raise PowerInputError(
            "没有可用于评估的有效记录"
            + ("（输入问题见上方）" if report["issues"] else "")
        )

    content = render(report, args.format)
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        print(f"评估报告已写入 {output}")
    else:
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")

    if errors:
        _print_issues(report, sys.stderr)
        return EXIT_ARTIFACT_ERROR
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except PowerError as exc:
        print(f"错误 [{exc.code}]: {exc.message}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False, indent=2), file=sys.stderr)
        if isinstance(exc, (PowerInputError, PowerConfigError)):
            return EXIT_INPUT_ERROR
        return EXIT_ARTIFACT_ERROR
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
