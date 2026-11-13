"""CSV fixture loading into private in-memory SQLite databases."""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import threading
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


class DatasetCatalog:
    """Owns writable loading connections and one query-only connection."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()
        # check_same_thread=False: the loading connection is only ever touched
        # by catalog internals (loads are finished before any worker starts, and
        # connection backups are serialized by ``_clones_lock``).  This allows
        # worker read-only clones to be provisioned from the run-driving thread.
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._readonly: sqlite3.Connection | None = None
        # Independent read-only connections handed out to concurrent workers.
        # Clones are created up-front (the in-memory backup must run on the
        # loading thread that owns ``_conn``) and checked out per worker; the
        # catalog closes every clone on shutdown even if a worker misbehaves.
        self._idle_clones: list[sqlite3.Connection] = []
        self._active_clones: set[sqlite3.Connection] = set()
        self._clones_lock = threading.Lock()
        self.datasets: dict[str, DatasetMeta] = {}
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

    def _load_file(self, path: Path) -> None:
        key = normalize_header(path.stem, 0)
        if key in self.datasets:
            raise DatasetError(f"数据集 key 重复: {key}")
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
        self.datasets[key] = DatasetMeta(
            key=key,
            path=str(path),
            encoding=encoding,
            row_count=len(rows),
            column_count=len(header),
            columns=columns,
        )

    def list_metadata(self) -> list[DatasetMeta]:
        return [self.datasets[key] for key in sorted(self.datasets)]

    def require(self, key: str) -> DatasetMeta:
        if key not in self.datasets:
            raise NotFoundError(f"数据集不存在: {key}")
        return self.datasets[key]

    def readonly_connection(self) -> sqlite3.Connection:
        if self._readonly is None:
            self._readonly = self._make_readonly_clone()
        return self._readonly

    def _build_clone_locked(self) -> sqlite3.Connection:
        # Caller must hold ``_clones_lock``; the backup touches the shared
        # in-memory source connection and must be serialized across threads.
        clone = sqlite3.connect(":memory:", check_same_thread=False)
        clone.row_factory = sqlite3.Row
        self._conn.backup(clone)
        clone.execute("PRAGMA query_only = ON")
        return clone

    def _make_readonly_clone(self) -> sqlite3.Connection:
        # check_same_thread=False is safe here: each clone is handed to exactly
        # one worker thread for its whole lifetime, and query_only blocks writes.
        with self._clones_lock:
            return self._build_clone_locked()

    def provision_worker_connections(self, count: int) -> None:
        """Pre-create ``count`` worker clones (backup serialized by the lock)."""
        with self._clones_lock:
            current = len(self._idle_clones) + len(self._active_clones)
            for _ in range(max(0, int(count) - current)):
                self._idle_clones.append(self._build_clone_locked())

    def acquire_worker_connection(self) -> sqlite3.Connection:
        """Check out a private read-only connection for one worker."""
        with self._clones_lock:
            if self._idle_clones:
                clone = self._idle_clones.pop()
            else:
                # Fallback for callers that did not provision up-front.
                clone = self._build_clone_locked()
            self._active_clones.add(clone)
        return clone

    def release_worker_connection(self, conn: sqlite3.Connection) -> None:
        """Return one worker connection to the pool; idempotent and safe."""
        with self._clones_lock:
            if conn not in self._active_clones:
                return
            self._active_clones.discard(conn)
            self._idle_clones.append(conn)

    @property
    def active_worker_connections(self) -> int:
        """Number of clones currently checked out (used by tests)."""
        with self._clones_lock:
            return len(self._active_clones)

    @property
    def total_worker_connections(self) -> int:
        with self._clones_lock:
            return len(self._idle_clones) + len(self._active_clones)

    def preview(self, key: str, limit: int = 10) -> dict[str, Any]:
        meta = self.require(key)
        capped = max(1, min(int(limit), 100))
        rows = self._conn.execute(
            f"SELECT * FROM {_quote(key)} LIMIT ?", (capped,)
        ).fetchall()
        return {"meta": meta.to_dict(), "rows": [dict(row) for row in rows]}

    def close(self) -> None:
        with self._clones_lock:
            clones = list(self._idle_clones) + list(self._active_clones)
            self._idle_clones.clear()
            self._active_clones.clear()
        for clone in clones:
            try:
                clone.close()
            except sqlite3.Error:
                pass
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
