"""Tests for the bounded concurrent rule executor itself."""
from __future__ import annotations

import threading
import time

import pytest

from app.core.executor import (
    DEFAULT_MAX_WORKERS,
    MAX_HARD_WORKER_LIMIT,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    ConcurrentRuleExecutor,
    RuleContext,
    WorkerScope,
)


class _RecordingScope(WorkerScope):
    """Scope backed by a fake resource that records open/close events."""

    def __init__(self, name: str, events: list[str], live: set[str]) -> None:
        super().__init__(name, name)
        self._events = events
        self._live = live
        self._events.append(f"open:{name}")
        self._live.add(name)

    def close(self) -> None:
        if self.resource is None:
            return
        self._events.append(f"close:{self.name}")
        self._live.discard(self.name)
        # Mirror the executor contract: a closed scope has no resource left,
        # so a later re-use on the same worker must build a fresh scope.
        self.resource = None

def _make_executor(events: list[str], live: set[str], max_workers: int):
    return ConcurrentRuleExecutor(
        lambda name: _RecordingScope(name, events, live),
        max_workers=max_workers,
    )


def test_default_worker_count_and_validation():
    assert DEFAULT_MAX_WORKERS >= 1
    executor = _make_executor([], set(), DEFAULT_MAX_WORKERS)
    assert executor.max_workers == DEFAULT_MAX_WORKERS
    for bad in (0, -1, "abc"):
        with pytest.raises(ValueError):
            ConcurrentRuleExecutor(lambda name: WorkerScope(name, None), max_workers=bad)
    with pytest.raises(ValueError):
        ConcurrentRuleExecutor(
            lambda name: WorkerScope(name, None), max_workers=MAX_HARD_WORKER_LIMIT + 1
        )


def test_outcomes_returned_in_input_order_even_when_completion_reorders():
    events: list[str] = []
    live: set[str] = set()
    executor = _make_executor(events, live, 4)

    def task(resource: str, context: RuleContext) -> str:
        # Lower index sleeps longer, so completion order differs from input.
        time.sleep(0.05 * (4 - context.index))
        return f"result:{context.key}"

    outcomes = executor.run_rules(
        [("a", None), ("b", None), ("c", None), ("d", None)], task
    )
    assert [item.key for item in outcomes] == ["a", "b", "c", "d"]
    assert [item.index for item in outcomes] == [0, 1, 2, 3]
    assert all(item.status == STATUS_SUCCESS for item in outcomes)
    assert [item.result for item in outcomes] == [
        "result:a",
        "result:b",
        "result:c",
        "result:d",
    ]


def test_peak_concurrency_never_exceeds_limit_and_rules_queue():
    events: list[str] = []
    live: set[str] = set()
    limit = 3
    executor = _make_executor(events, live, limit)
    observed_peak = 0
    peak_lock = threading.Lock()
    barrier = threading.Event()

    def task(resource: str, context: RuleContext) -> int:
        nonlocal observed_peak
        with peak_lock:
            observed_peak = max(observed_peak, executor.active_rule_count)
            assert executor.active_rule_count <= limit
        # Hold the slot until the main thread releases everyone, proving that
        # later rules queue instead of bursting past the ceiling.
        barrier.wait(timeout=5)
        return context.index

    rules = [(f"rule-{index}", None) for index in range(8)]
    done = []

    def consume():
        done.extend(executor.run_rules_stream(rules, task))

    thread = threading.Thread(target=consume)
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if executor.active_rule_count == limit:
            break
        time.sleep(0.01)
    assert executor.active_rule_count == limit
    assert executor.peak_workers <= limit
    barrier.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert executor.peak_workers == limit
    assert observed_peak == limit
    assert len(done) == 8
    assert {item.status for item in done} == {STATUS_SUCCESS}


def test_rule_failure_is_isolated_and_captured_per_rule():
    events: list[str] = []
    live: set[str] = set()
    executor = _make_executor(events, live, 4)

    def task(resource: str, context: RuleContext) -> str:
        if context.key == "boom":
            raise RuntimeError("规则内部爆炸")
        if context.key == "caliber":
            class _DomainError(Exception):
                code = "evaluation_timeout"
            raise _DomainError("查询超时")
        return f"ok:{context.key}"

    outcomes = executor.run_rules(
        [("ok-1", None), ("boom", None), ("caliber", None), ("ok-2", None)], task
    )
    statuses = {item.key: item.status for item in outcomes}
    assert statuses == {
        "ok-1": STATUS_SUCCESS,
        "boom": STATUS_FAILED,
        "caliber": STATUS_FAILED,
        "ok-2": STATUS_SUCCESS,
    }
    failed = {item.key: item for item in outcomes if item.status == STATUS_FAILED}
    assert "规则内部爆炸" in failed["boom"].error
    assert failed["boom"].error_code == "RuntimeError"
    assert failed["caliber"].error_code == "evaluation_timeout"
    # No scope leaked: every opened scope was closed exactly once.
    from collections import Counter

    opens = Counter(event.split(":", 1)[1] for event in events if event.startswith("open:"))
    closes = Counter(event.split(":", 1)[1] for event in events if event.startswith("close:"))
    assert opens == closes
    assert live == set()


def test_every_scope_is_closed_when_scope_creation_or_task_raises():
    live: set[str] = set()
    events: list[str] = []

    def factory(name: str) -> _RecordingScope:
        scope = _RecordingScope(name, events, live)
        if len(live) == 2:
            live.discard(name)
            events.append(f"close:{name}")
            raise RuntimeError("资源创建失败")
        return scope

    executor = ConcurrentRuleExecutor(factory, max_workers=3)

    def task(resource: str, context: RuleContext) -> str:
        time.sleep(0.02)
        return context.key

    outcomes = executor.run_rules(
        [(f"k{index}", None) for index in range(6)], task
    )
    # The rule whose scope failed is a normal failed outcome, not a pool crash.
    assert any(item.status == STATUS_FAILED for item in outcomes)
    assert sum(1 for item in outcomes if item.status == STATUS_SUCCESS) >= 1
    assert live == set()
    assert executor.active_rule_count == 0


def test_stream_break_reclaims_pool_and_all_scopes():
    events: list[str] = []
    live: set[str] = set()
    executor = _make_executor(events, live, 3)
    started = threading.Event()

    def task(resource: str, context: RuleContext) -> str:
        started.set()
        time.sleep(0.02)
        return context.key

    stream = executor.run_rules_stream(
        [(f"k{index}", None) for index in range(20)], task
    )
    first = next(stream)
    assert first.status == STATUS_SUCCESS
    started.wait(timeout=2)
    stream.close()  # consumer gives up early: generator must tear everything down

    assert live == set()
    assert executor.active_rule_count == 0
    # Thread pool threads are named; none may remain after teardown.
    leftovers = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("caliber-rule") and thread.is_alive()
    ]
    assert leftovers == []


def test_worker_scope_is_reused_across_rules_on_same_worker():
    events: list[str] = []
    live: set[str] = set()
    executor = _make_executor(events, live, 2)

    seen_workers: set[str] = set()

    def task(resource: str, context: RuleContext) -> str:
        seen_workers.add(context.worker)
        return context.key

    outcomes = executor.run_rules(
        [(f"k{index}", None) for index in range(8)], task
    )
    assert len(outcomes) == 8
    # Only two scopes should ever have been opened for two workers...
    opens = [event for event in events if event.startswith("open:")]
    assert len(opens) <= 2
    # ...but both are closed when the run ends.
    assert live == set()
    assert len(seen_workers) <= 2


def test_empty_rule_list_runs_nothing_and_leaves_nothing():
    events: list[str] = []
    live: set[str] = set()
    executor = _make_executor(events, live, 4)
    assert executor.run_rules([], lambda resource, ctx: None) == []
    assert events == []
    assert live == set()


def test_cancellation_marks_queued_work_without_leaking_slots():
    events: list[str] = []
    live: set[str] = set()
    executor = _make_executor(events, live, 2)
    release = threading.Event()

    def slow(resource: str, context: RuleContext) -> str:
        release.wait(timeout=5)
        if context.cancelled:
            raise RuntimeError("cancelled mid-flight")
        return context.key

    rules = [(f"k{index}", None) for index in range(6)]
    outcomes: list = []

    def consume():
        outcomes.extend(executor.run_rules_stream(rules, slow))

    thread = threading.Thread(target=consume)
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and executor.active_rule_count < 2:
        time.sleep(0.01)
    executor.cancel()
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(outcomes) == 6
    # Every permit came back: nothing remains in flight and no scope survives.
    assert executor.active_rule_count == 0
    assert live == set()
    assert all(item.status in (STATUS_SUCCESS, STATUS_FAILED) for item in outcomes)
    # After cancellation the semaphore must be fully acquirable again.
    for _ in range(2):
        assert executor._slots.acquire(timeout=1)
    for _ in range(2):
        executor._slots.release()


def test_skipped_constant_is_part_of_public_contract():
    assert STATUS_SKIPPED == "skipped"


def test_outcome_ok_property_reflects_status():
    from app.core.executor import RuleOutcome

    success = RuleOutcome(key="a", index=0, status=STATUS_SUCCESS, result="x")
    failure = RuleOutcome(key="b", index=1, status=STATUS_FAILED, error="boom")
    assert success.ok is True
    assert failure.ok is False


def test_scope_close_exception_does_not_mask_teardown():
    closed: list[str] = []
    bad_names = {"caliber-rule_1"}

    class MixedScope(WorkerScope):
        def close(self) -> None:
            if self.resource is None:
                return
            if self.name in bad_names:
                self.resource = None
                raise RuntimeError("关闭失败也不能泄漏其他资源")
            closed.append(self.name)
            self.resource = None

    executor = ConcurrentRuleExecutor(
        lambda name: MixedScope(name, name), max_workers=2
    )
    # Three rules across two workers; one worker's scope raises on close, but
    # teardown must still close the other scope and finish cleanly.
    outcomes = executor.run_rules(
        [("k1", None), ("k2", None), ("k3", None)], lambda resource, ctx: ctx.key
    )
    assert all(item.status == STATUS_SUCCESS for item in outcomes)
    assert closed  # the healthy scope was still closed
    assert all(name not in bad_names for name in closed)
    leftovers = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("caliber-rule") and thread.is_alive()
    ]
    assert leftovers == []
