"""Workflow acceptance fixtures. No production adapters are installed or used."""

from __future__ import annotations

import copy
import datetime as dt
import json
from dataclasses import replace
from unittest import TestCase, mock

from hylms.core import HylmsError
from hylms.orchestration import (
    STAGES, CommitReceipt, InputRefs, Preview, RunPair, StepOutcome,
    WorkflowContext, run_workflow,
)


NOW = dt.datetime(2026, 9, 5, 12, tzinfo=dt.timezone.utc)
BASE = InputRefs("r0", "r0", "memory:snapshots", "memory:state:r0")
CONTEXT = WorkflowContext("fixture-run", "memory:source", BASE)


class FakeWorld:
    """Synthetic runs and an in-memory committed state, plus scripted failures.

    Rules receive isolated requests. No parsing, real collection, external I/O,
    or replacement implementation of the existing state validator lives here.
    """

    def __init__(self, *, runs=1, text=True, semantic=True, verdicts=("pass",)):
        self.runs = runs
        self.text = text
        self.semantic = semantic
        self.verdicts = verdicts
        self.calls = []
        self.commits = []
        self.outputs = {}
        self.rules = {}
        self.state = {"cursor": "r0", "accepted": [], "pending": []}

    @property
    def adapters(self):
        return {stage: (lambda request, stage=stage: self.execute(stage, request)) for stage in STAGES}

    def execute(self, stage, request):
        self.calls.append((stage, copy.deepcopy(request)))
        key = (stage, request.pair.current_run if request.pair else None, request.attempt)
        rule = self.rules.get(key, self.rules.get(stage))
        if rule is not None:
            if isinstance(rule, BaseException):
                raise rule
            return rule(request) if callable(rule) else rule
        return getattr(self, stage)(request)

    def collect(self, request):
        return StepOutcome(data=replace(request.inputs or BASE, snapshot_run=f"r{self.runs}"))

    def prepare(self, request):
        current = int(request.inputs.cursor[1:])
        if current == self.runs:
            return StepOutcome()
        return StepOutcome(data=RunPair(f"r{current}", f"r{current + 1}",
                                       {"fixture": "PRIVATE-PACKET"}, self.text))

    def interpret(self, request):
        return StepOutcome(data={"fixture": "PRIVATE-DECISION", "attempt": request.attempt})

    def preview(self, request):
        if not self.semantic:
            return StepOutcome(data=Preview(False))
        return StepOutcome(data=Preview(True, {
            "mode": "automatic", "transaction_id": request.pair.current_run,
            "candidate_state_sha256": f"candidate-{request.attempt}",
            "changes": [], "structured_changes": [], "current_entities": [],
            "candidate_entities": [{"id": "fixture-entity"}], "rules": [], "decision_draft": request.decision or {},
            "instruction": None,
        }))

    def qa(self, request):
        verdict = self.verdicts[min(request.attempt - 1, len(self.verdicts) - 1)]
        packet = request.qa_packet
        return StepOutcome(data={
            "schema_version": 1, "qa_packet_id": packet["qa_packet_id"],
            "review_id": packet["review_id"], "attempt": request.attempt,
            "verdict": verdict, "issues": [] if verdict == "pass" else [{
                "code": "fixture_issue", "message": "PRIVATE-REVIEW", "change_ids": [],
                "entity_ids": ["fixture-entity"], "field_paths": [],
            }],
        })

    def commit(self, request):
        self.commits.append(copy.deepcopy(request))
        self.state["cursor"] = request.pair.current_run
        bucket = "pending" if request.commit_mode == "pending" else "accepted"
        self.state[bucket].append(request.pair.current_run)
        return StepOutcome(data=CommitReceipt(replace(
            request.inputs, cursor=request.pair.current_run,
            state_ref=f"memory:state:{request.pair.current_run}",
        ), new_pending=int(bucket == "pending")), counts={"succeeded": 1})

    def project(self, request):
        return StepOutcome(data={"fixture": "PRIVATE-EVENT", "cursor": request.inputs.cursor})

    def _output(self, name, request):
        self.outputs[name] = request
        return StepOutcome(counts={"succeeded": 1, "unchanged": 1})

    def ntfy(self, request):
        return self._output("ntfy", request)

    def ics(self, request):
        return self._output("ics", request)

    def google(self, request):
        return self._output("google", request)

    def run(self, context=CONTEXT):
        return run_workflow(context, self.adapters, clock=lambda: NOW)


class OrchestrationTests(TestCase):
    def setUp(self):
        # Fail closed if a future edit accidentally adds production side effects.
        for target in (
            "socket.socket", "urllib.request.urlopen", "webbrowser.open",
            "subprocess.Popen", "hylms.credentials.WindowsCredentialStore._read_bytes",
            "hylms.credentials.WindowsCredentialStore._write_bytes",
            "hylms.credentials.WindowsCredentialStore._delete_target",
        ):
            patch = mock.patch(target, side_effect=AssertionError("Forbidden production I/O"))
            self.addCleanup(patch.stop)
            blocked = patch.start()
            self.addCleanup(blocked.assert_not_called)

    def step(self, result, stage, index=0):
        return [item for item in result["steps"] if item["stage"] == stage][index]

    def names(self, world):
        return [name for name, _ in world.calls]

    def test_normal_end_to_end_and_report_provenance(self):
        world = FakeWorld()
        result = world.run()
        self.assertEqual(self.names(world), ["collect", "prepare", "interpret", "preview", "qa",
                                           "commit", "prepare", "project", "ntfy", "ics", "google"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"], {"snapshot_run": "r1", "cursor": "r1"})
        self.assertEqual(result["pairs_committed"], 1)
        self.assertEqual(result["stage_counts"]["success"], 11)
        self.assertEqual(world.state["accepted"], ["r1"])
        for name in ("ntfy", "ics", "google"):
            self.assertEqual(self.step(result, name)["input"]["cursor"], "r1")
            self.assertEqual(world.outputs[name].inputs.state_ref, "memory:state:r1")
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_no_unprocessed_run_still_attempts_all_outputs(self):
        world = FakeWorld(runs=0)
        result = world.run()
        self.assertEqual(self.names(world), ["collect", "prepare", "project", "ntfy", "ics", "google"])
        self.assertEqual(result["pairs_committed"], 0)
        self.assertEqual(result["output"]["cursor"], "r0")
        self.assertEqual(world.commits, [])

    def test_cursor_only_skips_interpret_and_qa_but_previews(self):
        world = FakeWorld(text=False, semantic=False)
        result = world.run()
        self.assertNotIn("interpret", self.names(world))
        self.assertNotIn("qa", self.names(world))
        self.assertIn("preview", self.names(world))
        self.assertEqual(result["pairs_committed"], 1)
        self.assertEqual(self.step(result, "qa")["codes"], ["no_semantic_mutation"])

    def test_structured_mutation_still_requires_qa(self):
        world = FakeWorld(text=False, semantic=True)
        world.run()
        self.assertNotIn("interpret", self.names(world))
        self.assertIn("qa", self.names(world))
        self.assertIsNotNone(world.commits[0].qa_packet)

    def test_text_change_with_no_mutation_skips_qa(self):
        world = FakeWorld(semantic=False)
        world.run()
        self.assertIn("interpret", self.names(world))
        self.assertNotIn("qa", self.names(world))

    def test_multiple_pairs_read_latest_committed_reference(self):
        world = FakeWorld(runs=3)
        result = world.run()
        self.assertEqual([item.inputs.cursor for item in world.commits], ["r0", "r1", "r2"])
        self.assertEqual(result["pairs_committed"], 3)
        self.assertEqual(result["output"]["cursor"], "r3")
        self.assertEqual(self.step(result, "prepare")["pair"]["current_run"], "r1")

    def test_failure_in_second_pair_preserves_first_commit_and_stops_third(self):
        for stage in ("interpret", "preview", "qa", "commit"):
            with self.subTest(stage=stage):
                world = FakeWorld(runs=3)
                world.rules[(stage, "r2", 1)] = HylmsError("fixture_failed", "PRIVATE")
                result = world.run()
                self.assertEqual(world.state["cursor"], "r1")
                self.assertEqual(world.state["accepted"], ["r1"])
                self.assertFalse(any(req.pair and req.pair.current_run == "r3" for _, req in world.calls))
                self.assertEqual(result["pairs_committed"], 1)
                self.assertEqual(result["status"], "partial_failure")
                self.assertTrue(result["fallback"])
                self.assertEqual(world.outputs["google"].inputs.cursor, "r1")
                if stage != "commit":
                    skipped_commit = self.step(result, "commit", 1)
                    self.assertEqual(skipped_commit["status"], "skipped")
                    self.assertEqual(skipped_commit["codes"], ["pair_failed"])

    def test_prepare_failure_keeps_cursor_and_continues(self):
        world = FakeWorld()
        world.rules["prepare"] = RuntimeError("SECRET-error")
        result = world.run()
        self.assertEqual(result["output"]["cursor"], "r0")
        self.assertEqual(self.names(world)[-3:], ["ntfy", "ics", "google"])
        self.assertEqual(self.step(result, "prepare")["codes"], ["orchestration_unexpected_error"])

    def test_revision_repreviews_and_binds_new_candidate(self):
        world = FakeWorld(verdicts=("revise", "pass"))
        result = world.run()
        self.assertEqual([req.attempt for name, req in world.calls if name == "qa"], [1, 2])
        second = [req for name, req in world.calls if name == "interpret"][1]
        self.assertEqual(second.qa_verdict["verdict"], "revise")
        self.assertEqual(world.commits[0].qa_packet["candidate_state_sha256"], "candidate-2")
        self.assertEqual(result["status"], "success")

    def test_revision_may_remove_semantic_change(self):
        world = FakeWorld(verdicts=("revise",))
        world.rules[("preview", "r1", 2)] = StepOutcome(data=Preview(False))
        result = world.run()
        self.assertEqual(self.names(world).count("qa"), 1)
        self.assertIsNone(world.commits[0].qa_verdict)
        self.assertIsNone(world.commits[0].qa_packet)
        self.assertEqual(result["status"], "success")

    def test_pending_requires_system_review_without_commit_or_cursor_advance(self):
        for verdicts in (("pending",), ("revise", "pending"), ("revise", "revise")):
            with self.subTest(verdicts=verdicts):
                world = FakeWorld(verdicts=verdicts)
                result = world.run()
                self.assertEqual(result["status"], "partial_failure")
                self.assertEqual(result["new_pending"], 0)
                self.assertEqual(world.commits, [])
                self.assertEqual(world.state["pending"], [])
                self.assertEqual(world.state["cursor"], "r0")
                self.assertLessEqual(self.names(world).count("qa"), 2)

    def test_failed_verdict_never_commits(self):
        for verdicts in (("failed",), ("revise", "failed")):
            with self.subTest(verdicts=verdicts):
                world = FakeWorld(verdicts=verdicts)
                result = world.run()
                self.assertEqual(world.commits, [])
                self.assertEqual(result["output"]["cursor"], "r0")
                self.assertEqual(result["status"], "partial_failure")
                self.assertEqual(self.step(result, "qa", len(verdicts) - 1)["status"], "failed")

    def test_stale_qa_verdict_is_rejected(self):
        world = FakeWorld()
        def stale(req):
            value = world.qa(req)
            value.data["qa_packet_id"] = "wrong-candidate"
            return value
        world.rules["qa"] = stale
        result = world.run()
        self.assertEqual(world.commits, [])
        self.assertEqual(self.step(result, "qa")["status"], "failed")

    def test_manual_qa_context_cannot_enter_automatic_flow(self):
        world = FakeWorld()
        def manual(req):
            outcome = world.preview(req)
            outcome.data.qa_context.update(mode="manual", instruction="PRIVATE-MANUAL")
            return outcome
        world.rules["preview"] = manual
        result = world.run()
        self.assertNotIn("qa", self.names(world))
        self.assertEqual(world.commits, [])
        self.assertEqual(self.step(result, "preview")["codes"], ["orchestration_invalid_result"])

    def test_collection_exception_uses_explicit_baseline(self):
        world = FakeWorld(runs=0)
        world.rules["collect"] = TimeoutError("PRIVATE")
        result = world.run()
        self.assertEqual(world.outputs["google"].inputs, BASE)
        self.assertTrue(result["fallback"])
        self.assertEqual(result["status"], "partial_failure")

    def test_failed_collection_can_return_validated_previous_input(self):
        world = FakeWorld(runs=0)
        world.rules["collect"] = StepOutcome("failed", BASE, codes=("snapshot_failed",), fallback=True)
        result = world.run(replace(CONTEXT, inputs=None))
        self.assertEqual(result["output"], {"snapshot_run": "r0", "cursor": "r0"})
        self.assertEqual(world.outputs["ntfy"].inputs, BASE)

    def test_partial_collection_passes_usable_snapshot_with_previous_state(self):
        world = FakeWorld(runs=0)
        usable = replace(BASE, snapshot_run="partial-run", snapshot_ref="memory:mixed-courses")
        world.rules["collect"] = StepOutcome("partial_failure", usable,
            counts={"succeeded": 2, "failed": 1}, codes=("course_failed",), fallback=True)
        result = world.run()
        self.assertEqual(world.outputs["ics"].inputs, usable)
        self.assertEqual(self.step(result, "ics")["input"]["snapshot_run"], "partial-run")
        self.assertTrue(self.step(result, "ics")["fallback"])

    def test_warning_fallback_without_failed_stage_is_warning(self):
        world = FakeWorld(runs=0)
        world.rules["collect"] = StepOutcome("warning", BASE, codes=("snapshot_stale",), fallback=True)
        result = world.run()
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["stage_counts"]["failed"], 0)

    def test_no_usable_input_skips_dependencies_and_is_failed(self):
        world = FakeWorld()
        world.rules["collect"] = StepOutcome("failed", codes=("snapshot_missing",))
        result = world.run(replace(CONTEXT, inputs=None))
        self.assertEqual(self.names(world), ["collect"])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["output"])
        self.assertFalse(result["fallback"])
        self.assertEqual(result["stage_counts"]["skipped"], 5)
        self.assertTrue(all(item["codes"] == ["no_usable_input"] for item in result["steps"][1:]))

    def test_collection_success_without_input_is_invalid(self):
        world = FakeWorld()
        world.rules["collect"] = StepOutcome()
        result = world.run(replace(CONTEXT, inputs=None))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.step(result, "collect")["codes"], ["orchestration_invalid_result"])
        self.assertEqual(self.names(world), ["collect"])

    def test_failed_collection_cannot_disguise_new_input_as_success(self):
        world = FakeWorld(runs=0)
        world.rules["collect"] = StepOutcome("failed", replace(BASE, snapshot_run="r9"),
                                              codes=("snapshot_failed",))
        result = world.run()
        self.assertEqual(world.outputs["google"].inputs, BASE)
        self.assertEqual(self.step(result, "collect")["codes"], ["orchestration_invalid_result"])

    def test_collection_cannot_advance_semantic_cursor(self):
        world = FakeWorld(runs=0)
        world.rules["collect"] = StepOutcome(data=replace(BASE, cursor="r9"))
        result = world.run()
        self.assertEqual(result["output"]["cursor"], "r0")
        self.assertEqual(self.step(result, "collect")["codes"], ["orchestration_invalid_result"])

    def test_ntfy_failure_does_not_stop_ics_or_google(self):
        world = FakeWorld()
        world.rules["ntfy"] = StepOutcome("failed", codes=("ntfy_unavailable",))
        result = world.run()
        self.assertEqual(self.names(world)[-3:], ["ntfy", "ics", "google"])
        self.assertEqual(self.step(result, "ics")["status"], "success")
        self.assertEqual(result["pairs_committed"], 1)

    def test_ics_failure_does_not_stop_google_or_supply_file(self):
        world = FakeWorld()
        world.rules["ics"] = OSError("SECRET-file")
        result = world.run()
        self.assertEqual(self.step(result, "google")["status"], "success")
        self.assertEqual(world.outputs["google"].projection["cursor"], "r1")
        self.assertNotIn("ics", world.outputs["google"].projection)

    def test_google_partial_failure_preserves_success_counts(self):
        world = FakeWorld()
        world.rules["google"] = StepOutcome("partial_failure", counts={"succeeded": 3, "failed": 1},
                                            codes=("google_rate_limited",))
        result = world.run()
        self.assertEqual(self.step(result, "google")["counts"], {"succeeded": 3, "failed": 1})
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(world.state["accepted"], ["r1"])

    def test_project_failure_only_skips_ics(self):
        world = FakeWorld()
        world.rules["project"] = RuntimeError("PRIVATE")
        result = world.run()
        self.assertNotIn("ics", self.names(world))
        self.assertEqual(self.names(world)[-2:], ["ntfy", "google"])
        self.assertIsNone(world.outputs["google"].projection)
        self.assertEqual(self.step(result, "ics")["codes"], ["projection_unavailable"])

    def test_empty_calendar_is_usable_and_outputs_are_attempted(self):
        world = FakeWorld(runs=0)
        world.rules["project"] = StepOutcome(data={})
        result = world.run()
        self.assertEqual(world.outputs["ics"].projection, {})
        self.assertEqual(result["status"], "success")

    def test_malformed_projection_does_not_reach_ics(self):
        world = FakeWorld()
        world.rules["project"] = StepOutcome(data=42)
        result = world.run()
        self.assertEqual(self.step(result, "project")["codes"], ["orchestration_invalid_result"])
        self.assertNotIn("ics", self.names(world))
        self.assertIsNone(world.outputs["google"].projection)

    def test_only_read_successes_do_not_mask_total_work_failure(self):
        world = FakeWorld(runs=0)
        for stage in ("collect", "ntfy", "ics", "google"):
            world.rules[stage] = StepOutcome("failed", codes=("fixture_failed",))
        self.assertEqual(world.run()["status"], "failed")

    def test_partial_success_is_work_even_if_other_work_failed(self):
        world = FakeWorld(runs=0)
        for stage in ("collect", "ntfy", "ics"):
            world.rules[stage] = StepOutcome("failed", codes=("fixture_failed",))
        world.rules["google"] = StepOutcome("partial_failure", counts={"succeeded": 1, "failed": 1},
                                            codes=("google_failed",))
        self.assertEqual(world.run()["status"], "partial_failure")

    def test_malformed_adapter_envelopes_fail_closed(self):
        for value in (None, {}, StepOutcome("unknown"), StepOutcome("skipped"),
                      StepOutcome(counts={"failed": -1}), StepOutcome(counts={"succeeded": True}),
                      StepOutcome(codes=("https://secret.invalid",)), StepOutcome("failed"),
                      StepOutcome("partial_failure", codes=("failed",))):
            with self.subTest(value=value):
                world = FakeWorld()
                world.rules["interpret"] = lambda req, value=value: value
                result = world.run()
                self.assertEqual(world.commits, [])
                self.assertEqual(self.step(result, "interpret")["codes"], ["orchestration_invalid_result"])
                self.assertIn("google", self.names(world))

    def test_malformed_stage_payloads_do_not_commit(self):
        for stage, payload in (("prepare", {}), ("interpret", []), ("preview", {}),
                               ("qa", {}), ("commit", BASE)):
            with self.subTest(stage=stage):
                world = FakeWorld()
                world.rules[stage] = StepOutcome(data=payload)
                result = world.run()
                self.assertEqual(result["pairs_committed"], 0)
                self.assertEqual(result["output"]["cursor"], "r0")
                self.assertEqual(self.step(result, stage)["status"], "failed")

    def test_bad_commit_receipt_does_not_promote_candidate(self):
        world = FakeWorld()
        world.rules["commit"] = StepOutcome(data=CommitReceipt(replace(BASE, cursor="wrong")))
        result = world.run()
        self.assertEqual(result["pairs_committed"], 0)
        self.assertEqual(world.outputs["google"].inputs.cursor, "r0")

    def test_partial_commit_is_rejected_as_non_atomic(self):
        world = FakeWorld()
        world.rules["commit"] = StepOutcome("partial_failure", CommitReceipt(BASE),
            counts={"succeeded": 1, "failed": 1}, codes=("state_write_failed",))
        result = world.run()
        self.assertEqual(result["pairs_committed"], 0)
        self.assertEqual(self.step(result, "commit")["codes"], ["orchestration_invalid_result"])

    def test_commit_cannot_switch_snapshot_under_output(self):
        world = FakeWorld()
        world.rules["commit"] = StepOutcome(data=CommitReceipt(replace(BASE, cursor="r1", snapshot_run="r9")))
        result = world.run()
        self.assertEqual(result["output"], {"snapshot_run": "r1", "cursor": "r0"})
        self.assertEqual(self.step(result, "commit")["status"], "failed")

    def test_repeated_pair_and_wrong_predecessor_stop_without_looping(self):
        for pair in (RunPair("r0", "r0", {}, True), RunPair("wrong", "r1", {}, True)):
            with self.subTest(pair=pair):
                world = FakeWorld()
                world.rules["prepare"] = StepOutcome(data=pair)
                result = world.run()
                self.assertEqual(self.names(world).count("prepare"), 1)
                self.assertEqual(result["pairs_committed"], 0)

    def test_report_excludes_payload_paths_and_exception_details(self):
        world = FakeWorld(verdicts=("pending",))
        world.rules["ntfy"] = HylmsError("ntfy_failed", "SECRET-authorization-code")
        serialized = json.dumps(world.run())
        for private in ("PRIVATE", "SECRET", "memory:", "candidate-", "decision_draft", "qa_packet_id"):
            self.assertNotIn(private, serialized)
        self.assertIn("ntfy_failed", serialized)

    def test_adapter_request_mutation_cannot_taint_next_stage(self):
        world = FakeWorld()
        def mutate(req):
            req.pair.packet["fixture"] = "TAINTED"
            return StepOutcome(data={"ok": True})
        world.rules["interpret"] = mutate
        world.run()
        preview_request = next(req for name, req in world.calls if name == "preview")
        self.assertEqual(preview_request.pair.packet["fixture"], "PRIVATE-PACKET")

    def test_output_mutation_cannot_taint_next_output_projection(self):
        world = FakeWorld()
        def mutate(req):
            req.projection["cursor"] = "UNCOMMITTED"
            return StepOutcome()
        world.rules["ics"] = mutate
        world.run()
        self.assertEqual(world.outputs["google"].projection["cursor"], "r1")

    def test_user_cancellation_propagates_without_later_calls(self):
        for stage in ("collect", "qa", "ntfy", "ics"):
            for error in (KeyboardInterrupt(), SystemExit(2)):
                with self.subTest(stage=stage, error=type(error)):
                    world = FakeWorld()
                    world.rules[stage] = error
                    with self.assertRaises(type(error)):
                        world.run()
                    self.assertEqual(self.names(world)[-1], stage)
                    if stage in {"ntfy", "ics"}:
                        self.assertEqual(world.state["accepted"], ["r1"])

    def test_configuration_errors_happen_before_any_adapter(self):
        world = FakeWorld()
        adapters = world.adapters
        del adapters["google"]
        with self.assertRaises(ValueError):
            run_workflow(CONTEXT, adapters, clock=lambda: NOW)
        with self.assertRaises(ValueError):
            run_workflow(replace(CONTEXT, execution_id=""), world.adapters, clock=lambda: NOW)
        with self.assertRaises(ValueError):
            run_workflow(CONTEXT, world.adapters, clock=lambda: NOW.replace(tzinfo=None))
        self.assertEqual(world.calls, [])

    def test_deterministic_fake_execution_has_no_hidden_runtime_state(self):
        self.assertEqual(FakeWorld().run(), FakeWorld().run())
