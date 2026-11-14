from __future__ import annotations

from pathlib import Path

import pytest

from app.core.dsl_parser import parse_metric_file, parse_metric_text
from app.errors import DslParseError

VALID = """
# 顶层注释应被 YAML 解析器忽略
key: daily_orders
name: 每日订单数
owner: data-platform
description: 按自然日统计已支付订单。
unit: 单
grain: day
impls:
  - id: impl_a
    source: orders_a
    sql_file: a.sql
  - id: impl_b
    source: orders_b
    sql_file: b.sql
"""


def test_parse_valid_definition_and_nested_impls():
    metric = parse_metric_text(VALID)
    assert metric.key == "daily_orders"
    assert metric.name == "每日订单数"
    assert metric.grain == "day"
    assert [item.id for item in metric.impls] == ["impl_a", "impl_b"]
    assert metric.impls[0].source == "orders_a"
    assert metric.impls[1].sql_file == "b.sql"
    assert "注释" not in metric.description


def test_parse_file_accepts_utf8_bom(tmp_path: Path):
    path = tmp_path / "metric.yaml"
    path.write_text(VALID, encoding="utf-8-sig")
    metric = parse_metric_file(path)
    assert metric.key == "daily_orders"


def test_parse_missing_required_field_reports_field():
    broken = VALID.replace("owner: data-platform\n", "")
    with pytest.raises(DslParseError) as caught:
        parse_metric_text(broken)
    assert caught.value.code == "dsl_field_error"
    fields = {item["field"] for item in caught.value.details}
    assert "owner" in fields


def test_parse_yaml_syntax_error_has_location():
    with pytest.raises(DslParseError) as caught:
        parse_metric_text("key: [unclosed")
    assert caught.value.code == "yaml_syntax_error"
    assert "行" in caught.value.message


def test_parse_natural_keys_list_and_string_shorthand():
    with_keys = VALID + "natural_keys:\n  - region\n  - segment\n"
    metric = parse_metric_text(with_keys)
    assert metric.natural_keys == ["region", "segment"]

    shorthand = parse_metric_text(VALID + "natural_keys: day\n")
    assert shorthand.natural_keys == ["day"]

    # 未声明时默认为空列表（运行期降级为 skipped）
    assert parse_metric_text(VALID).natural_keys == []


def test_parse_bad_natural_keys_type_is_reported():
    with pytest.raises(DslParseError) as caught:
        parse_metric_text(VALID + "natural_keys: 123\n")
    fields = {item["field"] for item in caught.value.details}
    assert "natural_keys" in fields

    with pytest.raises(DslParseError) as caught:
        parse_metric_text(VALID + "natural_keys:\n  - ''\n")
    assert any("natural_keys" in item["field"] for item in caught.value.details)

