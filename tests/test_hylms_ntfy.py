from __future__ import annotations

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import hylms.ntfy as ntfy
import hylms_snapshot as hs

from tests.test_hylms_diff import course_payload, event, pending, phase2_state, write_run


NOW = dt.datetime(2026, 9, 4, 12, 0, tzinfo=hs.KST)


def boundary(value=None, state=None):
    return {"value": value, "state": state or ("known" if value else "unbounded")}


def schedule(*, opens=None, due=None):
    return {
        "basis": "provider_effective",
        "default": None,
        "effective": {
            "opens_at": boundary(opens),
            "due_at": boundary(due),
            "late_until_at": boundary(),
            "closes_at": boundary(),
        },
    }


def write_ntfy_run(
    term: Path,
    run_id: str,
    started_at: dt.datetime,
    payload: dict,
    *,
    course_status="updated",
    root_status=True,
):
    run = write_run(
        term, run_id, started_at, payload, course_status=course_status
    )
    status_path = run / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["courses"][0]["last_success_at"] = started_at.isoformat(timespec="seconds")
    status["courses"][0]["warning_codes"] = []
    hs.atomic_write_json(status_path, status)
    if root_status:
        current = copy.deepcopy(status)
        current["run_archive"] = {
            "id": run_id,
            "path": f"runs/{run_id}",
            "status": "committed",
            "error_code": None,
        }
        hs.atomic_write_json(term / "status.json", current)
    return run


def prepare(term: Path, state: dict, *, now=NOW, new_pending_ids=()):
    return hs.prepare_ntfy_delivery(
        term, state, now=now, new_pending_ids=new_pending_ids
    )


def payload_for(delivery: dict, group: str) -> dict:
    return next(item for item in delivery["payloads"] if item["group"] == group)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class NtfyProjectionTest(unittest.TestCase):
    def test_natural_today_tomorrow_done_cancelled_and_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            run_id = "run"
            write_ntfy_run(term, run_id, NOW - dt.timedelta(hours=1), course_payload())
            state = phase2_state(run_id)
            ongoing = event(
                "ongoing", kind="activity", mode="period", all_day=True,
                start="2026-09-01", end="2026-09-05",
            )
            tomorrow = event(
                "tomorrow", kind="class_session", start="2026-09-05T10:00:00+09:00",
                end="2026-09-05T11:00:00+09:00",
            )
            optional = event(
                "optional", kind="application", mode="action_window", all_day=True,
                start="2026-09-04", end="2026-09-06",
            )
            optional["optional"] = True
            ending = event(
                "ending", kind="activity", mode="period", all_day=True,
                start="2026-09-01", end="2026-09-04",
            )
            cancelled = event("cancelled")
            cancelled["status"] = "cancelled"
            done = event("done", kind="action")
            done["action_state"] = "done"
            state["natural_events"] = [ongoing, tomorrow, optional, ending, cancelled, done]

            delivery = prepare(term, state)
            targets = {item["identity"]: item for item in delivery["targets"]}

            self.assertEqual(1, delivery["counts"]["urgent"])
            self.assertEqual(3, delivery["counts"]["general"])
            self.assertIn("tomorrow_due", targets["natural:ongoing"]["reasons"])
            self.assertIn("optional", targets["natural:optional"]["reasons"])
            self.assertIn("today_starts", targets["natural:optional"]["reasons"])
            self.assertIn("today_ends", targets["natural:ending"]["reasons"])
            self.assertEqual("general", targets["natural:tomorrow"]["severity"])
            self.assertNotIn("natural:cancelled", targets)
            self.assertNotIn("natural:done", targets)

    def test_structured_completion_confirmation_and_reason_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            run_id = "run"
            payload = course_payload(extra=True)
            assignment = payload["assignments"][0]
            assignment["schedule"] = schedule(
                opens="2026-09-04T09:00:00+09:00",
                due="2026-09-04T23:59:00+09:00",
            )
            assignment["access"] = {"state": "restricted", "reason": "provider_restricted"}
            assignment["progress"] = {"submitted": False, "graded": False, "workflow_state": None}
            assignment["submission"] = None
            write_ntfy_run(term, run_id, NOW - dt.timedelta(hours=1), payload)
            state = phase2_state(run_id)

            delivery = prepare(term, state)
            target = next(item for item in delivery["targets"] if item["record_id"] == "30")

            self.assertEqual("urgent", target["severity"])
            self.assertEqual(
                ["today_due", "completion_check", "today_open"], target["reasons"]
            )
            self.assertEqual("confirmation_required", target["completion"])

            payload["assignments"][0]["progress"]["submitted"] = True
            payload["assignments"][0]["submission"] = {"workflow_state": "submitted"}
            completed_term = Path(directory) / "26-2-completed"
            write_ntfy_run(completed_term, run_id, NOW - dt.timedelta(hours=1), payload)
            completed_state = phase2_state(run_id)
            completed_state["term"] = completed_term.name
            completed = prepare(completed_term, completed_state)
            self.assertFalse(any(item.get("record_id") == "30" for item in completed["targets"]))
            self.assertTrue(any(
                item["identity"].endswith("assignment:30") and item["reason"] == "completed"
                for item in completed["excluded"]
            ))

    def test_all_structured_kinds_are_projected_and_completed_items_disappear(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            run_id = "run"
            payload = course_payload(
                extra=True, discussion_graded=True,
                discussion_workflow="unsubmitted", own_entry_count=0,
            )
            payload["assignments"][0]["schedule"] = schedule(
                due="2026-09-04T23:59:00+09:00"
            )
            payload["discussions"][0]["schedule"] = schedule(
                due="2026-09-04T23:59:00+09:00"
            )
            payload["weekly_learning"][0]["schedule"] = schedule(
                opens="2026-09-05T09:00:00+09:00"
            )
            payload["quizzes"] = [{
                "id": "40", "title": "시험",
                "description": {"text": "", "links": [], "images": []},
                "source_url": "https://example.test/quizzes/40",
                "source_kind": "classic_quiz",
                "access": {"state": "available", "reason": None},
                "detail_state": "collected",
                "progress": {"workflow_state": "unsubmitted", "attempt": None},
                "submission": {"workflow_state": "unsubmitted"},
                "schedule": schedule(due="2026-09-05T23:59:00+09:00"),
            }]
            write_ntfy_run(term, run_id, NOW - dt.timedelta(hours=1), payload)
            state = phase2_state(run_id)

            delivery = prepare(term, state)
            identities = {item["identity"] for item in delivery["targets"]}

            self.assertTrue({
                "101:assignment:30", "101:quiz:40", "101:discussion:20",
                "101:weekly_learning:10",
            }.issubset(identities))

            completed_payload = copy.deepcopy(payload)
            completed_payload["assignments"][0]["progress"]["submitted"] = True
            completed_payload["assignments"][0]["submission"]["workflow_state"] = "submitted"
            completed_payload["quizzes"][0]["progress"]["workflow_state"] = "complete"
            completed_payload["quizzes"][0]["submission"]["workflow_state"] = "complete"
            completed_payload["discussions"][0] = course_payload(
                discussion_graded=True, discussion_workflow="submitted", own_entry_count=1
            )["discussions"][0]
            completed_payload["discussions"][0]["schedule"] = schedule(
                due="2026-09-04T23:59:00+09:00"
            )
            completed_payload["weekly_learning"][0]["progress"]["completed"] = True
            completed_term = Path(directory) / "26-2-completed-all"
            write_ntfy_run(
                completed_term, run_id, NOW - dt.timedelta(hours=1), completed_payload
            )
            completed_state = phase2_state(run_id)
            completed_state["term"] = completed_term.name
            completed = prepare(completed_term, completed_state)
            completed_identities = {item["identity"] for item in completed["targets"]}
            self.assertTrue({
                "101:assignment:30", "101:quiz:40", "101:discussion:20",
                "101:weekly_learning:10",
            }.isdisjoint(completed_identities))

    def test_linked_weekly_record_deduplicates_to_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            run_id = "run"
            payload = course_payload(extra=True)
            payload["weekly_learning"][0]["kind"] = "assignment"
            payload["weekly_learning"][0]["linked_entity"] = {
                "state": "linked", "kind": "assignment", "id": "30", "assignment_id": None,
            }
            payload["weekly_learning"][0]["schedule"] = schedule(
                due="2026-09-04T23:59:00+09:00"
            )
            write_ntfy_run(term, run_id, NOW - dt.timedelta(hours=1), payload)
            state = phase2_state(run_id)

            delivery = prepare(term, state)
            matching = [item for item in delivery["targets"] if item["record_id"] in {"10", "30"}]

            self.assertEqual(1, len(matching))
            self.assertEqual("101:assignment:30", matching[0]["identity"])
            self.assertEqual(
                ["assignment:30", "weekly_learning:10"], matching[0]["source_record_ids"]
            )

    def test_application_override_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            run_id = "run"
            payload = course_payload(extra=True)
            payload["assignments"][0]["schedule"] = schedule(
                due="2026-09-06T23:59:00+09:00"
            )
            write_ntfy_run(term, run_id, NOW - dt.timedelta(hours=1), payload)
            state = phase2_state(run_id)
            base_patch = {
                "timing": {
                    "mode": "deadline", "all_day": False, "start": None,
                    "end": "2026-09-04T23:59:00+09:00", "end_inclusive": False,
                },
                "attendance_required": None, "attendance_excluded_weeks": [],
                "attendance_status_check_required": None, "minimum_study_time_required": None,
                "delivery_mode": None, "maximum_playback_speed": None,
                "required_for_all": None, "optional": True, "location": None,
                "requirements": [], "consequence": None, "details": {},
            }
            state["announcement_applications"] = [{
                "id": "application", "course_id": "101", "course_name": "자료구조",
                "source_record_ids": ["announcement:1"], "target_record_ids": ["assignment:30"],
                "patch": base_patch, "evidence": [],
            }]

            delivery = prepare(term, state)
            target = next(item for item in delivery["targets"] if item["record_id"] == "30")
            self.assertEqual("urgent", target["severity"])
            self.assertIn("optional", target["reasons"])

            conflict = copy.deepcopy(state["announcement_applications"][0])
            conflict["id"] = "application-2"
            conflict["patch"]["timing"]["end"] = "2026-09-05T23:59:00+09:00"
            state["announcement_applications"].append(conflict)
            conflicted = prepare(term, state)
            self.assertTrue(any(value.startswith("application_conflict:") for value in conflicted["warnings"]))
            self.assertFalse(any(item.get("record_id") == "30" for item in conflicted["targets"]))

    def test_exactly_twenty_four_hours_is_fresh_then_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            run_id = "run"
            payload = course_payload(extra=True)
            payload["assignments"][0]["schedule"] = schedule(due="2026-09-04T23:59:00+09:00")
            write_ntfy_run(term, run_id, NOW - dt.timedelta(hours=24), payload)
            state = phase2_state(run_id)

            fresh = prepare(term, state, now=NOW)
            stale = prepare(term, state, now=NOW + dt.timedelta(seconds=1))

            self.assertFalse(any(item["group"] == "refresh" for item in fresh["payloads"]))
            self.assertEqual("refresh", payload_for(stale, "refresh")["group"])
            self.assertEqual([], stale["targets"])

    def test_fresh_partial_run_uses_previous_usable_record_with_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            before = course_payload(extra=True)
            before["assignments"][0]["schedule"] = schedule(
                due="2026-09-04T23:59:00+09:00"
            )
            after = copy.deepcopy(before)
            after["assignments"][0]["detail_state"] = "unavailable"
            after["assignments"][0]["title"] = "사용하면 안 되는 실패값"
            write_ntfy_run(term, "old", NOW - dt.timedelta(hours=2), before)
            write_ntfy_run(
                term, "new", NOW - dt.timedelta(hours=1), after,
                course_status="updated_with_warnings",
            )
            state = phase2_state("new")

            delivery = prepare(term, state)
            target = next(item for item in delivery["targets"] if item["identity"] == "101:assignment:30")

            self.assertEqual("새 과제", target["title"])
            self.assertIn("snapshot_partial_failure", delivery["warnings"])
            self.assertIn("course_101_updated_with_warnings", delivery["warnings"])

    def test_existing_pending_alone_is_empty_but_new_pending_creates_general(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            run_id = "run"
            write_ntfy_run(term, run_id, NOW - dt.timedelta(hours=1), course_payload())
            state = phase2_state(run_id)
            state["pending"] = [pending("pending-1")]

            empty = prepare(term, state)
            general = prepare(term, state, new_pending_ids=["pending-1"])

            self.assertEqual("empty", payload_for(empty, "empty")["group"])
            self.assertIn("확인대기 0+(1)", payload_for(empty, "empty")["payload"]["message"])
            self.assertEqual("general", payload_for(general, "general")["group"])
            self.assertIn("[새 확인 대기]", payload_for(general, "general")["payload"]["message"])

    def test_missing_runs_produces_refresh_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            state = phase2_state("missing-run")

            delivery = prepare(term, state)

            self.assertEqual("missing", delivery["snapshot"]["status"])
            self.assertEqual("refresh", payload_for(delivery, "refresh")["group"])
            self.assertEqual([], delivery["targets"])


class NtfyFreshnessRegressionTest(unittest.TestCase):
    @staticmethod
    def empty_course():
        data = course_payload()
        for key in ("assignments", "quizzes", "discussions", "weekly_learning"):
            data[key] = []
        return data

    def test_fresh_structured_deadline_survives_stale_natural_state(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            data = course_payload(extra=True)
            data["assignments"][0]["schedule"] = schedule(due=(NOW + dt.timedelta(hours=3)).isoformat())
            write_ntfy_run(term, "old", NOW - dt.timedelta(days=2), data)
            write_ntfy_run(term, "fresh", NOW, data)
            state = phase2_state("old")
            state["natural_events"] = [event("stale-natural", start=NOW.isoformat(), end=(NOW + dt.timedelta(hours=1)).isoformat())]
            before = copy.deepcopy(state)
            result = prepare(term, state)
            self.assertEqual(["101:assignment:30"], [item["identity"] for item in result["targets"]])
            self.assertEqual(1, result["counts"]["urgent"])
            self.assertEqual("fresh_with_warnings", result["snapshot"]["status"])
            self.assertIn("state_stale", result["warnings"])
            self.assertIn("state_cursor_behind", result["warnings"])
            self.assertNotIn("refresh", [item["group"] for item in result["payloads"]])
            self.assertEqual(before, state)

    def test_fresh_natural_event_without_structured_records(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            write_ntfy_run(term, "fresh", NOW, self.empty_course())
            state = phase2_state("fresh")
            state["natural_events"] = [event("today", start=NOW.isoformat(), end=(NOW + dt.timedelta(hours=1)).isoformat())]
            result = prepare(term, state)
            self.assertEqual(["natural:today"], [item["identity"] for item in result["targets"]])
            self.assertEqual("fresh", result["snapshot"]["status"])
            self.assertEqual(1, result["counts"]["urgent"])

    def test_fresh_natural_event_survives_stale_structured_records(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            data = course_payload(extra=True)
            data["assignments"][0]["schedule"] = schedule(due=(NOW + dt.timedelta(hours=3)).isoformat())
            write_ntfy_run(term, "old", NOW - dt.timedelta(days=2), data)
            write_ntfy_run(term, "fresh", NOW, data, course_status="kept")
            state = phase2_state("fresh")
            state["natural_events"] = [event("today", start=NOW.isoformat(), end=(NOW + dt.timedelta(hours=1)).isoformat())]
            result = prepare(term, state)
            self.assertEqual(["natural:today"], [item["identity"] for item in result["targets"]])
            self.assertIn("stale_courses:101", result["warnings"])
            self.assertEqual("fresh_with_warnings", result["snapshot"]["status"])

    def test_successful_empty_and_no_courses_remain_fresh_for_twenty_four_hours(self):
        for no_courses in (False, True):
            with self.subTest(no_courses=no_courses), tempfile.TemporaryDirectory() as directory:
                term = Path(directory) / "26-2"
                run = write_ntfy_run(term, "empty", NOW - dt.timedelta(hours=24), self.empty_course())
                if no_courses:
                    status = json.loads((run / "status.json").read_text(encoding="utf-8"))
                    status.update(overall_status="no_courses", courses=[])
                    hs.atomic_write_json(run / "status.json", status)
                    hs.atomic_write_json(term / "status.json", status)
                state = phase2_state("empty")
                fresh = prepare(term, state)
                self.assertEqual("fresh", fresh["snapshot"]["status"])
                self.assertEqual(["empty"], [item["group"] for item in fresh["payloads"]])
                stale = prepare(term, state, now=NOW + dt.timedelta(seconds=1))
                self.assertEqual("stale", stale["snapshot"]["status"])
                self.assertEqual(["refresh"], [item["group"] for item in stale["payloads"]])

    def test_partial_empty_does_not_prove_no_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            write_ntfy_run(term, "partial", NOW, self.empty_course(), course_status="updated_with_warnings")
            result = prepare(term, phase2_state("partial"))
            self.assertEqual(["refresh"], [item["group"] for item in result["payloads"]])
            self.assertIn("snapshot_partial_failure", result["warnings"])

    def test_missing_course_inventory_is_not_a_successful_empty_snapshot(self):
        for inventory in (None, {}):
            with self.subTest(inventory=inventory), tempfile.TemporaryDirectory() as directory:
                term = Path(directory) / "26-2"
                run = write_ntfy_run(term, "invalid", NOW, self.empty_course())
                status = json.loads((run / "status.json").read_text(encoding="utf-8"))
                status["courses"] = inventory
                hs.atomic_write_json(run / "status.json", status)
                with self.assertRaises(hs.HylmsError):
                    prepare(term, phase2_state("invalid"))

    def test_failed_attempt_does_not_refresh_the_age_of_a_confirmed_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            write_ntfy_run(term, "empty", NOW - dt.timedelta(hours=24), self.empty_course())
            write_ntfy_run(term, "failed", NOW, self.empty_course(), course_status="kept")
            state = phase2_state("failed")
            fresh = prepare(term, state)
            self.assertEqual(["empty"], [item["group"] for item in fresh["payloads"]])
            self.assertIn("snapshot_partial_failure", fresh["warnings"])
            stale = prepare(term, state, now=NOW + dt.timedelta(seconds=1))
            self.assertEqual(["refresh"], [item["group"] for item in stale["payloads"]])

    def test_fresh_pending_survives_partial_empty_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            write_ntfy_run(term, "partial", NOW, self.empty_course(), course_status="updated_with_warnings")
            state = phase2_state("partial")
            state["pending"] = [pending("new")]
            result = prepare(term, state, new_pending_ids=["new"])
            self.assertEqual(["general"], [item["group"] for item in result["payloads"]])
            self.assertEqual(1, result["counts"]["new_pending"])


class NtfyPayloadAndTransportTest(unittest.TestCase):
    def test_utf8_messages_split_without_losing_long_item(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            run_id = "run"
            write_ntfy_run(term, run_id, NOW - dt.timedelta(hours=1), course_payload())
            state = phase2_state(run_id)
            long_title = "긴" * 5000
            long_event = event(
                "long", kind="class_session",
                start="2026-09-04T13:00:00+09:00",
                end="2026-09-04T14:00:00+09:00",
            )
            long_event["title"] = long_title
            state["natural_events"] = [long_event]

            delivery = prepare(term, state)
            urgent = [item for item in delivery["payloads"] if item["group"] == "urgent"]
            joined = "".join(item["payload"]["message"] for item in urgent)

            self.assertGreater(len(urgent), 1)
            self.assertTrue(all(
                len(item["payload"]["message"].encode("utf-8")) <= 4096 for item in urgent
            ))
            self.assertEqual(5000, joined.count("긴"))
            self.assertEqual({"natural:long"}, {
                identity for item in urgent for identity in item["item_ids"]
            })
            self.assertTrue(all("/" in item["payload"]["title"] for item in urgent))

    def test_visual_parts_hold_at_most_three_items_and_hide_normal_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            run_id = "run"
            write_ntfy_run(term, run_id, NOW - dt.timedelta(hours=1), course_payload())
            state = phase2_state(run_id)
            state["natural_events"] = [
                event(
                    f"session-{index}", kind="class_session",
                    start=f"2026-09-04T{13 + index:02d}:00:00+09:00",
                    end=f"2026-09-04T{13 + index:02d}:30:00+09:00",
                )
                for index in range(7)
            ]

            delivery = prepare(term, state)
            urgent = [item for item in delivery["payloads"] if item["group"] == "urgent"]

            self.assertEqual(3, len(urgent))
            self.assertTrue(all(len(item["item_ids"]) <= 3 for item in urgent))
            self.assertTrue(all("실행:" not in item["payload"]["message"] for item in urgent))
            self.assertTrue(all("Snapshot:" not in item["payload"]["message"] for item in urgent))
            self.assertTrue(all(item["payload"]["title"].startswith("HY-LMS 긴급 · 7건") for item in urgent))

    def test_json_post_success_failure_and_no_retry(self):
        delivery = hs.acceptance_delivery("urgent", now=NOW)
        transport = FakeTransport([
            (200, {"content-type": "application/json"}, b'{"id":"abc","time":2}'),
        ])

        sent = hs.send_ntfy_delivery(delivery, transport=transport, timeout=10)

        self.assertEqual(1, sent["counts"]["sent"])
        self.assertEqual(0, sent["counts"]["failed"])
        self.assertEqual("abc", sent["deliveries"][0]["response_id"])
        self.assertEqual(1, len(transport.calls))
        method, url, headers, body, timeout = transport.calls[0]
        self.assertEqual(("POST", "https://ntfy.sh/", 10.0), (method, url, timeout))
        self.assertEqual("application/json", headers["Content-Type"])
        self.assertEqual(hs.NTFY_TOPIC, json.loads(body)["topic"])
        self.assertEqual(
            delivery["payloads"][0]["payload"], json.loads(body)
        )
        self.assertNotEqual(hs.NTFY_SEPARATOR, json.loads(body)["message"])

        for status in (403, 503):
            with self.subTest(status=status):
                failed_transport = FakeTransport([
                    (status, {}, b"unavailable"),
                ])
                failed = hs.send_ntfy_delivery(delivery, transport=failed_transport)
                self.assertEqual(1, failed["counts"]["failed"])
                self.assertEqual(f"ntfy_http_{status}", failed["deliveries"][0]["error_code"])
                self.assertEqual(1, len(failed_transport.calls))

    def test_transport_error_and_cli_exit_codes(self):
        transport = FakeTransport([
            hs.HylmsError("ntfy_transport_error", "offline"),
        ])
        output = []
        code = ntfy.main(
            ["acceptance", "--sample", "empty"], out=output.append,
            clock=lambda: NOW, transport=transport,
        )

        self.assertEqual(2, code)
        self.assertEqual(1, len(transport.calls))
        self.assertEqual(1, json.loads(output[0])["counts"]["failed"])

        invalid_output = []
        invalid_code = ntfy.main(
            [
                "plan", "--term-dir", "missing-term", "--state", "missing-state.json",
            ],
            out=invalid_output.append,
            clock=lambda: NOW,
        )
        self.assertEqual(1, invalid_code)
        self.assertIn("ntfy_state_invalid", invalid_output[0])

    def test_send_failure_changes_no_state_snapshot_or_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            term = root / "26-2"
            run_id = "run"
            write_ntfy_run(term, run_id, NOW - dt.timedelta(hours=1), course_payload())
            state = phase2_state(run_id)
            state_path = root / "state.json"
            hs.atomic_write_json(state_path, state)
            delivery = prepare(term, state)
            paths = sorted(path for path in root.rglob("*") if path.is_file())
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

            result = hs.send_ntfy_delivery(
                delivery, transport=FakeTransport([
                    (500, {}, b"failed"),
                ])
            )

            after_paths = sorted(path for path in root.rglob("*") if path.is_file())
            after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in after_paths}
            self.assertEqual(1, result["counts"]["failed"])
            self.assertEqual(before, after)

    def test_acceptance_samples_have_fixed_styles(self):
        expectations = {
            "urgent": ("HY-LMS 긴급", 5),
            "general": ("HY-LMS 알림", 3),
            "empty": ("HY-LMS 상태", 3),
        }
        for sample, (title, priority) in expectations.items():
            with self.subTest(sample=sample):
                delivery = hs.acceptance_delivery(sample, now=NOW)
                self.assertEqual([sample], [item["group"] for item in delivery["payloads"]])
                self.assertEqual(0, delivery["counts"]["separator"])
                payload = payload_for(delivery, sample)["payload"]
                self.assertEqual(title, payload["title"])
                self.assertEqual(priority, payload["priority"])
                self.assertEqual(hs.NTFY_TOPIC, payload["topic"])

    def test_legacy_separator_plan_is_rejected_before_any_send(self):
        delivery = hs.acceptance_delivery("empty", now=NOW)
        delivery["payloads"].insert(0, {"group": "separator", "part": 1, "parts": 1,
            "item_ids": [], "payload": {"topic": hs.NTFY_TOPIC, "message": hs.NTFY_SEPARATOR}})
        delivery["counts"].update(separator=1, payloads=2)
        transport = FakeTransport([])
        with self.assertRaises(hs.HylmsError):
            hs.send_ntfy_delivery(delivery, transport=transport)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
