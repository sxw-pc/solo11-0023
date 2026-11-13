"""Bounded concurrent executor for per-rule batch evaluation.

The previous batch entry point consumed the rule list strictly one rule at a
time: a slow rule blocked every later rule and there was no upper bound on the
number of concurrently live evaluation resources.  This module keeps the same
"consume a list of rules" model but adds:

* a configurable concurrency ceiling (``max_workers``) — new rules queue on a
  bounded semaphore instead of materialising an unbounded amount of work;
* failure isolation — an exception raised by one rule is captured in its own
  outcome and never aborts the other rules or tears down the pool;
* deterministic resource reclamation — every worker owns an isolated scope
  (its own read-only SQLite connection, ...) that is released on the normal,
  error and cancellation paths, so a stuck/failed rule can never leak an
  executor or hold the concurrency pool hostage.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Generator, Generic, Iterable, TypeVar

# Per-rule terminal states.
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
RULE_STATUSES = (STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED)

DEFAULT_MAX_WORKERS = 4
MAX_HARD_WORKER_LIMIT = 64

T = TypeVar("T")
R = TypeVar("R")


@dataclass(slots=True)
class RuleOutcome(Generic[T, R]):
    """Final state of one rule after going through the pool."""

    key: str
    index: int
    status: str
    result: R | None = None
    error: str | None = None
    error_code: str | None = None
    duration_ms: float = 0.0
    worker: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS


class WorkerScope(Generic[T]):
    """Resources bound to one worker thread for its whole lifetime.

    A scope is created lazily the first time a worker picks up a rule and is
    closed exactly once when the pool drains, regardless of how execution
    ends.  ``resource`` is typically a private read-only database connection.
    """

    def __init__(self, name: str, resource: T) -> None:
        self.name = name
        self.resource = resource

    def close(self) -> None:  # pragma: no cover - overridden by implementations
        """Release everything owned by this scope; must be idempotent."""


ScopeFactory = Callable[[str], WorkerScope[T]]
RuleTask = Callable[[T, "RuleContext"], R]


@dataclass(slots=True)
class RuleContext:
    """Per-rule bookkeeping handed to the task callable."""

    key: str
    index: int
    worker: str
    cancel_event: threading.Event

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


@dataclass(slots=True)
class _WorkerState(Generic[T]):
    name: str
    scope: WorkerScope[T] | None = None
    active_rules: int = 0


class ConcurrentRuleExecutor(Generic[T, R]):
    """Run rule tasks concurrently with a hard, configurable ceiling.

    The semaphore is the authoritative concurrency limiter: even though the
    thread pool is sized to ``max_workers`` as well, acquiring a permit before
    work starts guarantees the number of simultaneously live rule executions
    (and therefore live evaluation resources) can never exceed the configured
    peak.  Rules that cannot get a permit wait in FIFO order instead of being
    buffered without bound.
    """

    def __init__(
        self,
        scope_factory: ScopeFactory[T],
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        name_prefix: str = "caliber-rule",
    ) -> None:
        self.max_workers = self._validate_workers(max_workers)
        self._scope_factory = scope_factory
        self._name_prefix = name_prefix
        self._slots = threading.BoundedSemaphore(self.max_workers)
        self._cancel_event = threading.Event()
        self._states_lock = threading.Lock()
        self._states: dict[str, _WorkerState[T]] = {}
        self._peak = 0
        self._live = 0

    @staticmethod
    def _validate_workers(value: Any) -> int:
        try:
            workers = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_workers 必须是正整数") from exc
        if workers < 1:
            raise ValueError("max_workers 必须 >= 1")
        if workers > MAX_HARD_WORKER_LIMIT:
            raise ValueError(f"max_workers 不能超过 {MAX_HARD_WORKER_LIMIT}")
        return workers

    @property
    def peak_workers(self) -> int:
        """Highest number of rules that were ever executing simultaneously."""
        with self._states_lock:
            return self._peak

    @property
    def active_rule_count(self) -> int:
        """Rules currently holding a permit (for tests/diagnostics)."""
        with self._states_lock:
            return self._live

    def cancel(self) -> None:
        """Request cooperative cancellation; queued/running rules stop cleanly."""
        self._cancel_event.set()

    def _state_for(self, worker: str) -> _WorkerState[T]:
        with self._states_lock:
            state = self._states.get(worker)
            if state is None:
                state = _WorkerState(name=worker)
                self._states[worker] = state
            return state

    def _scope_for(self, state: _WorkerState[T]) -> WorkerScope[T]:
        if state.scope is None:
            # The factory owns resource creation; a failure here propagates out
            # of the task and is still covered by the rule-level except clause.
            state.scope = self._scope_factory(state.name)
        return state.scope

    def _run_rule(self, index: int, key: str, task: RuleTask[T, R]) -> RuleOutcome[T, R]:
        worker = threading.current_thread().name
        state = self._state_for(worker)
        started = time.monotonic()
        # Block here while the pool is saturated: this is the bounded queue.
        self._slots.acquire()
        try:
            with self._states_lock:
                self._live += 1
                state.active_rules += 1
                if self._live > self._peak:
                    self._peak = self._live
            try:
                if self._cancel_event.is_set():
                    return RuleOutcome(
                        key=key,
                        index=index,
                        status=STATUS_FAILED,
                        error="运行已取消，规则尚未执行",
                        error_code="cancelled",
                        duration_ms=round((time.monotonic() - started) * 1000, 3),
                        worker=worker,
                    )
                scope = self._scope_for(state)
                context = RuleContext(
                    key=key,
                    index=index,
                    worker=worker,
                    cancel_event=self._cancel_event,
                )
                result = task(scope.resource, context)
                return RuleOutcome(
                    key=key,
                    index=index,
                    status=STATUS_SUCCESS,
                    result=result,
                    duration_ms=round((time.monotonic() - started) * 1000, 3),
                    worker=worker,
                )
            except Exception as exc:  # failure isolation: one rule, one outcome
                code = getattr(exc, "code", None) or type(exc).__name__
                return RuleOutcome(
                    key=key,
                    index=index,
                    status=STATUS_FAILED,
                    error=str(exc) or type(exc).__name__,
                    error_code=code,
                    duration_ms=round((time.monotonic() - started) * 1000, 3),
                    worker=worker,
                )
            finally:
                # Counts are reclaimed on every exit: success, rule failure and
                # the cancelled-before-start path, so no slot can leak.
                with self._states_lock:
                    self._live -= 1
                    state.active_rules -= 1
        finally:
            # Every permit is released on success, failure and cancellation, so
            # a stuck/failed rule cannot occupy the pool forever.
            self._slots.release()

    def _close_scopes(self) -> None:
        with self._states_lock:
            states = list(self._states.values())
        for state in states:
            scope = state.scope
            if scope is None:
                continue
            state.scope = None
            try:
                scope.close()
            except Exception:
                # Resource cleanup must never mask other teardown work.
                pass

    def run_rules_stream(
        self, rules: Iterable[tuple[str, Any]], task: RuleTask[T, R]
    ) -> Generator[RuleOutcome[T, R], None, None]:
        """Yield each rule outcome as soon as it finishes.

        Outcomes are produced in completion order (callers that need input
        order should sort by ``index``).  The generator owns teardown: if the
        consumer breaks, raises or is interrupted, in-flight work is cancelled
        cooperatively, the pool is drained and every worker scope is closed —
        no executor thread or evaluation resource can survive the loop.
        """
        indexed: list[tuple[int, str, Any]] = [
            (index, key, payload)
            for index, (key, payload) in enumerate(rules)
        ]
        if not indexed:
            self._close_scopes()
            return
        # One worker per permit, never more: idle threads are not pre-spawned
        # beyond what the workload needs.
        pool_size = min(self.max_workers, len(indexed))
        pool = ThreadPoolExecutor(
            max_workers=pool_size,
            thread_name_prefix=self._name_prefix,
        )
        futures = {
            pool.submit(self._run_rule, index, key, task): index
            for index, key, _payload in indexed
        }
        try:
            for future in as_completed(futures):
                # _run_rule never raises: rule errors live on the outcome.
                yield future.result()
        except BaseException:
            # KeyboardInterrupt / SystemInterrupt while waiting, or a consumer
            # failure: stop queued work and let the finally reclaim everything.
            self._cancel_event.set()
            for future in futures:
                future.cancel()
            raise
        finally:
            # wait=True so worker threads (and their scopes) always wind down,
            # then close every scope exactly once.  No executor can survive.
            pool.shutdown(wait=True, cancel_futures=True)
            self._close_scopes()

    def run_rules(
        self, rules: Iterable[tuple[str, Any]], task: RuleTask[T, R]
    ) -> list[RuleOutcome[T, R]]:
        """Execute ``(key, payload)`` rules and return outcomes in input order.

        Outcomes are always returned ordered by the input list even though
        execution is concurrent, so callers keep deterministic, serial-like
        result semantics.
        """
        outcomes = list(self.run_rules_stream(rules, task))
        return sorted(outcomes, key=lambda item: item.index)
