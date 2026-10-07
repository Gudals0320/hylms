"""Internal, dependency-injected workflow coordinator; no production adapters.

See docs/orchestration.md for the adapter contract. Only committed references
cross transaction boundaries. Payloads remain internal and never enter reports.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from .core import HylmsError
from .qa import prepare_qa_packet, qa_next_action


STAGES = ("collect", "prepare", "interpret", "preview", "qa", "commit",
          "project", "ntfy", "ics", "google")
STATUSES = ("success", "warning", "partial_failure", "failed", "skipped")
WORK_STAGES = {"collect", "commit", "ntfy", "ics", "google"}
_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")


@dataclass(frozen=True)
class InputRefs:
    snapshot_run: str
    cursor: str
    snapshot_ref: str
    state_ref: str


@dataclass(frozen=True)
class WorkflowContext:
    execution_id: str
    source_ref: str
    inputs: InputRefs | None = None


@dataclass(frozen=True)
class RunPair:
    previous_run: str
    current_run: str
    packet: Mapping[str, Any]
    has_text_changes: bool


@dataclass(frozen=True)
class Preview:
    semantic_mutation: bool
    # The existing Build #9 preview's qa_context; no new candidate schema.
    qa_context: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class CommitReceipt:
    inputs: InputRefs
    new_pending: int = 0


@dataclass(frozen=True)
class StepRequest:
    context: WorkflowContext
    inputs: InputRefs | None
    pair: RunPair | None = None
    attempt: int = 1
    decision: Any = None
    preview: Preview | None = None
    qa_packet: Mapping[str, Any] | None = None
    qa_verdict: Mapping[str, Any] | None = None
    commit_mode: str | None = None
    projection: Any = None


@dataclass(frozen=True)
class StepOutcome:
    status: str = "success"
    data: Any = None
    counts: Mapping[str, int] = field(default_factory=dict)
    codes: tuple[str, ...] = ()
    fallback: bool = False


Adapter = Callable[[StepRequest], StepOutcome]


def _invalid() -> None:
    raise HylmsError("orchestration_invalid_result", "Invalid adapter result")


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 512 and not any(
        ord(char) < 32 for char in value
    )


def _refs(value: Any) -> bool:
    return isinstance(value, InputRefs) and all(_identifier(item) for item in (
        value.snapshot_run, value.cursor, value.snapshot_ref, value.state_ref
    ))


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _validate_outcome(outcome: Any) -> None:
    if not isinstance(outcome, StepOutcome) or not isinstance(outcome.status, str) or outcome.status not in STATUSES[:-1]:
        _invalid()
    if type(outcome.fallback) is not bool or not isinstance(outcome.counts, Mapping):
        _invalid()
    if any(not isinstance(key, str) or not _CODE.fullmatch(key) or not _count(value)
           for key, value in outcome.counts.items()):
        _invalid()
    if not isinstance(outcome.codes, tuple) or any(
        not isinstance(code, str) or not _CODE.fullmatch(code) for code in outcome.codes
    ):
        _invalid()
    if outcome.status in {"failed", "partial_failure"} and not outcome.codes:
        _invalid()
    if outcome.status == "partial_failure" and not (
        outcome.counts.get("succeeded", 0) > 0 and outcome.counts.get("failed", 0) > 0
    ):
        _invalid()
    if outcome.status == "failed" and outcome.counts.get("succeeded", 0):
        _invalid()
    if outcome.status in {"success", "warning"} and outcome.counts.get("failed", 0):
        _invalid()


class _Run:
    def __init__(self, context: WorkflowContext, adapters: Mapping[str, Adapter], started: str):
        self.context = context
        self.adapters = adapters
        self.inputs = context.inputs
        self.steps: list[dict[str, Any]] = []
        self.started = started
        self.pairs_committed = 0
        self.new_pending = 0
        self.fallback = False

    def request(self, **kwargs: Any) -> StepRequest:
        return StepRequest(self.context, self.inputs, **kwargs)

    def record(self, stage: str, request: StepRequest, outcome: StepOutcome) -> None:
        self.steps.append({
            "stage": stage, "status": outcome.status, "attempt": request.attempt,
            "pair": None if request.pair is None else {
                "previous_run": request.pair.previous_run,
                "current_run": request.pair.current_run,
            },
            "input": self.provenance(request.inputs),
            "counts": dict(outcome.counts), "codes": list(outcome.codes),
            "fallback": self.fallback or outcome.fallback,
        })

    @staticmethod
    def provenance(inputs: InputRefs | None) -> dict[str, str] | None:
        if inputs is None:
            return None
        return {"snapshot_run": inputs.snapshot_run, "cursor": inputs.cursor}

    def call(self, stage: str, request: StepRequest,
             validate: Callable[[StepOutcome], None] | None = None) -> StepOutcome:
        try:
            # An adapter cannot mutate another stage's packet or saved context.
            outcome = copy.deepcopy(self.adapters[stage](copy.deepcopy(request)))
            _validate_outcome(outcome)
            if validate is not None:
                validate(outcome)
        except HylmsError as exc:
            code = exc.code if isinstance(exc.code, str) and _CODE.fullmatch(exc.code) else "orchestration_stage_error"
            outcome = StepOutcome("failed", codes=(code,))
        except Exception:
            outcome = StepOutcome("failed", codes=("orchestration_unexpected_error",))
        # KeyboardInterrupt/SystemExit intentionally propagate: no later writes.
        self.record(stage, request, outcome)
        return outcome

    def skip(self, stage: str, code: str, request: StepRequest | None = None) -> None:
        self.record(stage, request or self.request(), StepOutcome("skipped", codes=(code,)))

    @staticmethod
    def ok(outcome: StepOutcome) -> bool:
        return outcome.status in {"success", "warning"}

    def collect(self) -> None:
        def validate(outcome: StepOutcome) -> None:
            if outcome.data is None:
                if self.ok(outcome):
                    _invalid()
                return
            if not _refs(outcome.data):
                _invalid()
            if self.context.inputs is not None and (
                outcome.data.state_ref != self.context.inputs.state_ref
                or outcome.data.cursor != self.context.inputs.cursor
            ):
                _invalid()  # Collection never advances the semantic state.
            if not self.ok(outcome) and not outcome.fallback:
                _invalid()

        outcome = self.call("collect", self.request(), validate)
        if outcome.data is not None:
            self.inputs = outcome.data
        self.fallback = self.inputs is not None and (outcome.fallback or not self.ok(outcome))
        self.steps[-1]["fallback"] = self.fallback
        self.steps[-1]["output"] = self.provenance(self.inputs)

    def process_pairs(self) -> None:
        seen = {self.inputs.cursor} if self.inputs else set()
        while self.inputs is not None:
            def validate(outcome: StepOutcome) -> None:
                if not self.ok(outcome) or outcome.data is None:
                    return
                pair = outcome.data
                if not isinstance(pair, RunPair) or not (
                    _identifier(pair.previous_run) and _identifier(pair.current_run)
                    and isinstance(pair.packet, Mapping) and type(pair.has_text_changes) is bool
                    and pair.previous_run == self.inputs.cursor and pair.current_run not in seen
                ):
                    _invalid()

            prepared = self.call("prepare", self.request(), validate)
            if not self.ok(prepared):
                self.fallback = True
                for stage in ("interpret", "preview", "qa", "commit"):
                    self.skip(stage, "prepare_failed")
                break
            if prepared.data is None:
                break
            pair = prepared.data
            self.steps[-1]["pair"] = {"previous_run": pair.previous_run, "current_run": pair.current_run}
            if not self.process_pair(pair):
                self.fallback = True
                last = self.steps[-1]
                remaining = ("interpret", "preview", "qa", "commit")
                for stage in remaining[remaining.index(last["stage"]) + 1:]:
                    self.skip(stage, "pair_failed", self.request(pair=pair, attempt=last["attempt"]))
                break
            seen.add(pair.current_run)

    def process_pair(self, pair: RunPair) -> bool:
        decision = None
        feedback = None
        for attempt in (1, 2):
            request = self.request(pair=pair, attempt=attempt, decision=decision, qa_verdict=feedback)
            if pair.has_text_changes or attempt == 2:
                def validate_decision(outcome: StepOutcome) -> None:
                    if self.ok(outcome) and not isinstance(outcome.data, Mapping):
                        _invalid()
                interpreted = self.call("interpret", request, validate_decision)
                if not self.ok(interpreted):
                    return False
                decision = interpreted.data
            else:
                self.skip("interpret", "no_text_changes", request)

            request = replace(request, decision=decision)
            def validate_preview(outcome: StepOutcome) -> None:
                if not self.ok(outcome):
                    return
                preview = outcome.data
                if not isinstance(preview, Preview) or type(preview.semantic_mutation) is not bool:
                    _invalid()
                if preview.semantic_mutation:
                    if not isinstance(preview.qa_context, Mapping) or preview.qa_context.get("mode") != "automatic":
                        _invalid()
                    prepare_qa_packet(preview.qa_context, attempt=attempt,
                                      review_id=self.context.execution_id)

            previewed = self.call("preview", request, validate_preview)
            if not self.ok(previewed):
                return False
            preview = previewed.data
            request = replace(request, preview=preview)
            if not preview.semantic_mutation:
                self.skip("qa", "no_semantic_mutation", request)
                return self.commit(replace(request, qa_verdict=None, commit_mode="approved"))

            packet = prepare_qa_packet(preview.qa_context, attempt=attempt,
                                       review_id=self.context.execution_id)
            request = replace(request, qa_packet=packet)
            def validate_review(outcome: StepOutcome) -> None:
                if self.ok(outcome):
                    qa_next_action(packet, outcome.data)

            reviewed = self.call("qa", request, validate_review)
            if not self.ok(reviewed):
                return False
            action = qa_next_action(packet, reviewed.data)
            self.steps[-1]["qa_action"] = action
            if action == "fail":
                self.steps[-1].update(status="failed", codes=["phase2_qa_not_approved"])
                return False
            if action == "revise":
                feedback = reviewed.data
                continue
            if action == "pending":
                # Uncertainty about a candidate is not a student obligation.
                # Real academic uncertainty must already be a concrete pending
                # candidate which QA can approve. Keep cursor/state for retry.
                self.steps[-1].update(status="failed", codes=["phase2_qa_review_required"])
                return False
            return self.commit(replace(request, qa_verdict=reviewed.data,
                                       commit_mode="pending" if action == "pending" else "approved"))
        return False  # qa_next_action makes the second review terminal.

    def commit(self, request: StepRequest) -> bool:
        def validate(outcome: StepOutcome) -> None:
            if not self.ok(outcome):
                if outcome.status == "partial_failure":
                    _invalid()  # A state transaction is atomic, never partial.
                return
            receipt = outcome.data
            if not isinstance(receipt, CommitReceipt) or not _refs(receipt.inputs) or not _count(receipt.new_pending):
                _invalid()
            if (receipt.inputs.cursor != request.pair.current_run
                    or receipt.inputs.snapshot_run != self.inputs.snapshot_run
                    or receipt.inputs.snapshot_ref != self.inputs.snapshot_ref):
                _invalid()

        outcome = self.call("commit", request, validate)
        self.steps[-1]["commit_mode"] = request.commit_mode
        if not self.ok(outcome):
            return False
        self.inputs = outcome.data.inputs
        self.pairs_committed += 1
        self.new_pending += outcome.data.new_pending
        self.steps[-1]["output"] = self.provenance(self.inputs)
        if outcome.data.new_pending or request.commit_mode == "pending":
            self.steps[-1]["status"] = "warning"
            self.steps[-1]["codes"].append("pending_confirmation")
        return True

    def outputs(self) -> None:
        if self.inputs is None:
            for stage in ("prepare", "project", "ntfy", "ics", "google"):
                self.skip(stage, "no_usable_input")
            return

        def validate(outcome: StepOutcome) -> None:
            if self.ok(outcome) and not isinstance(outcome.data, Mapping):
                _invalid()

        projected = self.call("project", self.request(), validate)
        # Each output owns its preparation. Failure of shared calendar projection
        # cannot block ntfy or Google, which also receive the committed references.
        projection = projected.data if self.ok(projected) else None
        self.call("ntfy", self.request(projection=projection))
        if projection is None:
            self.skip("ics", "projection_unavailable")
        else:
            self.call("ics", self.request(projection=projection))
        self.call("google", self.request(projection=projection))

    def result(self) -> dict[str, Any]:
        fallback = self.fallback or any(step["fallback"] for step in self.steps)
        counts = {status: sum(step["status"] == status for step in self.steps) for status in STATUSES}
        failed = counts["failed"] + counts["partial_failure"] > 0
        succeeded = any(
            step["stage"] in WORK_STAGES and (step["status"] in {"success", "warning"}
                or (step["status"] == "partial_failure" and step["counts"].get("succeeded", 0) > 0))
            for step in self.steps
        )
        status = ("partial_failure" if succeeded else "failed") if failed else (
            "warning" if counts["warning"] or fallback else "success"
        )
        return {
            "schema_version": 1, "execution_id": self.context.execution_id,
            "started_at": self.started, "status": status,
            "input": self.provenance(self.context.inputs), "output": self.provenance(self.inputs),
            "fallback": fallback, "pairs_committed": self.pairs_committed,
            "new_pending": self.new_pending, "stage_counts": counts, "steps": self.steps,
        }


def run_workflow(context: WorkflowContext, adapters: Mapping[str, Adapter], *,
                 clock: Callable[[], dt.datetime]) -> dict[str, Any]:
    """Run one internal workflow using explicitly supplied adapters and clock.

    Configuration errors raise ValueError before calling adapters. Operational
    errors become stage results; user/process cancellation propagates immediately.
    There are no default adapters, external calls, retries, or persistent queues.
    """
    if not isinstance(context, WorkflowContext) or not (
        _identifier(context.execution_id) and _identifier(context.source_ref)
        and (context.inputs is None or _refs(context.inputs))
    ):
        raise ValueError("Invalid workflow context")
    if not isinstance(adapters, Mapping) or set(adapters) != set(STAGES) or not all(
        callable(adapter) for adapter in adapters.values()
    ):
        raise ValueError("All workflow adapters must be supplied explicitly")
    started = clock()
    if not isinstance(started, dt.datetime) or started.utcoffset() is None:
        raise ValueError("Workflow clock must return a timezone-aware datetime")
    run = _Run(copy.deepcopy(context), dict(adapters), started.isoformat())
    run.collect()
    run.process_pairs()
    run.outputs()
    return run.result()
