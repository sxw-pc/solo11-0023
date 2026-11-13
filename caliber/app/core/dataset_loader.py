"""CSV fixture loading into private in-memory SQLite databases.

Loading policy (also recorded per dataset in ``DatasetMeta.load_metadata``):

* Encoding is probed with a strict fallback chain: UTF-8 (``utf-8-sig`` when a
  byte-order mark is present) -> GBK -> latin-1.  latin-1 is a lossless last
  resort, so undecodable *text* does not exist; genuinely binary input is
  rejected before the chain and reported with its file/line/column.
* Column types are inferred from non-empty cells only.  A column that has no
  non-empty cell at all is reported as ``unknown`` instead of being coerced to
  text, so an all-null numeric column does not silently become a string.
* Decoding and inference happen exactly once per file while the catalog is
  built, and the result is kept in that catalog's metadata.  They are *not*
  cached across ``load_catalog`` calls: CSV files are mutable inputs, and a
  cross-run cache would need fingerprint invalidation and could still serve
  stale metadata when a file is replaced between runs.  Rebuilding is
  deterministic (sorted files, pure detection, no sampling), so repeated loads
  of an unchanged directory produce identical metadata.
* A file whose content changes while it is being read is retried once; if it
  is still unstable the file fails with an explicit error instead of mixing
  bytes from two versions.
* Per-file failures are isolated: valid files are still loaded, and the
  failing file is listed in ``DatasetCatalog.errors``.  If every file fails the
  catalog constructor raises an aggregate :class:`DatasetError`.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
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

BOM_UTF8 = b"\xef\xbb\xbf"
ENCODING_CHAIN = ("utf-8", "gbk", "latin-1")
DECLARED_TYPES = {
    "int": "INTEGER",
    "float": "REAL",
    "date": "TEXT",
    "str": "TEXT",
    "unknown": "TEXT",
}

CACHE_POLICY = {
    "scope": "catalog_snapshot",
    "compute_once_per_load": True,
    "reuse_across_loads": False,
    "reason": (
        "编码探测与类型推断对同一文件在单次 load_catalog 构建中只执行一次，"
        "结果写入 DatasetMeta.load_metadata；不在多次 load_catalog 之间复用，"
        "因为 CSV 是可变的输入文件，跨调用缓存需要指纹失效且仍可能读到过期类型。"
        "读取使用内容指纹 sha256，可审计本次元信息对应的确切文件版本。"
    ),
}

ENCODING_POLICY = (
    "依次严格尝试 UTF-8（存在 BOM 时记为 utf-8-sig）、GBK、latin-1；"
    "latin-1 仅作无损兜底，若结果含 C1 控制字符或二进制内容则判定为无法解码并只报出该文件位置。"
)


def normalize_header(raw: str, index: int) -> str:
    """Normalize a header cell; BOM characters are always removed."""
    name = HEADER_RE.sub("_", raw.lstrip("﻿").strip().lower()).strip("_")
    if not name:
        raise DatasetError(f"第 {index + 1} 列表头为空")
    if name[0].isdigit():
        name = f"col_{name}"
    return name


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def infer_type(values: Iterable[str]) -> str:
    """Infer a column type from non-empty values.

    ``unknown`` means the column had no non-empty sample at all; it must not
    be conflated with ``str`` because downstream numeric SQL would then compare
    text instead of numbers.
    """
    items = [value for value in values if value not in ("", None)]
    if not items:
        return "unknown"
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


def _failure(
    path: Path,
    message: str,
    *,
    reason: str,
    **details: Any,
) -> DatasetError:
    """Build a DatasetError that always names the offending file."""
    payload: dict[str, Any] = {"file": str(path), "reason": reason}
    payload.update({key: value for key, value in details.items() if value is not None})
    return DatasetError(f"{path}: {message}", details=payload)


def _line_column(text: str, offset: int) -> tuple[int, int]:
    """Map a character offset to a 1-based line and CSV column."""
    prefix = text[: max(0, offset)]
    line = prefix.count("\n") + 1
    segment = prefix.rsplit("\n", 1)[-1]
    try:
        cells = next(csv.reader([segment]), [])
    except csv.Error:
        cells = segment.split(",")
    return line, max(1, len(cells))


def _header_label(text: str, column: int) -> str | None:
    """Best-effort header name for error messages."""
    first_line = text.split("\n", 1)[0]
    try:
        cells = next(csv.reader([first_line]), [])
    except csv.Error:
        cells = first_line.split(",")
    if 1 <= column <= len(cells):
        label = cells[column - 1].lstrip("﻿").strip()
        if not label or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in label):
            return None
        return label[:80]
    return None


def _binary_offset(raw: bytes) -> int | None:
    """Return the first offset that looks like binary rather than text."""
    sample = raw[:65536]
    nul = sample.find(b"\x00")
    if nul >= 0:
        return nul
    if not sample:
        return None
    control = sum(1 for byte in sample if byte < 32 and byte not in (9, 10, 13))
    if control / len(sample) <= 0.10:
        return None
    for index, byte in enumerate(sample):
        if byte < 32 and byte not in (9, 10, 13):
            return index
    return None


def _decode(raw: bytes, path: Path) -> tuple[str, dict[str, Any]]:
    """Decode bytes using the documented fallback chain.

    Returns the decoded text and a metadata dict describing the decision.
    """
    binary = _binary_offset(raw)
    if binary is not None:
        preview = raw.decode("latin-1", errors="replace")
        line, column = _line_column(preview, binary)
        label = _header_label(preview, column)
        where = f"第 {line} 行第 {column} 列"
        if label:
            where += f"（{label}）"
        raise _failure(
            path,
            f"字节偏移 {binary}（{where}）处是二进制内容，无法按文本解码",
            reason="binary_content",
            line=line,
            column=column,
            column_name=label,
            byte_offset=binary,
        )

    bom = raw.startswith(BOM_UTF8)
    payload = raw[len(BOM_UTF8):] if bom else raw
    candidates: list[dict[str, Any]] = []
    for encoding in ENCODING_CHAIN:
        try:
            text = payload.decode(encoding)
        except UnicodeDecodeError as exc:
            prefix = payload[: exc.start].decode("latin-1", errors="replace")
            line, column = _line_column(prefix, len(prefix))
            candidates.append(
                {
                    "encoding": encoding,
                    "ok": False,
                    "error": str(exc),
                    "byte_offset": exc.start + (len(BOM_UTF8) if bom else 0),
                    "line": line,
                    "column": column,
                }
            )
            continue
        selected = "utf-8-sig" if bom and encoding == "utf-8" else encoding
        fallback = encoding != "utf-8"
        if encoding == "latin-1":
            # latin-1 maps every byte, so it cannot fail.  A character in the
            # C1 range (U+0080-U+009F) means the bytes were never valid
            # latin-1 text; report the file instead of silently producing
            # mojibake metadata.
            bad = next(
                (
                    index
                    for index, char in enumerate(text)
                    if "\x80" <= char <= "\x9f"
                ),
                None,
            )
            if bad is not None:
                line, column = _line_column(text, bad)
                label = _header_label(text, column)
                where = f"第 {line} 行第 {column} 列"
                if label:
                    where += f"（{label}）"
                candidates.append(
                    {
                        "encoding": selected,
                        "ok": False,
                        "error": "包含 C1 控制字符，不是合法文本",
                        "byte_offset": bad + (len(BOM_UTF8) if bom else 0),
                        "line": line,
                        "column": column,
                    }
                )
                raise _failure(
                    path,
                    f"UTF-8、GBK、latin-1 均无法得到合法文本；"
                    f"字节偏移 {bad}（{where}）处为 C1 控制字符",
                    reason="undecodable_content",
                    line=line,
                    column=column,
                    column_name=label,
                    byte_offset=bad + (len(BOM_UTF8) if bom else 0),
                    encoding_chain=candidates,
                )
        candidates.append({"encoding": selected, "ok": True})
        if fallback and selected == "latin-1":
            warnings_extra = "latin-1 为无损兜底；若原文件并非 latin-1 文本，解码结果可能是乱码"
        else:
            warnings_extra = None
        if text.startswith("﻿"):
            text = text.lstrip("﻿")
        return text, {
            "selected": selected,
            "fallback": fallback,
            "bom": bom,
            "chain": list(ENCODING_CHAIN),
            "candidates": candidates,
            "policy": ENCODING_POLICY,
            "note": warnings_extra,
        }

    # latin-1 decodes every byte sequence, so this is unreachable in practice.
    raise _failure(path, "无法确定文件编码", reason="decode_error", candidates=candidates)


def _signature(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    """Identity of a file version: device, inode, size and mtime."""
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def _read_source(path: Path) -> tuple[bytes, dict[str, Any]]:
    """Read a file once, refusing to mix two versions of it.

    The file is read through a single handle and its descriptor is compared
    with the directory entry afterwards.  If the path was replaced or modified
    during the read, the whole file is read again; a second unstable read is a
    hard error for this file.
    """
    reason = "读取期间文件发生变化"
    attempts = 0
    last_error: OSError | None = None
    for attempt in range(2):
        attempts = attempt + 1
        try:
            with path.open("rb") as handle:
                before = _signature(os.fstat(handle.fileno()))
                data = handle.read()
                after = _signature(os.fstat(handle.fileno()))
            current = _signature(path.stat())
        except OSError as exc:
            last_error = exc
            reason = f"无法读取数据集: {exc}"
            continue
        if before == after == current and len(data) == after[2]:
            return data, {
                "path": str(path),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mtime_ns": after[3],
                "read_attempts": attempts,
                "stable": True,
            }
        last_error = None
    if last_error is not None:
        raise _failure(
            path,
            f"{reason}，已重试 {attempts} 次",
            reason="read_error",
            attempts=attempts,
        )
    raise _failure(
        path,
        f"{reason}，已重试 {attempts} 次",
        reason="file_changed_during_read",
        attempts=attempts,
    )


def _parse_csv(
    text: str, path: Path
) -> tuple[list[str], list[str], list[list[str]]]:
    """Parse decoded text into normalized headers, originals and data rows."""
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        rows = list(reader)
    except csv.Error as exc:
        line = max(1, reader.line_num)
        lines = text.splitlines()
        segment = lines[line - 1] if line - 1 < len(lines) else ""
        column = max(1, len(segment.split(",")))
        label = _header_label(text, column)
        where = f"第 {line} 行第 {column} 列"
        if label:
            where += f"（{label}）"
        raise _failure(
            path,
            f"CSV 解析失败（{where}）: {exc}",
            reason="csv_parse_error",
            line=line,
            column=column,
            column_name=label,
        ) from exc

    if not rows:
        raise _failure(path, "空数据集", reason="empty_dataset")

    original = [cell.lstrip("﻿").strip() for cell in rows[0]]
    header: list[str] = []
    for index, item in enumerate(original):
        try:
            header.append(normalize_header(item, index))
        except DatasetError as exc:
            raise _failure(
                path,
                f"第 {index + 1} 列表头为空",
                reason="empty_header",
                line=1,
                column=index + 1,
            ) from exc

    duplicates = sorted({name for name in header if header.count(name) > 1})
    if duplicates:
        raise _failure(
            path,
            f"存在重复列名: {', '.join(duplicates)}",
            reason="duplicate_column",
            columns=duplicates,
        )

    data_rows: list[list[str]] = []
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(header):
            index = min(len(row), len(header))
            label = header[index] if index < len(header) else None
            where = f"第 {line_no} 行第 {index + 1} 列"
            if label:
                where += f"（{label}）"
            raise _failure(
                path,
                f"{where}列数 {len(row)} 与表头列数 {len(header)} 不一致",
                reason="column_count_mismatch",
                line=line_no,
                column=index + 1,
                column_name=label,
                expected=len(header),
                actual=len(row),
            )
        data_rows.append([cell.strip() for cell in row])

    if not data_rows:
        raise _failure(path, "数据集没有数据行", reason="no_data_rows")
    return header, original, data_rows


def _infer_columns(
    header: list[str], rows: list[list[str]]
) -> tuple[list[str], dict[str, Any]]:
    columns_raw = list(zip(*rows))
    types: list[str] = []
    empty_cells = 0
    unknown: list[str] = []
    for name, values in zip(header, columns_raw):
        non_empty = [value for value in values if value not in ("", None)]
        empty_cells += len(values) - len(non_empty)
        kind = infer_type(non_empty)
        if kind == "unknown":
            unknown.append(name)
        types.append(kind)
    stats = {
        "strategy": "non_empty_cells",
        "policy": "跳过空单元格，只用非空样本推断；整列为空标记为 unknown。",
        "row_count": len(rows),
        "empty_cell_count": empty_cells,
        "unknown_columns": unknown,
    }
    return types, stats


class DatasetCatalog:
    """Owns writable loading connections and one query-only connection."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._readonly: sqlite3.Connection | None = None
        self.datasets: dict[str, DatasetMeta] = {}
        # Per-file failures; populated while still loading the remaining files.
        self.errors: list[dict[str, Any]] = []
        self._failed_keys: dict[str, dict[str, Any]] = {}
        self._sources: dict[str, Path] = {}
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
            try:
                self._load_file(path)
            except DatasetError as exc:
                record = {
                    "file": str(path),
                    "key": self._key_for(path),
                    "error": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
                self.errors.append(record)
                key = record["key"]
                if key:
                    self._failed_keys.setdefault(key, record)
        if not self.datasets:
            summary = "; ".join(
                f"{item['file']}: {item['message']}" for item in self.errors
            )
            raise DatasetError(
                f"数据集目录中的 CSV 全部加载失败: {summary}",
                details={"directory": str(self.directory), "files": self.errors},
            )
        self._conn.commit()

    @staticmethod
    def _key_for(path: Path) -> str:
        try:
            return normalize_header(path.stem, 0)
        except DatasetError:
            return path.stem

    def _load_file(self, path: Path) -> None:
        key = self._key_for(path)
        if key in self.datasets:
            existing = self._sources[key]
            raise _failure(
                path,
                f"数据集 key {key!r} 与 {existing} 冲突；二者元信息不会互相覆盖",
                reason="duplicate_key",
                key=key,
                existing_file=str(existing),
            )

        raw, source_meta = _read_source(path)
        text, encoding_meta = _decode(raw, path)
        header, original, rows = _parse_csv(text, path)
        types, type_meta = _infer_columns(header, rows)

        warnings: list[str] = []
        if encoding_meta["fallback"]:
            warnings.append(
                f"文件未通过 UTF-8 校验，已回退到 {encoding_meta['selected']} 解码"
            )
        if encoding_meta.get("note"):
            warnings.append(encoding_meta["note"])
        if type_meta["unknown_columns"]:
            warnings.append(
                "整列为空，类型标记为 unknown: "
                + ", ".join(type_meta["unknown_columns"])
            )

        definitions = ", ".join(
            f"{_quote(name)} {DECLARED_TYPES[kind]}"
            for name, kind in zip(header, types)
        )
        placeholders = ", ".join("?" for _ in header)
        converted = self._convert_rows(path, header, rows, types)

        savepoint = "dataset_file_load"
        self._conn.execute(f"SAVEPOINT {savepoint}")
        try:
            self._conn.execute(f"CREATE TABLE {_quote(key)} ({definitions})")
            self._conn.executemany(
                f"INSERT INTO {_quote(key)} VALUES ({placeholders})", converted
            )
        except sqlite3.Error as exc:
            self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise _failure(
                path, f"写入内存数据库失败: {exc}", reason="sqlite_error"
            ) from exc
        self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")

        self._sources[key] = path
        self.datasets[key] = DatasetMeta(
            key=key,
            path=str(path),
            encoding=encoding_meta["selected"],
            row_count=len(rows),
            column_count=len(header),
            columns=[
                DatasetColumn(name, kind, original_name=source_name)
                for name, kind, source_name in zip(header, types, original)
            ],
            encoding_fallback=encoding_meta["fallback"],
            bom=encoding_meta["bom"],
            load_metadata={
                "source": source_meta,
                "encoding": encoding_meta,
                "type_inference": type_meta,
                "cache": CACHE_POLICY,
                "warnings": warnings,
            },
        )

    @staticmethod
    def _convert_rows(
        path: Path,
        header: list[str],
        rows: list[list[str]],
        types: list[str],
    ) -> list[tuple[Any, ...]]:
        converted: list[tuple[Any, ...]] = []
        for row_no, row in enumerate(rows, start=2):
            item: list[Any] = []
            for index, (value, kind) in enumerate(zip(row, types)):
                try:
                    item.append(_coerce(value, kind))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise _failure(
                        path,
                        f"第 {row_no} 行第 {index + 1} 列（{header[index]}）"
                        f"无法转换为 {kind}: {value!r}",
                        reason="coercion_error",
                        line=row_no,
                        column=index + 1,
                        column_name=header[index],
                        value=value,
                        target_type=kind,
                    ) from exc
            converted.append(tuple(item))
        return converted

    def list_metadata(self) -> list[DatasetMeta]:
        return [self.datasets[key] for key in sorted(self.datasets)]

    def require(self, key: str) -> DatasetMeta:
        if key in self.datasets:
            return self.datasets[key]
        if key in self._failed_keys:
            record = self._failed_keys[key]
            raise DatasetError(
                f"数据集 {key} 加载失败: {record['message']}", details=record
            )
        raise NotFoundError(f"数据集不存在: {key}")

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
