"""CSV fixture loading into private in-memory SQLite databases."""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from ..errors import DatasetError, NotFoundError
from ..models import DatasetColumn, DatasetMeta

INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][+-]?\d+)?$")
HEADER_RE = re.compile(r"[^0-9A-Za-z_一-鿿]+")
UTF8_BOM = "\ufeff"


def normalize_header(raw: str, index: int) -> str:
    # 显式剥离可能残留在列名内部的 UTF-8 BOM，避免归一化后留下不可见字符
    cleaned = raw.strip().lstrip(UTF8_BOM).strip()
    name = HEADER_RE.sub("_", cleaned.lower()).strip("_")
    if not name:
        raise DatasetError(f"第 {index + 1} 列表头为空")
    if name[0].isdigit():
        name = f"col_{name}"
    return name


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def infer_type(values: Iterable[str]) -> str:
    items = [value for value in values if value not in ("", None)]
    if not items:
        return "str"
    if all(INT_RE.fullmatch(value) for value in items):
        return "int"
    if all(FLOAT_RE.fullmatch(value) for value in items):
        return "float"
    if all(_is_date(value) for value in items):
        return "date"
    return "str"


def _is_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _coerce(value: str, data_type: str) -> Any:
    if value == "":
        return None
    if data_type == "int":
        return int(value)
    if data_type == "float":
        return float(value)
    return value


def _read_csv(path: Path) -> tuple[str, list[str], list[list[str]]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DatasetError(f"数据集 {path.name} 无法读取文件: {exc}") from exc
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise DatasetError(
            f"数据集 {path.name} 不是合法的 UTF-8 文件: {exc}"
        ) from exc
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise DatasetError(f"数据集 {path.name} CSV 解析失败: {exc}") from exc
    if not rows:
        raise DatasetError(f"数据集 {path.name} 内容为空")
    raw_header = [item.strip() for item in rows[0]]
    try:
        header = [normalize_header(item, index) for index, item in enumerate(raw_header)]
    except DatasetError as exc:
        raise DatasetError(f"数据集 {path.name} {exc.message}") from exc
    duplicates = sorted({name for name in header if header.count(name) > 1})
    if duplicates:
        columns = "、".join(duplicates)
        raise DatasetError(f"数据集 {path.name} 存在重复列名: {columns}")
    data_rows: list[list[str]] = []
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(header):
            raise DatasetError(
                f"数据集 {path.name} 第 {line_no} 行列数 {len(row)} "
                f"与表头列数 {len(header)} 不一致"
            )
        data_rows.append([cell.strip() for cell in row])
    if not data_rows:
        raise DatasetError(f"数据集 {path.name} 没有数据行")
    return encoding, header, data_rows


class DatasetCatalog:
    """Owns writable loading connections and one query-only connection.

    单个数据集加载失败只会记录到 :attr:`load_errors` 并跳过该文件，不会中断
    同批其他数据集；只有整个目录不可用或没有任何数据集成功时才抛出异常。
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._readonly: sqlite3.Connection | None = None
        self.datasets: dict[str, DatasetMeta] = {}
        self.load_errors: list[dict[str, str]] = []
        try:
            self._load_directory()
        except Exception:
            self._conn.close()
            raise

    def _load_directory(self) -> None:
        if not self.directory.is_dir():
            raise DatasetError(f"数据集目录不存在: {self.directory}")
        files = sorted(self.directory.glob("*.csv"))
        if not files:
            raise DatasetError(f"数据集目录中没有 CSV 文件: {self.directory}")
        for path in files:
            self._load_file(path)
        self._conn.commit()
        if not self.datasets:
            detail = "; ".join(
                f"{item['dataset']}: {item['message']}" for item in self.load_errors
            )
            raise DatasetError(
                f"数据集目录 {self.directory.name} 中没有可加载的数据集（{detail}）",
                details={"errors": self.load_errors},
            )

    def _load_file(self, path: Path) -> None:
        key = normalize_header(path.stem, 0)
        if key in self.datasets:
            self._record_error(path, key, f"数据集 key 重复: {key}")
            return
        try:
            encoding, header, rows = _read_csv(path)
            columns_raw = list(zip(*rows))
            types = [infer_type(column) for column in columns_raw]
            columns = [
                DatasetColumn(name, kind) for name, kind in zip(header, types)
            ]
            declared = {"int": "INTEGER", "float": "REAL", "date": "TEXT", "str": "TEXT"}
            definitions = ", ".join(
                f"{_quote(name)} {declared[kind]}" for name, kind in zip(header, types)
            )
            # 先在保存点内建表并写入，失败时整体回滚，避免残留半张表污染连接
            self._conn.execute("SAVEPOINT dataset_load")
            try:
                self._conn.execute(f"CREATE TABLE {_quote(key)} ({definitions})")
                placeholders = ", ".join("?" for _ in header)
                sql = f"INSERT INTO {_quote(key)} VALUES ({placeholders})"
                converted = [
                    tuple(_coerce(value, types[index]) for index, value in enumerate(row))
                    for row in rows
                ]
                self._conn.executemany(sql, converted)
            except Exception:
                self._conn.execute("ROLLBACK TO SAVEPOINT dataset_load")
                self._conn.execute("RELEASE SAVEPOINT dataset_load")
                raise
            self._conn.execute("RELEASE SAVEPOINT dataset_load")
        except DatasetError as exc:
            self._record_error(path, key, exc.message)
            return
        except (sqlite3.Error, ValueError, OverflowError) as exc:
            # 覆盖建表/绑定阶段的所有失败（含超大整数 OverflowError），单文件隔离
            self._record_error(path, key, f"数据集落库失败: {exc}")
            return
        self.datasets[key] = DatasetMeta(
            key=key,
            path=str(path),
            encoding=encoding,
            row_count=len(rows),
            column_count=len(header),
            columns=columns,
        )

    def _record_error(self, path: Path, key: str, message: str) -> None:
        self.load_errors.append(
            {"dataset": key, "file": path.name, "path": str(path), "message": message}
        )

    def list_metadata(self) -> list[DatasetMeta]:
        return [self.datasets[key] for key in sorted(self.datasets)]

    def require(self, key: str) -> DatasetMeta:
        if key not in self.datasets:
            raise NotFoundError(f"数据集不存在: {key}")
        return self.datasets[key]

    def readonly_connection(self) -> sqlite3.Connection:
        if self._readonly is None:
            self._readonly = sqlite3.connect(":memory:")
            self._readonly.row_factory = sqlite3.Row
            self._conn.backup(self._readonly)
            self._readonly.execute("PRAGMA query_only = ON")
        return self._readonly

    def preview(self, key: str, limit: int = 10) -> dict[str, Any]:
        meta = self.require(key)
        capped = max(1, min(int(limit), 100))
        rows = self._conn.execute(
            f"SELECT * FROM {_quote(key)} LIMIT ?", (capped,)
        ).fetchall()
        return {"meta": meta.to_dict(), "rows": [dict(row) for row in rows]}

    def close(self) -> None:
        if self._readonly is not None:
            self._readonly.close()
            self._readonly = None
        self._conn.close()

    def __enter__(self) -> "DatasetCatalog":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def load_catalog(directory: str | Path) -> DatasetCatalog:
    return DatasetCatalog(directory)
