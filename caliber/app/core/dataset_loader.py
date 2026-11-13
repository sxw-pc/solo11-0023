"""CSV fixture loading into private in-memory SQLite databases.

Loading a whole directory of large CSVs on every run is expensive, so the
loader keeps an incremental-refresh snapshot (``.caliber_catalog.json``)
next to the datasets directory.  The snapshot records, per CSV file, the
path/size/mtime signature plus the parsed column metadata and coerced rows;
a run only re-parses files whose signature changed, reuses the cached
payload for untouched files, and records a disposal entry for deleted ones.
See ``STORAGE_RATIONALE`` for why the snapshot is a JSON sidecar instead of
a new table in the repository SQLite database.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from ..errors import DatasetError, NotFoundError
from ..models import DatasetColumn, DatasetMeta, utc_now

INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][+-]?\d+)?$")
HEADER_RE = re.compile(r"[^0-9A-Za-z_一-鿿]+")

SNAPSHOT_VERSION = 1
SNAPSHOT_FILENAME = ".caliber_catalog.json"
MAX_READ_ATTEMPTS = 3

STORAGE_RATIONALE = (
    "目录快照存储选择：跟随 datasets 目录的 JSON 文件（.caliber_catalog.json），"
    "而不是在现有 SQLite 仓库中新加表。理由："
    "(1) 快照是数据集目录的派生缓存，与指标仓库的生命周期解耦，现有数据库零迁移，"
    "旧版本没有这张表也能照常工作；"
    "(2) 多人共享同一个 datasets 目录时天然共享同一份快照，避免每个仓库各缓存一份、"
    "各自重复全量解析；"
    "(3) 并发写入通过临时文件 + 原子替换兜底，冲突的最坏结果只是缓存失效、"
    "下一次退化为全量扫描，不影响比对正确性；"
    "(4) 删除该文件即可安全重置缓存，系统会自动降级为全量扫描并重新生成。"
)

_DECLARED_TYPES = {"int": "INTEGER", "float": "REAL", "date": "TEXT", "str": "TEXT"}
_COLUMN_VALUE_TYPES = {
    "int": (int,),
    "float": (int, float),
    "date": (str,),
    "str": (str,),
}


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


def _stat_signature(path: Path) -> tuple[int, int]:
    """(size, mtime_ns) pair; any difference marks the file as changed."""
    try:
        stat = path.stat()
    except OSError as exc:
        raise DatasetError(f"无法读取数据集文件状态: {path}: {exc}") from exc
    return (stat.st_size, stat.st_mtime_ns)


def _read_csv_stable(
    path: Path, warnings: list[str]
) -> tuple[int, int, str, list[str], list[list[str]]]:
    """Read a CSV while guarding against concurrent replacement.

    The file signature is taken before and after the read.  If it changed,
    the data just read may be torn, so instead of trusting it we take the
    conservative path: reload and re-check, up to ``MAX_READ_ATTEMPTS``
    times, and record a hint in ``warnings``.  A file that never stabilises
    aborts the load rather than producing a half-valid snapshot entry.
    """
    for attempt in range(1, MAX_READ_ATTEMPTS + 1):
        before = _stat_signature(path)
        encoding, header, rows = _read_csv(path)
        after = _stat_signature(path)
        if before == after:
            if attempt > 1:
                warnings.append(
                    f"数据集文件在读取过程中被并发修改，已保守地重新加载并确认稳定: {path.name}"
                )
            return after[0], after[1], encoding, header, rows
    raise DatasetError(
        f"数据集文件在读取过程中被反复修改，无法获得稳定内容，请稍后重试: {path.name}"
    )


class CatalogSnapshot:
    """Versioned JSON snapshot of one datasets directory.

    The snapshot is a disposable cache: any problem while reading it
    degrades to a full rescan (with a recorded reason) instead of failing,
    and writes go through a temporary file plus atomic rename so a
    concurrent or crashed writer never leaves a half-written snapshot.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / SNAPSHOT_FILENAME
        self.degraded = False
        self.degrade_reason: str | None = None
        self.files: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._degrade("snapshot_missing")
            return
        except OSError:
            self._degrade("snapshot_unreadable")
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._degrade("snapshot_corrupt")
            return
        if not isinstance(payload, dict) or payload.get("version") != SNAPSHOT_VERSION:
            self._degrade("snapshot_version_mismatch")
            return
        if payload.get("directory") != str(self.directory):
            self._degrade("snapshot_directory_mismatch")
            return
        files = payload.get("files")
        if not isinstance(files, dict):
            self._degrade("snapshot_corrupt")
            return
        self.files = files

    def _degrade(self, reason: str) -> None:
        self.degraded = True
        self.degrade_reason = reason
        self.files = {}

    def save(self, files: dict[str, dict[str, Any]]) -> None:
        payload = {
            "version": SNAPSHOT_VERSION,
            "directory": str(self.directory),
            "updated_at": utc_now(),
            "files": files,
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        tmp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self.files = files
        self.degraded = False
        self.degrade_reason = None


def _validated_entry(
    entry: dict[str, Any]
) -> tuple[str, list[DatasetColumn], list[list[Any]]]:
    """Validate a cached snapshot entry, raising ValueError if unusable."""
    key = entry.get("key")
    columns_raw = entry.get("columns")
    rows = entry.get("rows")
    if not isinstance(key, str) or not key:
        raise ValueError("快照条目缺少 key")
    if not isinstance(columns_raw, list) or not columns_raw:
        raise ValueError("快照条目缺少列元信息")
    columns: list[DatasetColumn] = []
    for item in columns_raw:
        if not isinstance(item, dict):
            raise ValueError("快照列元信息格式错误")
        name, kind = item.get("name"), item.get("type")
        if not isinstance(name, str) or kind not in _DECLARED_TYPES:
            raise ValueError("快照列元信息格式错误")
        columns.append(DatasetColumn(name, kind))
    if not isinstance(rows, list) or not rows:
        raise ValueError("快照条目缺少数据行")
    if entry.get("row_count") != len(rows):
        raise ValueError("快照行数与数据不一致")
    width = len(columns)
    allowed = [_COLUMN_VALUE_TYPES[column.type] for column in columns]
    for row in rows:
        if not isinstance(row, list) or len(row) != width:
            raise ValueError("快照数据行宽度与列数不一致")
        for index, value in enumerate(row):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, allowed[index]):
                raise ValueError("快照数据值类型与列类型不符")
    return key, columns, rows


class DatasetCatalog:
    """Owns writable loading connections and one query-only connection."""

    def __init__(self, directory: str | Path, *, use_snapshot: bool = True) -> None:
        self.directory = Path(directory).resolve()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._readonly: sqlite3.Connection | None = None
        self.datasets: dict[str, DatasetMeta] = {}
        self._warnings: list[str] = []
        self._snapshot = CatalogSnapshot(self.directory) if use_snapshot else None
        self.refresh_report: dict[str, Any] = {}
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
        snapshot = self._snapshot
        incremental = snapshot is not None and not snapshot.degraded
        cached = snapshot.files if incremental else {}
        report: dict[str, Any] = {
            "snapshot_enabled": snapshot is not None,
            "snapshot_storage": "json_file",
            "snapshot_path": str(snapshot.path) if snapshot is not None else None,
            "storage_rationale": STORAGE_RATIONALE,
            "mode": "incremental" if incremental else "full",
            "degraded": snapshot is not None and snapshot.degraded,
            "degrade_reason": snapshot.degrade_reason if snapshot is not None else None,
            "reused": [],
            "reparsed": [],
            "added": [],
            "stale": [],
            "warnings": self._warnings,
            "snapshot_written": False,
        }
        current_names = {path.name for path in files}
        new_files: dict[str, dict[str, Any]] = {}
        for path in files:
            key = normalize_header(path.stem, 0)
            if key in self.datasets:
                raise DatasetError(f"数据集 key 重复: {key}")
            entry = cached.get(path.name)
            if (
                isinstance(entry, dict)
                and entry.get("status") == "active"
                and (entry.get("size"), entry.get("mtime_ns")) == _stat_signature(path)
            ):
                try:
                    meta = self._restore_file(path, entry)
                except (ValueError, KeyError, TypeError):
                    self._warnings.append(
                        f"快照条目校验失败，已回退为重新解析: {path.name}"
                    )
                else:
                    self.datasets[key] = meta
                    new_files[path.name] = entry
                    report["reused"].append(key)
                    continue
            previous_status = entry.get("status") if isinstance(entry, dict) else None
            meta, new_entry = self._parse_file(path, key)
            self.datasets[key] = meta
            new_files[path.name] = new_entry
            if previous_status == "active":
                report["reparsed"].append(key)
            else:
                report["added"].append(key)
        for name, entry in cached.items():
            if name in current_names or not isinstance(entry, dict):
                continue
            if entry.get("status") == "stale":
                new_files[name] = entry
            else:
                new_files[name] = {
                    "key": entry.get("key"),
                    "status": "stale",
                    "size": entry.get("size"),
                    "mtime_ns": entry.get("mtime_ns"),
                    "encoding": entry.get("encoding"),
                    "row_count": entry.get("row_count"),
                    "columns": entry.get("columns"),
                    "disposal": {
                        "action": "excluded_from_run",
                        "reason": "file_deleted",
                        "recorded_at": utc_now(),
                    },
                }
        report["stale"] = [
            {
                "key": entry.get("key"),
                "file": name,
                "reason": entry.get("disposal", {}).get("reason", "file_deleted"),
                "action": entry.get("disposal", {}).get("action", "excluded_from_run"),
                "recorded_at": entry.get("disposal", {}).get("recorded_at"),
            }
            for name, entry in sorted(new_files.items())
            if isinstance(entry, dict) and entry.get("status") == "stale"
        ]
        if snapshot is not None and new_files != cached:
            try:
                snapshot.save(new_files)
                report["snapshot_written"] = True
            except OSError as exc:
                self._warnings.append(f"快照写入失败（不影响本次比对）: {exc}")
        self._conn.commit()
        self.refresh_report = report

    def _create_table(self, key: str, columns: list[DatasetColumn]) -> None:
        self._conn.execute(f"DROP TABLE IF EXISTS {_quote(key)}")
        definitions = ", ".join(
            f"{_quote(column.name)} {_DECLARED_TYPES[column.type]}" for column in columns
        )
        self._conn.execute(f"CREATE TABLE {_quote(key)} ({definitions})")

    def _insert_rows(
        self, key: str, columns: list[DatasetColumn], rows: list[list[Any]]
    ) -> None:
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {_quote(key)} VALUES ({placeholders})"
        self._conn.executemany(sql, rows)

    def _parse_file(self, path: Path, key: str) -> tuple[DatasetMeta, dict[str, Any]]:
        size, mtime_ns, encoding, header, raw_rows = _read_csv_stable(path, self._warnings)
        types = [infer_type(column) for column in zip(*raw_rows)]
        columns = [DatasetColumn(name, kind) for name, kind in zip(header, types)]
        coerced = [
            [_coerce(value, types[index]) for index, value in enumerate(row)]
            for row in raw_rows
        ]
        self._create_table(key, columns)
        self._insert_rows(key, columns, coerced)
        meta = DatasetMeta(
            key=key,
            path=str(path),
            encoding=encoding,
            row_count=len(raw_rows),
            column_count=len(header),
            columns=columns,
        )
        entry = {
            "key": key,
            "status": "active",
            "size": size,
            "mtime_ns": mtime_ns,
            "encoding": encoding,
            "row_count": len(raw_rows),
            "columns": [
                {"name": column.name, "type": column.type} for column in columns
            ],
            "rows": coerced,
        }
        return meta, entry

    def _restore_file(self, path: Path, entry: dict[str, Any]) -> DatasetMeta:
        key, columns, rows = _validated_entry(entry)
        self._create_table(key, columns)
        self._insert_rows(key, columns, rows)
        return DatasetMeta(
            key=key,
            path=str(path),
            encoding=str(entry.get("encoding", "utf-8")),
            row_count=len(rows),
            column_count=len(columns),
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


def load_catalog(directory: str | Path, *, use_snapshot: bool = True) -> DatasetCatalog:
    return DatasetCatalog(directory, use_snapshot=use_snapshot)
