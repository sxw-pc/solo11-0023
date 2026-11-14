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


def normalize_header(raw: str, index: int) -> str:
    name = HEADER_RE.sub("_", raw.strip().lower()).strip("_")
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
        raise DatasetError(f"无法读取数据集: {path}: {exc}") from exc
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise DatasetError(f"数据集不是合法的 UTF-8 文件: {path}") from exc
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise DatasetError(f"CSV 解析失败: {path}: {exc}") from exc
    if not rows:
        raise DatasetError(f"空数据集: {path}")
    raw_header = [item.strip() for item in rows[0]]
    header = [normalize_header(item, index) for index, item in enumerate(raw_header)]
    duplicates = sorted({name for name in header if header.count(name) > 1})
    if duplicates:
        raise DatasetError(f"数据集存在重复列名: {', '.join(duplicates)}")
    data_rows: list[list[str]] = []
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(header):
            raise DatasetError(
                f"第 {line_no} 行列数 {len(row)} 与表头列数 {len(header)} 不一致"
            )
        data_rows.append([cell.strip() for cell in row])
    if not data_rows:
        raise DatasetError(f"数据集没有数据行: {path}")
    return encoding, header, data_rows


def _build_keys(directory: Path, paths: list[Path]) -> dict[Path, str]:
    """Assign each CSV a stable key.

    Top-level files keep their bare stem key (backwards compatible); files in
    subdirectories get a path-derived, deterministic key so that same-named
    CSVs in different subdirectories never overwrite each other.
    """
    base_keys: dict[Path, str] = {}
    for path in paths:
        stem_key = normalize_header(path.stem, 0)
        if path.parent == directory:
            base_keys[path] = stem_key
            continue
        rel = path.relative_to(directory).with_suffix("")
        parts = [normalize_header(part, 0) for part in rel.parts]
        base_keys[path] = "__".join(parts)
    used: set[str] = set()
    result: dict[Path, str] = {}
    for path in paths:
        key = base_keys[path]
        if key not in used:
            used.add(key)
            result[path] = key
            continue
        # Deterministic disambiguation for any residual collision.
        suffix = 2
        while f"{key}_{suffix}" in used:
            suffix += 1
        unique_key = f"{key}_{suffix}"
        used.add(unique_key)
        result[path] = unique_key
    return result


class DatasetCatalog:
    """Owns writable loading connections and one query-only connection."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._readonly: sqlite3.Connection | None = None
        self.datasets: dict[str, DatasetMeta] = {}
        self._by_name: dict[str, list[DatasetMeta]] = {}
        try:
            self._load_directory()
        except Exception:
            self._conn.close()
            raise

    def _discover_csv_files(self) -> list[Path]:
        return sorted(
            (path for path in self.directory.rglob("*.csv") if path.is_file()),
            key=lambda item: item.relative_to(self.directory).as_posix(),
        )

    def _load_directory(self) -> None:
        if not self.directory.is_dir():
            raise DatasetError(f"数据集目录不存在: {self.directory}")
        files = self._discover_csv_files()
        if not files:
            raise DatasetError(f"数据集目录中没有 CSV 文件: {self.directory}")
        keys = _build_keys(self.directory, files)
        for path in files:
            self._load_file(path, keys[path])
        self._conn.commit()

    def _load_file(self, path: Path, key: str) -> None:
        if key in self.datasets:
            raise DatasetError(f"数据集 key 重复: {key}")
        rel_path = path.relative_to(self.directory).as_posix()
        encoding, header, rows = _read_csv(path)
        columns_raw = list(zip(*rows))
        types = [infer_type(column) for column in columns_raw]
        columns = [DatasetColumn(name, kind) for name, kind in zip(header, types)]
        declared = {"int": "INTEGER", "float": "REAL", "date": "TEXT", "str": "TEXT"}
        definitions = ", ".join(
            f"{_quote(name)} {declared[kind]}" for name, kind in zip(header, types)
        )
        self._conn.execute(f"CREATE TABLE {_quote(key)} ({definitions})")
        placeholders = ", ".join("?" for _ in header)
        sql = f"INSERT INTO {_quote(key)} VALUES ({placeholders})"
        converted = [
            tuple(_coerce(value, types[index]) for index, value in enumerate(row))
            for row in rows
        ]
        self._conn.executemany(sql, converted)
        meta = DatasetMeta(
            key=key,
            path=str(path),
            encoding=encoding,
            row_count=len(rows),
            column_count=len(header),
            columns=columns,
            name=path.stem,
            rel_path=rel_path,
        )
        self.datasets[key] = meta
        self._by_name.setdefault(meta.name, []).append(meta)

    def list_metadata(self) -> list[DatasetMeta]:
        """Metadata in deterministic load order (relative path order)."""
        return sorted(self.datasets.values(), key=lambda item: (item.rel_path, item.key))

    def manifest(self) -> list[dict[str, Any]]:
        """Ordered dataset references representing this run's actual files."""
        return [item.reference() for item in self.list_metadata()]

    def require(self, key: str) -> DatasetMeta:
        if key not in self.datasets:
            raise NotFoundError(f"数据集不存在: {key}")
        return self.datasets[key]

    def resolve(self, source: str) -> DatasetMeta | None:
        """Resolve a metric's ``source`` reference without raising.

        Exact keys (including path-derived keys) win; a bare file stem is
        accepted only when it uniquely identifies one dataset.
        """
        if source in self.datasets:
            return self.datasets[source]
        candidates = self._by_name.get(source, [])
        if len(candidates) == 1:
            return candidates[0]
        return None

    def ambiguity(self, source: str) -> list[DatasetMeta]:
        """Datasets sharing a bare-stem name when ``source`` is ambiguous."""
        if source in self.datasets:
            return []
        return list(self._by_name.get(source, []))

    def references_for(self, sources: Iterable[str]) -> list[dict[str, Any]]:
        """Stable references for the sources actually used by a metric."""
        metas: list[DatasetMeta] = []
        seen: set[str] = set()
        for source in sources:
            meta = self.resolve(source)
            if meta is not None and meta.key not in seen:
                seen.add(meta.key)
                metas.append(meta)
        metas.sort(key=lambda item: (item.rel_path, item.key))
        return [item.reference() for item in metas]

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
