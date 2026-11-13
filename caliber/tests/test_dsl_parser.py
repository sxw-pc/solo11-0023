from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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


ACCESS_VALID = """
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
    access:
      tables: [orders_a]
      sqlite_master: false
    allowlist:
      - rule: table_out_of_scope
        reason: 临时核对
  - id: impl_b
    source: orders_b
    sql_file: b.sql
"""


def test_parse_access_and_allowlist_fields():
    metric = parse_metric_text(ACCESS_VALID)
    impl_a = metric.impls[0]
    assert impl_a.access == {"tables": ["orders_a"], "sqlite_master": False}
    assert impl_a.allowlist == [
        {"rule": "table_out_of_scope", "reason": "临时核对"}
    ]
    # 未声明 access/allowlist 的老形态实现得到 None / 空列表。
    assert metric.impls[1].access is None
    assert metric.impls[1].allowlist == []
    # 往返序列化保留新字段。
    roundtrip = parse_metric_text(yaml.safe_dump(metric.to_dict()))
    assert roundtrip.impls[0].access == impl_a.access


def test_parse_bad_access_and_allowlist_shapes():
    base = ACCESS_VALID
    bad_access = base.replace(
        "      tables: [orders_a]", "      tables: not-a-list"
    )
    with pytest.raises(DslParseError) as caught:
        parse_metric_text(bad_access)
    fields = {item["field"] for item in caught.value.details}
    assert any("access.tables" in item for item in fields)

    bad_master = base.replace(
        "      sqlite_master: false", "      sqlite_master: 'yes'"
    )
    with pytest.raises(DslParseError):
        parse_metric_text(bad_master)

    bad_allowlist = base.replace(
        "      - rule: table_out_of_scope\n        reason: 临时核对",
        "      - reason: 缺少 rule",
    )
    with pytest.raises(DslParseError) as caught:
        parse_metric_text(bad_allowlist)
    assert any("rule" in item["field"] for item in caught.value.details)

    unknown_access = base.replace(
        "      sqlite_master: false",
        "      sqlite_master: false\n      unexpected: 1",
    )
    with pytest.raises(DslParseError) as caught:
        parse_metric_text(unknown_access)
    assert any("unknown" in item["code"] for item in caught.value.details)

