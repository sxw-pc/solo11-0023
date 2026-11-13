from __future__ import annotations

import json
from pathlib import Path

from app.power.__main__ import EXIT_ARTIFACT_ERROR, main

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "power"


def test_cli_separates_real_differences_from_noise(capsys):
    code = main(
        [
            str(FIXTURES / "real_diff_a.json"),
            str(FIXTURES / "real_diff_b.json"),
            str(FIXTURES / "noise_a.json"),
            str(FIXTURES / "noise_b.json"),
            "--format",
            "json",
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    verdicts = {item["rule_key"]: item["verdict"] for item in report["rules"]}
    assert verdicts["amount_nonnegative"] == "difference"
    assert verdicts["channel_known"] == "difference"
    assert verdicts["refund_flag_ratio"] == "no_significant_difference"
    assert verdicts["discount_flag_ratio"] == "no_significant_difference"


def test_cli_marks_exact_path_for_sparse_rule(capsys):
    code = main([str(FIXTURES / "sparse_exact.json"), "--format", "md"])
    assert code == 0
    output = capsys.readouterr().out
    assert "精确检验" in output
    assert "标记 `EXACT`" in output


def test_cli_single_dataset_reports_no_control(capsys):
    code = main([str(FIXTURES / "single_dataset.json"), "--format", "json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["rules"][0]["verdict"] == "no_control"


def test_cli_missing_field_exits_with_readable_error(capsys):
    code = main([str(FIXTURES / "invalid" / "missing_total.json")])
    assert code == EXIT_ARTIFACT_ERROR
    captured = capsys.readouterr()
    assert "缺少必填字段" in captured.err
    assert "total" in captured.err
    assert captured.out == ""


def test_cli_allow_errors_still_emits_report(capsys):
    code = main(
        [
            str(FIXTURES / "invalid" / "missing_total.json"),
            str(FIXTURES / "single_dataset.json"),
            "--allow-errors",
            "--format",
            "json",
        ]
    )
    assert code == EXIT_ARTIFACT_ERROR
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["summary"]["error_count"] >= 1
    assert report["inputs"]["rule_count"] == 1


def test_cli_missing_artifact_file_exits_with_error(tmp_path: Path, capsys):
    code = main([str(tmp_path / "nope.json")])
    assert code == EXIT_ARTIFACT_ERROR
    assert "产物不存在" in capsys.readouterr().err


def test_cli_rejects_invalid_option_values():
    import pytest

    with pytest.raises(SystemExit) as excinfo:
        main([str(FIXTURES / "single_dataset.json"), "--alpha", "1.5"])
    assert excinfo.value.code == 2


def test_cli_requires_at_least_one_artifact():
    import pytest

    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_cli_is_deterministic_across_repeated_runs(capsys):
    args = [
        str(FIXTURES / "real_diff_a.json"),
        str(FIXTURES / "real_diff_b.json"),
        str(FIXTURES / "sparse_exact.json"),
        "--format",
        "json",
    ]
    assert main(args) == 0
    first = capsys.readouterr().out
    assert main(args) == 0
    second = capsys.readouterr().out
    assert first == second


def test_cli_writes_output_file(tmp_path: Path, capsys):
    target = tmp_path / "reports" / "power.json"
    code = main(
        [
            str(FIXTURES / "sparse_exact.json"),
            "--format",
            "json",
            "--output",
            str(target),
        ]
    )
    assert code == 0
    assert target.is_file()
    report = json.loads(target.read_text(encoding="utf-8"))
    assert report["summary"]["path_exact"] == 2
