"""独立只读安全门禁：import 与 run 共用同一套 SQL 判定逻辑。

架构取舍（同时写入运行元信息 ``gate_meta``）：

* 选择**独立模块**而不是塞进执行器内部。导入侧和运行侧必须执行完全一致的
  策略，独立模块让策略只有一份实现，执行器保持瘦身后的调用方角色；两处入口
  的一致性由「同一模块 + 内容哈希缓存键」保证，而不是靠约定。
* 缓存以 SQL 文本的 SHA-256 为键（配合文件路径上下文），同一批次内相同内容
  只解析一次；导入后文件被替换会得到不同哈希，运行前必然重新校验，从而拦住
  「导入时合法、运行前被掉包成 DROP」的场景。
* 降级策略：调用方拿不到带真实 schema 的只读连接时（例如导入现场没有数据
  目录），门禁退化为「语句前缀 + 括号结构」模式，仍能识别
  INSERT/UPDATE/DELETE/DROP/PRAGMA/ATTACH 等非只读语句（含
  ``WITH ... DELETE`` 形态），但无法做表级越权判定；决策会显式标注
  ``mode="degraded"`` 并记录原因，运行侧拿到 catalog 后始终执行完整校验。

判定不依赖关键字正则硬匹配：写操作以 ``EXPLAIN`` 字节码中的写游标/写操作码
为准，表名通过 rootpage 与 ``sqlite_master`` 映射得到；前缀只用于归类
PRAGMA/ATTACH 这类没有写操作码的控制语句，以及无连接降级场景。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..errors import UnsafeSQLError

# 只读 SELECT 永远不会出现 OpenWrite；其余命名写操作码在 SELECT 中只会以
# p4=None 的临时表（OpenEphemeral）形态出现，因此 p4 为 None 时不计违规。
WRITE_OPCODES = {
    "OpenWrite", "Clear", "Delete", "Insert", "IdxDelete",
    "Destroy", "CreateBtree", "SetCookie", "Write",
}
NAME_BEARING_WRITE_OPCODES = WRITE_OPCODES - {"OpenWrite"}

MASTER_TOKENS = (
    "sqlite_master", "sqlite_schema",
    "sqlite_temp_master", "sqlite_temp_schema",
)

READ_KEYWORDS = {"SELECT", "WITH"}
WITH_TERMINAL_KEYWORDS = {
    "SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE",
}
WRITE_PREFIX_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE"}
DDL_PREFIX_KEYWORDS = {"CREATE", "DROP", "ALTER"}
MAINTENANCE_PREFIX_KEYWORDS = {"VACUUM", "REINDEX", "ANALYZE"}
CONTROL_PREFIX_KEYWORDS = {
    "PRAGMA", "ATTACH", "DETACH", "BEGIN", "COMMIT", "END", "ROLLBACK",
    "SAVEPOINT", "RELEASE", "LOAD_EXTENSION", "RESET",
}

# 以下规则即使命中白名单也不放行：它们意味着无法证明语句安全性。
NEVER_WHITELISTABLE = {"unsafe_empty", "unsafe_multistatement", "explain_error"}

_FIRST_TOKEN = re.compile(r"[A-Za-z]+")
_WORD_TOKEN = re.compile(r"[A-Za-z_]+")


def _strip_sql(sql: str) -> str:
    """去掉注释与引号内容，保留语句结构（括号/分号/关键字）。"""
    out: list[str] = []
    index, length = 0, len(sql)
    while index < length:
        char, nxt = sql[index], sql[index + 1: index + 2]
        if char == "-" and nxt == "-":
            newline = sql.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
        elif char == "/" and nxt == "*":
            end = sql.find("*/", index + 2)
            if end < 0:
                raise UnsafeSQLError("SQL 块注释未闭合")
            index = end + 2
        elif char in {"'", '"', "`"}:
            quote = char
            index += 1
            while index < length:
                if sql[index] == quote:
                    if sql[index + 1: index + 2] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise UnsafeSQLError("SQL 字符串或标识符引号未闭合")
            out.append(" '' ")
        else:
            out.append(char)
            index += 1
    return "".join(out)


def outer_statement_keyword(sql: str) -> str | None:
    """返回外层语句关键字（结构性扫描，支持 WITH ... 终止关键字）。"""
    text = _strip_sql(sql)
    match = _FIRST_TOKEN.search(text)
    if match is None:
        return None
    keyword = match.group(0).upper()
    if keyword != "WITH":
        return keyword
    for token in _WORD_TOKEN.finditer(text):
        word = token.group(0).upper()
        prefix = text[: token.start()]
        if prefix.count("(") - prefix.count(")") == 0 and word in WITH_TERMINAL_KEYWORDS:
            return word
    return "WITH"


def classify_statement(sql: str) -> str:
    """把语句归类为 SELECT/INSERT/.../PRAGMA/ATTACH 等类型标签。"""
    keyword = outer_statement_keyword(sql)
    if keyword is None:
        return "UNKNOWN"
    if keyword in (
        READ_KEYWORDS | WRITE_PREFIX_KEYWORDS | DDL_PREFIX_KEYWORDS
        | MAINTENANCE_PREFIX_KEYWORDS | CONTROL_PREFIX_KEYWORDS
    ):
        return "SELECT" if keyword in READ_KEYWORDS else keyword
    return "UNKNOWN"


@dataclass(slots=True)
class Violation:
    rule: str
    message: str
    statement_type: str | None = None
    table: str | None = None
    evidence: str | None = None
    whitelisted: bool = False
    whitelist_entry: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "message": self.message,
            "statement_type": self.statement_type,
            "table": self.table,
            "evidence": self.evidence,
            "whitelisted": self.whitelisted,
            "whitelist_entry": dict(self.whitelist_entry) if self.whitelist_entry else None,
        }


@dataclass(slots=True)
class _RawFinding:
    """内容派生的原始检查结果（不含 scope/白名单策略），可安全缓存复用。"""

    sql_sha256: str
    statement_type: str
    mode: str
    read_tables: list[str]
    write_tables: list[str]
    master_accessed: bool
    violations: list[Violation]
    degraded_reason: str | None


@dataclass(slots=True)
class GateDecision:
    allowed: bool
    sql_sha256: str
    statement_type: str
    mode: str  # "full"（EXPLAIN 完整校验）或 "degraded"（仅前缀结构）
    read_tables: list[str] = field(default_factory=list)
    write_tables: list[str] = field(default_factory=list)
    master_accessed: bool = False
    violations: list[Violation] = field(default_factory=list)
    whitelisted: list[Violation] = field(default_factory=list)
    degraded_reason: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_violation(self) -> Violation | None:
        return self.violations[0] if self.violations else None

    def rejection_reason(self) -> dict[str, Any] | None:
        primary = self.primary_violation
        if primary is None:
            return None
        return {
            "rule": primary.rule,
            "message": primary.message,
            "statement_type": primary.statement_type,
            "table": primary.table,
            "evidence": primary.evidence,
            "all_violations": [item.to_dict() for item in self.violations],
        }

    def audit_summary(self) -> dict[str, Any]:
        return {
            "statement_type": self.statement_type,
            "mode": self.mode,
            "read_tables": self.read_tables,
            "write_tables": self.write_tables,
            "master_accessed": self.master_accessed,
            "degraded_reason": self.degraded_reason,
            "whitelisted": [item.to_dict() for item in self.whitelisted],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "sql_sha256": self.sql_sha256,
            "statement_type": self.statement_type,
            "mode": self.mode,
            "read_tables": self.read_tables,
            "write_tables": self.write_tables,
            "master_accessed": self.master_accessed,
            "violations": [item.to_dict() for item in self.violations],
            "whitelisted": [item.to_dict() for item in self.whitelisted],
            "degraded_reason": self.degraded_reason,
            "context": self.context,
        }


@dataclass(slots=True)
class AccessScope:
    """指标实现声明的访问范围；``tables`` 为 None 表示未声明 → 全量校验。"""

    tables: frozenset[str] | None = None
    sqlite_master: bool = False

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> "AccessScope | None":
        if raw is None:
            return None
        tables = raw.get("tables")
        allowed: frozenset[str] | None
        if tables is None:
            allowed = None
        elif isinstance(tables, Sequence) and all(isinstance(item, str) for item in tables):
            allowed = frozenset(item.strip() for item in tables if item.strip())
        else:
            raise UnsafeSQLError("access.tables 必须是字符串数组")
        master = raw.get("sqlite_master", False)
        if not isinstance(master, bool):
            raise UnsafeSQLError("access.sqlite_master 必须是布尔值")
        return cls(tables=allowed, sqlite_master=master)


class SqlGate:
    """只读安全门禁。无连接实例自动工作在 degraded 模式。"""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection
        # 门禁自己创建并持有的连接（from_catalog），close() 时负责释放；
        # 外部传入的连接由外部管理，门禁不关闭。
        self._owns_connection = False
        # 缓存键 = (sql 哈希, 模式)；缓存只存内容派生的原始检查结果，
        # scope/白名单等调用方策略每次重新套用，避免缓存串策略。
        self._cache: dict[tuple[str, str], _RawFinding] = {}

    @classmethod
    def from_catalog(cls, catalog: Any) -> "SqlGate":
        """基于数据集 catalog 建独立只读连接，门禁自行管理其生命周期。"""
        source = catalog.readonly_connection()
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        source.backup(connection)
        connection.execute("PRAGMA query_only = ON")
        gate = cls(connection)
        gate._owns_connection = True
        return gate

    def close(self) -> None:
        if self._owns_connection and self.connection is not None:
            self.connection.close()
            self.connection = None
            self._owns_connection = False

    def __enter__(self) -> "SqlGate":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def mode(self) -> str:
        return "full" if self.connection is not None else "degraded"

    def inspect_text(
        self,
        sql: str,
        *,
        access_scope: AccessScope | Mapping[str, Any] | None = None,
        allowlist: Sequence[Mapping[str, Any]] = (),
        context: Mapping[str, Any] | None = None,
    ) -> GateDecision:
        if not isinstance(sql, str) or not sql.strip():
            raise UnsafeSQLError("SQL 不能为空")
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        scope = access_scope if isinstance(access_scope, AccessScope) else AccessScope.from_raw(
            access_scope if isinstance(access_scope, Mapping) else None
        )
        cached = self._cache.get((digest, self.mode))
        if cached is None:
            cached = self._inspect_raw(sql, digest)
            self._cache[(digest, self.mode)] = cached
        decision = GateDecision(
            allowed=False,
            sql_sha256=cached.sql_sha256,
            statement_type=cached.statement_type,
            mode=cached.mode,
            read_tables=list(cached.read_tables),
            write_tables=list(cached.write_tables),
            master_accessed=cached.master_accessed,
            violations=list(cached.violations),
            degraded_reason=cached.degraded_reason,
        )
        self._apply_policy(decision, scope, allowlist)
        if context:
            decision.context = dict(context)
        return decision

    def inspect_file(
        self,
        path: Any,
        *,
        access_scope: AccessScope | Mapping[str, Any] | None = None,
        allowlist: Sequence[Mapping[str, Any]] = (),
        context: Mapping[str, Any] | None = None,
    ) -> GateDecision:
        text = path.read_text(encoding="utf-8-sig")
        file_context = {"sql_file": str(path)}
        if context:
            file_context.update(context)
        return self.inspect_text(
            text,
            access_scope=access_scope,
            allowlist=allowlist,
            context=file_context,
        )

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #

    def _inspect_raw(self, sql: str, digest: str) -> _RawFinding:
        stripped = _strip_sql(sql).strip()
        statement_type = classify_statement(sql)
        finding = _RawFinding(
            sql_sha256=digest,
            statement_type=statement_type,
            mode=self.mode,
            read_tables=[],
            write_tables=[],
            master_accessed=False,
            violations=[],
            degraded_reason=None,
        )
        if not stripped:
            finding.violations.append(
                Violation("unsafe_empty", "SQL 不能为空", statement_type)
            )
            return finding
        body = stripped[:-1].rstrip() if stripped.endswith(";") else stripped
        if ";" in body:
            finding.violations.append(
                Violation(
                    "unsafe_multistatement",
                    "不允许一次执行多条 SQL 语句",
                    statement_type,
                )
            )
            return finding

        if statement_type == "UNKNOWN":
            finding.violations.append(
                Violation(
                    "non_readonly_statement",
                    "只允许 SELECT 或 WITH 查询",
                    statement_type=statement_type,
                    evidence="statement_prefix",
                )
            )
        elif statement_type != "SELECT":
            finding.violations.append(
                Violation(
                    "non_readonly_statement",
                    f"非只读语句类型: {statement_type}",
                    statement_type=statement_type,
                    evidence="statement_prefix",
                )
            )

        if self.connection is None:
            finding.mode = "degraded"
            finding.degraded_reason = (
                "调用方未提供带 schema 的只读连接，仅执行语句前缀/结构校验；"
                "表级越权与字节码写操作判定在运行侧补做"
            )
            return finding

        self._inspect_with_explain(sql, finding)
        return finding

    def _inspect_with_explain(self, sql: str, finding: _RawFinding) -> None:
        conn = self.connection
        assert conn is not None
        try:
            plan = conn.execute("EXPLAIN " + sql).fetchall()
            query_plan = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        except sqlite3.Error as exc:
            # 无法生成执行计划即无法证明只读，失败关闭（且不接受白名单豁免）。
            finding.violations.append(
                Violation(
                    "explain_error",
                    f"无法生成执行计划: {exc}",
                    statement_type=finding.statement_type,
                    evidence=str(exc),
                )
            )
            return

        root_map: dict[int, str] = {
            int(row[1]): str(row[0])
            for row in conn.execute(
                "SELECT name, rootpage FROM sqlite_master "
                "WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        known_names = set(root_map.values())
        read_tables: set[str] = set()
        write_tables: set[str] = set()
        write_cursor_open = False

        for row in plan:
            opcode = row["opcode"]
            p2, p4 = row["p2"], row["p4"]
            if opcode == "OpenRead":
                name = root_map.get(int(p2))
                if name:
                    read_tables.add(name)
            elif opcode == "OpenWrite":
                write_cursor_open = True
                name = root_map.get(int(p2))
                if name:
                    write_tables.add(name)
            elif opcode in NAME_BEARING_WRITE_OPCODES and isinstance(p4, str) and p4 in known_names:
                write_tables.add(p4)

        for row in query_plan:
            detail = str(row["detail"]).lower()
            if any(token in detail for token in MASTER_TOKENS):
                finding.master_accessed = True
                break

        finding.read_tables = sorted(read_tables)
        finding.write_tables = sorted(write_tables)

        if write_cursor_open or write_tables:
            evidence = "EXPLAIN 写操作码: " + ", ".join(
                row["opcode"]
                for row in plan
                if row["opcode"] in WRITE_OPCODES
                and (row["opcode"] == "OpenWrite" or row["p4"] is not None)
            )
            finding.violations.append(
                Violation(
                    "write_operation",
                    f"执行计划包含写操作，目标表: {', '.join(finding.write_tables) or '未知'}",
                    statement_type=finding.statement_type,
                    table=finding.write_tables[0] if finding.write_tables else None,
                    evidence=evidence,
                )
            )
        if finding.master_accessed:
            finding.violations.append(
                Violation(
                    "sqlite_master_access",
                    "未经授权访问 sqlite_master/sqlite_schema 系统表",
                    statement_type=finding.statement_type,
                    table="sqlite_master",
                    evidence="EXPLAIN QUERY PLAN",
                )
            )

    def _apply_policy(
        self,
        decision: GateDecision,
        scope: AccessScope | None,
        allowlist: Sequence[Mapping[str, Any]],
    ) -> None:
        # 访问范围：老 YAML 未声明 access 时 scope 为 None，降级为全量校验，不报错。
        if scope is not None and scope.tables is not None:
            for name in [*decision.read_tables, *decision.write_tables]:
                if name not in scope.tables:
                    decision.violations.append(
                        Violation(
                            "table_out_of_scope",
                            f"表 {name} 不在实现声明的访问范围内",
                            statement_type=decision.statement_type,
                            table=name,
                            evidence="access.tables",
                        )
                    )
        # sqlite_master：原始检查默认按违规记录；实现显式声明
        # access.sqlite_master=true 时撤销该违规（白名单是另一条放行通道）。
        if scope is not None and scope.sqlite_master:
            decision.violations = [
                item for item in decision.violations
                if item.rule != "sqlite_master_access"
            ]

        remaining: list[Violation] = []
        for violation in decision.violations:
            entry = self._match_allowlist(violation, allowlist)
            if entry is not None and violation.rule not in NEVER_WHITELISTABLE:
                violation.whitelisted = True
                violation.whitelist_entry = entry
                decision.whitelisted.append(violation)
            else:
                remaining.append(violation)
        decision.violations = remaining
        decision.allowed = not remaining

    def _match_allowlist(
        self,
        violation: Violation,
        allowlist: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any] | None:
        for entry in allowlist:
            if not isinstance(entry, Mapping):
                continue
            if "rule" in entry and str(entry["rule"]) != violation.rule:
                continue
            if "statement_type" in entry and str(entry["statement_type"]).upper() != violation.statement_type:
                continue
            if "table" in entry and violation.table is not None and str(entry["table"]) != violation.table:
                continue
            return entry
        return None


GATE_META: dict[str, Any] = {
    "module": "app.core.sql_gate",
    "architecture": "independent_module",
    "rationale": (
        "门禁做成独立模块只被 import/run 调用：两条入口共享同一份 EXPLAIN "
        "判定与白名单逻辑，避免执行器内部耦合；一致性由统一模块和内容哈希"
        "缓存保证，执行器仅保留调用与失败隔离职责"
    ),
    "cache_key": "sha256(sql_text)+mode",
    "degradation": (
        "无 schema 连接时降级为语句前缀/括号结构校验（mode=degraded），"
        "非只读语句仍被拦截，表级越权留待运行侧完整校验"
    ),
    "write_detection": (
        "EXPLAIN 写操作码（OpenWrite/Clear/Delete/Insert 等）结合 "
        "sqlite_master rootpage 表名映射；EXPLAIN QUERY PLAN 识别 sqlite_master"
    ),
}
