"""Metric-key merge windows over the existing runs/snapshots tables.

The window is a derived view.  It never creates a table or a column: the
window boundary and the derived state machine are written back into the
``summary_json`` of the newest snapshot of the metric, which can then be
exported by every report format.

Policy choice
-------------
The implemented policy is *fixed run count* (``run_count``).  A run is one
discrete, auditable evaluation event and ``run_id`` alone determines the
window boundary.  The window therefore has a stable size, a new snapshot only
needs the metric's most recent ``size + 1`` snapshots to be recomputed, and
recomputing the same window twice produces byte-identical content.  A
time-span policy would make the window size swing with run density, and
``created_at`` only has second precision, so several runs in the same second
would make the boundary unstable.

The switch point is :data:`WINDOW_POLICIES`: a new policy only has to
register a collector with the signature
``(conn, metric_key, size, anchor_snapshot_id) -> rows``.  State calculation,
write-back and reporting stay unchanged.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable

from ..errors import CaliberError

DEFAULT_POLICY = "run_count"
DEFAULT_WINDOW_SIZE = 5
MAX_WINDOW_SIZE = 200

STATUS_FIRST_DEVIATION = "first_deviation"
STATUS_ONGOING_DEVIATION = "ongoing_deviation"
STATUS_RECOVERED = "recovered"
STATUS_CONSISTENT = "consistent"

WINDOW_STATUSES = (
    STATUS_FIRST_DEVIATION,
    STATUS_ONGOING_DEVIATION,
    STATUS_RECOVERED,
    STATUS_CONSISTENT,
)
STATUS_LABELS = {
    STATUS_FIRST_DEVIATION: "首次偏离",
    STATUS_ONGOING_DEVIATION: "持续偏离",
    STATUS_RECOVERED: "恢复",
    STATUS_CONSISTENT: "从未偏离",
}

SELECTION_REASON = (
    "按固定 run 数归并：每个 run 是一次离散、可审计的求值，run_id 直接决定窗口边界；"
    "窗口大小恒定，新增快照时只读取该指标最近 K+1 条记录即可增量重算，"
    "同一窗口重复计算的结论与写回内容完全一致。"
    "按时间跨度归并会让窗口大小随运行密度抖动，且 created_at 只有秒级精度，"
    "同一秒内多次运行时边界不稳定，维护与验收成本更高。"
)
SWITCH_POINT = (
    "app/core/windowing.py 的 WINDOW_POLICIES 注册表；"
    "调用链为 Config.window_policy → perform_run(window_policy=...) → save_snapshot(...)。"
    "新增策略时注册一个 (conn, metric_key, size, anchor_snapshot_id) 采集函数即可，"
    "状态计算、写回与报告层无需改动。"
)
DEGRADED_NOTICE = (
    "旧快照缺少 result_json 细项（diffs/dimensions），窗口已降级为仅按 verdict 归并。"
)

_SNAPSHOT_COLUMNS = (
    "id, run_id, metric_key, metric_version, verdict, summary_json, result_json, created_at"
)


def normalize_policy(policy: str | None) -> str:
    normalized = str(policy or DEFAULT_POLICY)
    if normalized not in WINDOW_POLICIES:
        raise CaliberError(
            f"不支持的归并窗口策略: {normalized}。{SWITCH_POINT}",
            code="window_policy_unsupported",
        )
    return normalized


def normalize_size(size: Any) -> int:
    if size is None:
        return DEFAULT_WINDOW_SIZE
    try:
        normalized = int(size)
    except (TypeError, ValueError) as exc:
        raise CaliberError("窗口 run 数必须是正整数", code="window_size_invalid") from exc
    if normalized < 1 or normalized > MAX_WINDOW_SIZE:
        raise CaliberError(
            f"窗口 run 数必须在 1..{MAX_WINDOW_SIZE} 之间",
            code="window_size_invalid",
        )
    return normalized


def normalize_window(
    policy: str | None = DEFAULT_POLICY, size: Any = DEFAULT_WINDOW_SIZE
) -> tuple[str, int]:
    return normalize_policy(policy), normalize_size(size)


def execution_metadata(
    policy: str | None = DEFAULT_POLICY,
    size: Any = DEFAULT_WINDOW_SIZE,
    *,
    source: str = "run",
) -> dict[str, Any]:
    """Metadata written into ``runs.summary_json`` for traceability."""
    normalized_policy, normalized_size = normalize_window(policy, size)
    return {
        "policy": normalized_policy,
        "size": normalized_size,
        "unit": "runs",
        "selection_reason": SELECTION_REASON,
        "switch_point": SWITCH_POINT,
        "source": source,
    }


def _json_object(raw: Any) -> dict[str, Any] | None:
    """Decode a JSON object without letting legacy/corrupt rows break a run."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if isinstance(value, dict):
            return value
    return None


def _newest_per_run(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Keep the newest snapshot per run while preserving DESC order."""
    seen: set[int] = set()
    result: list[sqlite3.Row] = []
    for row in rows:
        run_id = int(row["run_id"])
        if run_id in seen:
            continue
        seen.add(run_id)
        result.append(row)
    return result


def collect_run_count(
    conn: sqlite3.Connection,
    metric_key: str,
    size: int,
    anchor_snapshot_id: int | None = None,
) -> list[sqlite3.Row]:
    """Collect the newest ``size + 1`` distinct runs for one metric.

    The ``LIMIT`` query uses ``idx_snapshots_key(metric_key, id DESC)``; it
    never scans snapshots belonging to other metrics.  One extra row is read
    as look-back context so a deviation that began before the window can be
    distinguished from a first deviation.
    """
    limit = size + 1
    while True:
        if anchor_snapshot_id is None:
            rows = conn.execute(
                f"SELECT {_SNAPSHOT_COLUMNS} FROM snapshots "
                "WHERE metric_key = ? ORDER BY id DESC LIMIT ?",
                (metric_key, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_SNAPSHOT_COLUMNS} FROM snapshots "
                "WHERE metric_key = ? AND id <= ? ORDER BY id DESC LIMIT ?",
                (metric_key, int(anchor_snapshot_id), limit),
            ).fetchall()
        distinct = _newest_per_run(list(rows))
        if len(distinct) >= size + 1 or len(rows) < limit:
            return distinct
        # Pathological duplicate snapshots inside one run: read a bit more,
        # still bounded by the metric's own history rather than the full table.
        limit += size + 1


WINDOW_POLICIES: dict[str, Callable[..., list[sqlite3.Row]]] = {
    "run_count": collect_run_count,
}


def _detail_fields(raw: Any) -> dict[str, Any]:
    """Reuse ``result_json`` details, or fall back to verdict-only merging."""
    result = _json_object(raw)
    if (
        result is None
        or not isinstance(result.get("diffs"), list)
        or not isinstance(result.get("dimensions"), list)
    ):
        return {
            "detail_available": False,
            "diff_count": None,
            "mismatch_dimensions": [],
            "missing_dimensions": [],
        }
    diffs = [item for item in result["diffs"] if isinstance(item, dict)]
    mismatch = sorted(
        {
            str(item["dimension"])
            for item in diffs
            if item.get("kind") == "value_mismatch" and item.get("dimension") is not None
        }
    )
    missing = sorted(
        {
            str(item["dimension"])
            for item in diffs
            if item.get("kind") == "missing_dimension" and item.get("dimension") is not None
        }
    )
    return {
        "detail_available": True,
        "diff_count": len(diffs),
        "mismatch_dimensions": mismatch,
        "missing_dimensions": missing,
    }


def _entry(row: sqlite3.Row) -> dict[str, Any]:
    verdict = str(row["verdict"])
    return {
        "run_id": int(row["run_id"]),
        "snapshot_id": int(row["id"]),
        "metric_version": int(row["metric_version"]),
        "verdict": verdict,
        "deviating": verdict != "consistent",
        **_detail_fields(row["result_json"]),
    }


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_window(
    rows: list[sqlite3.Row],
    metric_key: str,
    policy: str = DEFAULT_POLICY,
    size: int = DEFAULT_WINDOW_SIZE,
) -> dict[str, Any]:
    """Compute a deterministic window view from newest-first snapshot rows."""
    normalized_policy, normalized_size = normalize_window(policy, size)
    references = [_entry(row) for row in rows]
    window_entries = list(reversed(references[:normalized_size]))
    lookback_row = references[normalized_size] if len(references) > normalized_size else None
    lookback = (
        {
            "run_id": lookback_row["run_id"],
            "snapshot_id": lookback_row["snapshot_id"],
            "verdict": lookback_row["verdict"],
            "deviating": lookback_row["deviating"],
        }
        if lookback_row is not None
        else None
    )

    episodes: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    previous_deviating = bool(lookback and lookback["deviating"])
    previous_verdict = lookback["verdict"] if lookback else None
    open_episode: dict[str, Any] | None = None
    if previous_deviating:
        # The window starts already deviating; the real start lies before it.
        open_episode = {
            "start_run_id": None,
            "start_snapshot_id": None,
            "start_before_window": True,
            "start_verdict": None,
            "end_run_id": None,
            "end_snapshot_id": None,
            "end_verdict": None,
        }

    for entry in window_entries:
        if entry["deviating"] and not previous_deviating:
            open_episode = {
                "start_run_id": entry["run_id"],
                "start_snapshot_id": entry["snapshot_id"],
                "start_before_window": False,
                "start_verdict": entry["verdict"],
                "end_run_id": None,
                "end_snapshot_id": None,
                "end_verdict": None,
            }
            transitions.append(
                {
                    "run_id": entry["run_id"],
                    "snapshot_id": entry["snapshot_id"],
                    "type": "deviation_start",
                    "from_verdict": previous_verdict,
                    "to_verdict": entry["verdict"],
                }
            )
        elif not entry["deviating"] and previous_deviating:
            assert open_episode is not None
            open_episode["end_run_id"] = entry["run_id"]
            open_episode["end_snapshot_id"] = entry["snapshot_id"]
            open_episode["end_verdict"] = entry["verdict"]
            episodes.append(open_episode)
            open_episode = None
            transitions.append(
                {
                    "run_id": entry["run_id"],
                    "snapshot_id": entry["snapshot_id"],
                    "type": "deviation_end",
                    "from_verdict": previous_verdict,
                    "to_verdict": entry["verdict"],
                }
            )
        previous_deviating = entry["deviating"]
        previous_verdict = entry["verdict"]

    if previous_deviating and open_episode is not None:
        episodes.append(open_episode)

    last_episode = episodes[-1] if episodes else None
    last_entry = window_entries[-1]
    if last_entry["deviating"]:
        # first_deviation: this anchor run is itself the first deviating run.
        # ongoing_deviation: the current episode began in an earlier run.
        if last_episode and last_episode["start_snapshot_id"] == last_entry["snapshot_id"]:
            status = STATUS_FIRST_DEVIATION
        else:
            status = STATUS_ONGOING_DEVIATION
    elif last_episode:
        status = STATUS_RECOVERED
    else:
        status = STATUS_CONSISTENT

    degraded_ids = [
        entry["snapshot_id"] for entry in window_entries if not entry["detail_available"]
    ]
    digest_payload = {
        "metric_key": metric_key,
        "policy": normalized_policy,
        "size": normalized_size,
        "lookback": lookback,
        "rows": [
            {
                "run_id": entry["run_id"],
                "snapshot_id": entry["snapshot_id"],
                "verdict": entry["verdict"],
            }
            for entry in window_entries
        ],
    }
    run_ids = [entry["run_id"] for entry in window_entries]
    versions = sorted({entry["metric_version"] for entry in window_entries})
    return {
        "policy": normalized_policy,
        "size": normalized_size,
        "unit": "runs",
        "status": status,
        "status_label": STATUS_LABELS[status],
        "anchor_run_id": last_entry["run_id"],
        "anchor_snapshot_id": last_entry["snapshot_id"],
        "window_start_run_id": run_ids[0] if run_ids else None,
        "window_end_run_id": run_ids[-1] if run_ids else None,
        "window_run_ids": run_ids,
        "window_snapshot_ids": [entry["snapshot_id"] for entry in window_entries],
        "entry_count": len(window_entries),
        "metric_versions": versions,
        "mixed_versions": len(versions) > 1,
        "consistent_count": sum(1 for entry in window_entries if not entry["deviating"]),
        "deviation_count": sum(1 for entry in window_entries if entry["deviating"]),
        "current_verdict": last_entry["verdict"],
        "deviation_start_run_id": last_episode["start_run_id"] if last_episode else None,
        "deviation_end_run_id": last_episode["end_run_id"] if last_episode else None,
        "deviation_start_before_window": bool(
            last_episode and last_episode["start_before_window"]
        ),
        "lookback": lookback,
        "episodes": episodes,
        "transitions": transitions,
        "entries": window_entries,
        "degraded": bool(degraded_ids),
        "degraded_snapshot_ids": degraded_ids,
        "degraded_notice": DEGRADED_NOTICE if degraded_ids else None,
        "digest": _digest(digest_payload),
    }


def recompute_metric_window(
    conn: sqlite3.Connection,
    metric_key: str,
    *,
    policy: str = DEFAULT_POLICY,
    size: int = DEFAULT_WINDOW_SIZE,
    anchor_snapshot_id: int | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Recompute one metric window incrementally and write it back.

    The window block is attached to the anchor snapshot's ``summary_json``
    only.  A second recompute against unchanged snapshots yields the same
    block and is a no-op: ``changed`` is ``False`` and no UPDATE is issued,
    so no duplicate write-back can accumulate.
    """
    normalized_policy, normalized_size = normalize_window(policy, size)
    collector = WINDOW_POLICIES[normalized_policy]
    rows = collector(conn, metric_key, normalized_size, anchor_snapshot_id)
    if not rows:
        return {"changed": False, "snapshot_id": None, "run_id": None,
                "window": None, "rows_scanned": 0}
    anchor = rows[0]
    block = compute_window(rows, metric_key, normalized_policy, normalized_size)
    anchor_id = int(anchor["id"])
    if not write:
        return {"changed": False, "snapshot_id": anchor_id,
                "run_id": int(anchor["run_id"]), "window": block,
                "rows_scanned": len(rows)}
    existing = _json_object(anchor["summary_json"]) or {}
    if existing.get("window") == block:
        return {"changed": False, "snapshot_id": anchor_id,
                "run_id": int(anchor["run_id"]), "window": block,
                "rows_scanned": len(rows)}
    existing["window"] = block
    conn.execute(
        "UPDATE snapshots SET summary_json = ? WHERE id = ?",
        (json.dumps(existing, ensure_ascii=False, sort_keys=True), anchor_id),
    )
    conn.commit()
    return {"changed": True, "snapshot_id": anchor_id,
            "run_id": int(anchor["run_id"]), "window": block,
            "rows_scanned": len(rows)}


def resolve_snapshot_window(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    *,
    policy: str = DEFAULT_POLICY,
    size: int = DEFAULT_WINDOW_SIZE,
) -> tuple[dict[str, Any] | None, str]:
    """Return ``(window_block, source)`` for a decoded snapshot.

    Snapshots written by a normal run already carry the block.  Legacy
    snapshots (or snapshots from a failed run) fall back to a read-only
    derivation anchored at that snapshot, so report export never breaks.
    """
    normalized_policy, normalized_size = normalize_window(policy, size)
    summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    block = summary.get("window")
    if (
        isinstance(block, dict)
        and block.get("policy") == normalized_policy
        and block.get("size") == normalized_size
    ):
        return block, "snapshot"
    rows = collect_run_count(
        conn, str(snapshot["metric_key"]), normalized_size, int(snapshot["id"])
    )
    if not rows:
        return None, "unavailable"
    return (
        compute_window(
            rows,
            str(snapshot["metric_key"]),
            normalized_policy,
            normalized_size,
        ),
        "derived_on_read",
    )


def metric_window(
    conn: sqlite3.Connection,
    metric_key: str,
    *,
    policy: str = DEFAULT_POLICY,
    size: int = DEFAULT_WINDOW_SIZE,
    recompute: bool = False,
) -> dict[str, Any]:
    """Return the newest window for a metric, deriving it on read if absent.

    ``recompute=True`` performs an incremental, idempotent write-back first.
    """
    normalized_policy, normalized_size = normalize_window(policy, size)
    if recompute:
        result = recompute_metric_window(
            conn, metric_key, policy=normalized_policy, size=normalized_size,
        )
        if result["window"] is not None:
            return {
                **result["window"],
                "source": "recomputed",
                "changed": result["changed"],
                "rows_scanned": result["rows_scanned"],
            }
        return {"metric_key": metric_key, "source": "unavailable",
                "status": None, "degraded": False, "changed": False}
    row = conn.execute(
        "SELECT id, run_id, metric_key, summary_json FROM snapshots "
        "WHERE metric_key = ? ORDER BY id DESC LIMIT 1",
        (metric_key,),
    ).fetchone()
    if row is None:
        return {"metric_key": metric_key, "source": "unavailable",
                "status": None, "degraded": False, "changed": False}
    summary = _json_object(row["summary_json"]) or {}
    block = summary.get("window")
    if (
        isinstance(block, dict)
        and block.get("policy") == normalized_policy
        and block.get("size") == normalized_size
    ):
        return {**block, "metric_key": metric_key, "source": "snapshot", "changed": False}
    # No block yet, or the caller asked for a different policy/size than the
    # one recorded at write time: derive read-only, never write in a GET.
    rows = collect_run_count(
        conn, metric_key, normalized_size, int(row["id"])
    )
    if not rows:
        return {"metric_key": metric_key, "source": "unavailable",
                "status": None, "degraded": False, "changed": False}
    return {
        **compute_window(rows, metric_key, normalized_policy, normalized_size),
        "metric_key": metric_key,
        "source": "derived_on_read",
        "changed": False,
    }


def all_metric_windows(
    conn: sqlite3.Connection,
    *,
    policy: str = DEFAULT_POLICY,
    size: int = DEFAULT_WINDOW_SIZE,
    recompute: bool = False,
) -> list[dict[str, Any]]:
    keys = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT metric_key FROM snapshots ORDER BY metric_key"
        ).fetchall()
    ]
    return [
        metric_window(conn, key, policy=policy, size=size, recompute=recompute)
        for key in keys
    ]
