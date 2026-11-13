"""Local retention-window pruning for runs, snapshots and audit records.

The workbench is append-only by default; nothing is ever deleted unless a
retention window (in days) is explicitly supplied.  When pruning runs the
deletion order inside every batch is strictly::

    snapshots  ->  run-scoped audit records  ->  runs

and each batch is its own short transaction so a large repository is never
held inside a single long-lived write lock.  Audit rows belonging to pruned
runs are removed through their ``entity`` association (``run:<id>`` /
``run:<id>:...``), while audit rows that never reference a run are removed by
their own ``created_at`` age.

Concurrency is handled conservatively: the cutoff instant is fixed before
scanning, only *finished* (``completed`` / ``failed``) runs qualify, and each
batch re-checks status, ``finished_at`` and ``started_at`` inside its write
transaction.  The run ``DELETE`` itself carries the full eligibility
predicate, so a run whose state a concurrent writer changes in the narrow gap
between the read check and the write transaction matches zero rows, rolls the
whole batch back and is retried rather than being deleted.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..errors import CaliberError

FINISHED_STATUSES = ("completed", "failed")
DEFAULT_BATCH_SIZE = 200


@dataclass(slots=True)
class PruneStats:
    """Result of a dry-run or an actual pruning pass."""

    cutoff: str
    retention_days: int
    dry_run: bool
    eligible_runs: int = 0
    snapshots: int = 0
    run_audit_records: int = 0
    aged_audit_records: int = 0
    batches: int = 0
    pruned_run_ids: list[int] = field(default_factory=list)

    @property
    def audit_records(self) -> int:
        return self.run_audit_records + self.aged_audit_records

    def to_dict(self) -> dict[str, object]:
        return {
            "cutoff": self.cutoff,
            "retention_days": self.retention_days,
            "dry_run": self.dry_run,
            "eligible_runs": self.eligible_runs,
            "snapshots": self.snapshots,
            "audit_records": self.audit_records,
            "run_audit_records": self.run_audit_records,
            "aged_audit_records": self.aged_audit_records,
            "batches": self.batches,
            "pruned_run_ids": list(self.pruned_run_ids),
        }


def validate_retention_days(value: object) -> int:
    """Coerce and validate a positive integer retention window."""
    if isinstance(value, bool):
        raise CaliberError("保留窗口必须是正整数（天）")
    if isinstance(value, float) and not value.is_integer():
        raise CaliberError("保留窗口必须是正整数（天），不接受小数")
    try:
        days = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CaliberError("保留窗口必须是正整数（天）") from exc
    if days < 1:
        raise CaliberError("保留窗口必须是正整数（天）；不裁剪时请省略该参数")
    return days


def validate_batch_size(value: object) -> int:
    if isinstance(value, bool):
        raise CaliberError("批大小必须是正整数")
    try:
        size = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CaliberError("批大小必须是正整数") from exc
    if size < 1:
        raise CaliberError("批大小必须是正整数")
    return size


def retention_cutoff(retention_days: int, *, now: datetime | None = None) -> datetime:
    """Return the UTC instant older than which records may be pruned."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment - timedelta(days=validate_retention_days(retention_days))


def _cutoff_text(cutoff: datetime) -> str:
    return cutoff.astimezone(timezone.utc).isoformat(timespec="seconds")


def _run_entity_clause(run_ids: list[int]) -> tuple[str, list[object]]:
    """Build an OR clause matching ``run:<id>`` and ``run:<id>:...`` entities."""
    clauses: list[str] = []
    params: list[object] = []
    for run_id in run_ids:
        clauses.append("(entity = ? OR entity LIKE ?)")
        params.extend((f"run:{run_id}", f"run:{run_id}:%"))
    return " OR ".join(clauses), params


def _candidate_run_ids(conn: sqlite3.Connection, cutoff: str) -> list[int]:
    """All finished runs that ended before the fixed cutoff instant."""
    placeholders = ",".join("?" for _ in FINISHED_STATUSES)
    rows = conn.execute(
        f"""
        SELECT id FROM runs
        WHERE status IN ({placeholders})
          AND finished_at IS NOT NULL
          AND finished_at < ?
        ORDER BY id
        """,
        (*FINISHED_STATUSES, cutoff),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _still_eligible(conn: sqlite3.Connection, run_id: int, cutoff: str) -> bool:
    """Re-check a run inside the write transaction (concurrency guard).

    A run is deleted only when it is still finished, still ended before the
    cutoff, and was already started before the cutoff.  A brand-new run
    committed mid-prune -- or one that regressed to ``running`` -- fails this
    check and is left untouched.
    """
    placeholders = ",".join("?" for _ in FINISHED_STATUSES)
    row = conn.execute(
        f"""
        SELECT 1 FROM runs
        WHERE id = ?
          AND status IN ({placeholders})
          AND finished_at IS NOT NULL
          AND finished_at < ?
          AND started_at < ?
        """,
        (run_id, *FINISHED_STATUSES, cutoff, cutoff),
    ).fetchone()
    return row is not None


def _count_snapshots(conn: sqlite3.Connection, run_ids: list[int]) -> int:
    if not run_ids:
        return 0
    placeholders = ",".join("?" for _ in run_ids)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM snapshots WHERE run_id IN ({placeholders})",
        run_ids,
    ).fetchone()
    return int(row["n"])


def _count_run_audit(conn: sqlite3.Connection, run_ids: list[int]) -> int:
    if not run_ids:
        return 0
    clause, params = _run_entity_clause(run_ids)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM audit_log WHERE {clause}", params
    ).fetchone()
    return int(row["n"])


def _count_aged_audit(conn: sqlite3.Connection, cutoff: str) -> int:
    """Old audit rows that never reference a run (imports, CLI errors, ...)."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM audit_log
        WHERE created_at < ? AND entity NOT LIKE 'run:%'
        """,
        (cutoff,),
    ).fetchone()
    return int(row["n"])


def plan_pruning(
    conn: sqlite3.Connection,
    retention_days: int,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    now: datetime | None = None,
) -> PruneStats:
    """Compute what *would* be deleted without touching any data (read-only)."""
    days = validate_retention_days(retention_days)
    size = validate_batch_size(batch_size)
    cutoff = _cutoff_text(retention_cutoff(days, now=now))
    run_ids = _candidate_run_ids(conn, cutoff)
    stats = PruneStats(cutoff=cutoff, retention_days=days, dry_run=True)
    stats.eligible_runs = len(run_ids)
    stats.snapshots = _count_snapshots(conn, run_ids)
    stats.run_audit_records = _count_run_audit(conn, run_ids)
    stats.aged_audit_records = _count_aged_audit(conn, cutoff)
    stats.batches = (len(run_ids) + size - 1) // size if run_ids else 0
    stats.pruned_run_ids = run_ids
    return stats


def _delete_aged_audit_batch(
    conn: sqlite3.Connection, cutoff: str, batch_size: int
) -> int:
    """Delete at most ``batch_size`` non-run audit rows older than cutoff."""
    id_rows = conn.execute(
        """
        SELECT id FROM audit_log
        WHERE created_at < ? AND entity NOT LIKE 'run:%'
        ORDER BY id LIMIT ?
        """,
        (cutoff, batch_size),
    ).fetchall()
    ids = [int(row["id"]) for row in id_rows]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cursor = conn.execute(
        f"DELETE FROM audit_log WHERE id IN ({placeholders})", ids
    )
    return cursor.rowcount


def prune(
    conn: sqlite3.Connection,
    retention_days: int,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    now: datetime | None = None,
    progress: Callable[[PruneStats], None] | None = None,
) -> PruneStats:
    """Actually delete records outside the retention window, batch by batch.

    Every batch is committed independently so the write lock is held only
    briefly.  On error the current transaction is rolled back while
    already-committed batches stay durable -- re-running the command safely
    finishes whatever remains outside the window.

    Concurrency relies on SQLite's database-level write lock and snapshot
    isolation, plus the guarded run DELETE inside ``_delete_run_batch``; no
    extra bookkeeping table or trigger is required.
    """
    days = validate_retention_days(retention_days)
    size = validate_batch_size(batch_size)
    cutoff = _cutoff_text(retention_cutoff(days, now=now))

    candidate_ids = _candidate_run_ids(conn, cutoff)
    stats = PruneStats(cutoff=cutoff, retention_days=days, dry_run=False)
    stats.eligible_runs = len(candidate_ids)

    offset = 0
    while offset < len(candidate_ids):
        batch = candidate_ids[offset: offset + size]
        offset += size
        deleted_ids = _delete_run_batch(conn, batch, cutoff, stats)
        if deleted_ids and progress is not None:
            progress(stats)

    # Non-run audit rows are trimmed purely by their own age, in small batches.
    # These rows never reference runs, so concurrent run writers cannot create
    # an eligible row mid-flight; a plain batched delete is sufficient.
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            removed = _delete_aged_audit_batch(conn, cutoff, size)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if not removed:
            break
        stats.aged_audit_records += removed
        stats.batches += 1
        if progress is not None:
            progress(stats)

    _assert_no_orphans(conn, stats.pruned_run_ids)
    return stats


class _RunNoLongerEligible(Exception):
    """Internal signal: the guarded run DELETE matched zero rows."""


def _delete_guarded_run(
    conn: sqlite3.Connection, run_id: int, cutoff: str
) -> int:
    """Delete one run only if it still matches every retention predicate.

    The DELETE carries the full eligibility WHERE clause rather than trusting
    a prior SELECT.  SQLite serializes writers database-wide and freezes a
    write transaction's snapshot when it begins, so if a concurrent status
    flip lands between the read check and this statement, the statement's own
    snapshot re-evaluates the predicate against the latest committed row and
    matches zero rows.  Returns 1 when deleted, 0 when it no longer qualifies.
    """
    placeholders = ",".join("?" for _ in FINISHED_STATUSES)
    cursor = conn.execute(
        f"""
        DELETE FROM runs
        WHERE id = ?
          AND status IN ({placeholders})
          AND finished_at IS NOT NULL
          AND finished_at < ?
          AND started_at < ?
        """,
        (run_id, *FINISHED_STATUSES, cutoff, cutoff),
    )
    return max(0, cursor.rowcount)


def _delete_run_batch(
    conn: sqlite3.Connection,
    batch: list[int],
    cutoff: str,
    stats: PruneStats,
    *,
    max_attempts: int = 5,
) -> list[int]:
    """Delete one batch with a bounded retry on a concurrent eligibility flip.

    Per-attempt counts stay local and are merged into ``stats`` only after a
    successful commit, so a rolled-back attempt never pollutes the totals.

    SQLite already makes two of the three interleavings safe.  A flip
    committed before this transaction begins is visible to the in-transaction
    re-check (the run is skipped); a flip committed after it begins blocks on
    the database write lock and only lands after our commit, by which point the
    run is already deleted.  The remaining gap is a flip that commits in the
    instant between the read check and ``BEGIN IMMEDIATE``: the guarded run
    DELETE then matches zero rows, the whole transaction is rolled back
    (undoing its snapshot/audit deletes too), and the batch is retried against
    a fresh snapshot that observes the flip.
    """
    for _ in range(max_attempts):
        deleted_ids: list[int] = []
        snap_deleted = 0
        audit_deleted = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            for run_id in batch:
                # In-transaction re-check against this transaction's snapshot.
                if not _still_eligible(conn, run_id, cutoff):
                    continue
                # Required order inside the transaction: snapshots -> audit ->
                # runs; nothing is visible to other connections until commit.
                snap_cursor = conn.execute(
                    "DELETE FROM snapshots WHERE run_id = ?", (run_id,)
                )
                snap_deleted += max(0, snap_cursor.rowcount)
                audit_cursor = conn.execute(
                    "DELETE FROM audit_log WHERE entity = ? OR entity LIKE ?",
                    (f"run:{run_id}", f"run:{run_id}:%"),
                )
                audit_deleted += max(0, audit_cursor.rowcount)
                if _delete_guarded_run(conn, run_id, cutoff) == 0:
                    raise _RunNoLongerEligible(run_id)
                deleted_ids.append(run_id)
            conn.commit()
        except _RunNoLongerEligible:
            conn.rollback()
            # A concurrent writer flipped a run; retry with a fresh snapshot.
            continue
        except Exception:
            conn.rollback()
            raise
        stats.snapshots += snap_deleted
        stats.run_audit_records += audit_deleted
        if deleted_ids:
            stats.batches += 1
            stats.pruned_run_ids.extend(deleted_ids)
        return deleted_ids
    raise CaliberError(
        "裁剪期间检测到持续并发写入，该批次多次重试仍冲突，请稍后重试",
        code="retention_busy",
    )


def _assert_no_orphans(
    conn: sqlite3.Connection, pruned_run_ids: list[int]
) -> None:
    """Post-commit integrity guard: nothing may point at a deleted run."""
    if not pruned_run_ids:
        return
    placeholders = ",".join("?" for _ in pruned_run_ids)
    orphan_snaps = conn.execute(
        f"SELECT COUNT(*) AS n FROM snapshots WHERE run_id IN ({placeholders})",
        pruned_run_ids,
    ).fetchone()
    if int(orphan_snaps["n"]) != 0:  # pragma: no cover - defensive guard
        raise CaliberError(
            "裁剪后仍存在指向已删运行的孤儿快照",
            details={"orphan_snapshots": int(orphan_snaps["n"])},
        )
    clause, params = _run_entity_clause(pruned_run_ids)
    orphan_audit = conn.execute(
        f"SELECT COUNT(*) AS n FROM audit_log WHERE {clause}", params
    ).fetchone()
    if int(orphan_audit["n"]) != 0:  # pragma: no cover - defensive guard
        raise CaliberError(
            "裁剪后仍存在指向已删运行的审计记录",
            details={"orphan_audit": int(orphan_audit["n"])},
        )
