from __future__ import annotations

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hylms.ics as ics
import hylms_snapshot as hs
from tests.test_hylms_diff import announcement_application, course_payload, event, pending, phase2_state
from tests.test_hylms_ntfy import NOW, boundary, schedule, write_ntfy_run


def payload():
    value = course_payload(extra=True)
    value["weekly_learning"][0]["kind"] = "video"
    value["assignments"][0]["schedule"] = schedule(due="2026-09-10T23:59:30+09:00")
    return value


def unfolded(content: bytes) -> list[str]:
    # Independent reader for wire-level assertions; no renderer helpers used.
    return content.replace(b"\r\n ", b"").replace(b"\r\n\t", b"").decode("utf-8").split("\r\n")


def read_events(content: bytes) -> list[dict[str, str]]:
    events = []
    current = None
    for line in unfolded(content):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            events.append(current)
            current = None
        elif current is not None:
            key, value = line.split(":", 1)
            current[key] = value
    return events


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.term = Path(self.temp.name) / "26-2"
        self.data = payload()
        self.run = write_ntfy_run(self.term, "first", NOW, self.data)
        self.state = phase2_state("first")

    def replace_payload(self, value):
        hs.atomic_write_json(self.run / "course__c101.json", value)

    def prepare(self, **kwargs):
        return ics.prepare_calendar(self.term, self.state, now=kwargs.get("now", NOW))

    def natural_events(self, plan):
        return [item for item in plan["events"] if item["identity"].startswith("natural:")]

    def assignment(self, plan):
        return next(item for item in plan["events"] if item["identity"] == "101:assignment:30")

    def test_effective_deadline_and_all_boundaries(self):
        assignment = self.data["assignments"][0]
        assignment["schedule"]["default"] = schedule(due="2026-09-09T23:59:30+09:00")["effective"]
        assignment["schedule"]["basis"] = "user_override"
        effective = assignment["schedule"]["effective"]
        effective["opens_at"] = boundary("2026-09-01T10:00:00+09:00")
        effective["closes_at"] = boundary("2026-09-12T23:59:30+09:00")
        effective["late_until_at"] = boundary("2026-09-12T23:59:30+09:00", "same_as_closes_at")
        self.replace_payload(self.data)
        item = self.assignment(self.prepare())
        self.assertEqual("[마감] 자료구조 · 새 과제", item["title"])
        self.assertEqual("2026-09-10T23:59:30+09:00", item["start"])
        self.assertEqual("2026-09-11T00:00:30+09:00", item["end"])
        self.assertIn("공개: 2026-09-01 10:00:00", item["description"])
        self.assertIn("지각 마감: 2026-09-12 23:59:30", item["description"])
        self.assertIn("최종 종료: 2026-09-12 23:59:30", item["description"])
        self.assertIn("https://example.test/assignments/30", item["description"])

    def test_quiz_discussion_and_video_categories(self):
        self.data["quizzes"] = [{"id": "60", "title": "시험", "schedule": schedule(due="2026-10-01T15:00:00+09:00")}]
        self.data["discussions"][0]["schedule"] = schedule(due="2026-10-02T15:00:00+09:00")
        self.replace_payload(self.data)
        plan = self.prepare()
        self.assertEqual({"101:assignment:30", "101:quiz:60", "101:discussion:20", "101:weekly_learning:10"}, {e["identity"] for e in plan["events"]})

    def test_completed_past_and_stale_are_retained(self):
        assignment = self.data["assignments"][0]
        assignment["submission"]["workflow_state"] = "graded"
        assignment["progress"]["submitted"] = True
        self.replace_payload(self.data)
        natural = event("past")
        self.state["natural_events"] = [natural]
        result = self.prepare(now=NOW + dt.timedelta(days=100))
        self.assertIn("완료 상태: 완료", self.assignment(result)["description"])
        self.assertEqual(1, len(self.natural_events(result)))
        self.assertIn("state_stale", result["warnings"])
        self.assertIn("course_stale:101", result["warnings"])

    def test_wrapper_merges_once_with_linked_identity_and_priority(self):
        wrapper = self.data["weekly_learning"][0]
        wrapper["kind"] = "assignment"
        wrapper["linked_entity"] = {"state": "linked", "kind": "assignment", "id": "30"}
        wrapper["schedule"] = schedule(due="2026-09-11T13:00:00+09:00")
        self.data["assignments"][0]["schedule"]["effective"]["late_until_at"] = boundary("2026-09-13T13:00:00+09:00")
        self.replace_payload(self.data)
        result = self.prepare()
        self.assertEqual(1, len(result["events"]))
        item = self.assignment(result)
        self.assertEqual("2026-09-11T13:00:00+09:00", item["start"])
        self.assertEqual(["assignment:30", "weekly_learning:10"], item["source_record_ids"])
        self.assertEqual(2, len(item["source_urls"]))
        self.assertIn("지각 마감: 2026-09-13 13:00:00", item["description"])

    def test_missing_linked_record_preserves_canonical_uid_when_it_returns(self):
        wrapper = self.data["weekly_learning"][0]
        wrapper["kind"] = "assignment"
        wrapper["linked_entity"] = {"state": "linked", "kind": "assignment", "id": "30"}
        assignment = self.data["assignments"].pop()
        self.replace_payload(self.data)
        before = self.prepare()
        self.assertEqual(1, len(before["events"]))
        self.assertIn("linked_record_missing:101:weekly_learning:10:101:assignment:30", before["warnings"])
        self.data["assignments"].append(assignment)
        self.replace_payload(self.data)
        after = self.prepare()
        self.assertEqual(self.assignment(before)["uid"], self.assignment(after)["uid"])
        self.assertEqual(1, len(after["events"]))

    def test_partial_wrapper_keeps_missing_canonical_boundaries(self):
        opens = "2026-09-01T10:00:00+09:00"
        due = "2026-09-04T15:00:00+09:00"
        for missing in ("opens_at", "due_at"):
            for missing_state in ("provider_omitted", "unknown", "restricted", "absent"):
                with self.subTest(boundary=missing, state=missing_state):
                    data = payload()
                    data["assignments"][0]["schedule"] = schedule(opens=opens, due=due)
                    wrapper = data["weekly_learning"][0]
                    wrapper.update(
                        kind="assignment", linked_entity={"state": "linked", "kind": "assignment", "id": "30"},
                        schedule=schedule(opens=opens, due=due),
                    )
                    if missing_state == "absent":
                        del wrapper["schedule"]["effective"][missing]
                    else:
                        wrapper["schedule"]["effective"][missing] = boundary(None, missing_state)
                    self.replace_payload(data)
                    item = self.assignment(self.prepare())
                    self.assertEqual(due, item["start"])
                    self.assertIn("공개: 2026-09-01 10:00:00", item["description"])
                    delivery = hs.prepare_ntfy_delivery(self.term, self.state, now=NOW)
                    target = next(item for item in delivery["targets"] if item["identity"] == "101:assignment:30")
                    self.assertIn("today_due", target["reasons"])

    def test_patch_overrides_deadline_and_null_keeps_baseline(self):
        application = announcement_application()
        application["target_record_ids"] = ["assignment:30"]
        self.state["announcement_applications"] = [application]
        baseline = self.assignment(self.prepare())
        application["patch"]["timing"] = event("patch", start="2026-09-01T10:00:00+09:00", end="2026-10-02T10:00:00+09:00")["timing"]
        changed = self.assignment(self.prepare())
        self.assertEqual("2026-10-02T10:00:00+09:00", changed["start"])
        self.assertEqual(baseline["uid"], changed["uid"])

    def test_conflicting_patches_keep_baseline(self):
        first = announcement_application("one")
        first["target_record_ids"] = ["assignment:30"]
        first["patch"]["timing"] = event("one")["timing"]
        second = copy.deepcopy(first)
        second["id"] = "two"
        second["patch"]["timing"]["end"] = "2026-09-01T12:00:00+09:00"
        self.state["announcement_applications"] = [first, second]
        result = self.prepare()
        self.assertEqual("2026-09-10T23:59:30+09:00", self.assignment(result)["start"])
        self.assertIn("application_conflict:101:assignment:30:timing", result["warnings"])

    def test_missing_unknown_and_date_only_deadline_not_inferred(self):
        for state in ("unbounded", "restricted", "unknown", "provider_omitted"):
            with self.subTest(state=state):
                self.data["assignments"][0]["schedule"]["effective"]["due_at"] = boundary(None, state)
                self.replace_payload(self.data)
                result = self.prepare()
                self.assertIn({"identity": "101:assignment:30", "reason": "deadline_missing"}, result["excluded"])
        self.replace_payload(payload())
        application = announcement_application()
        application["target_record_ids"] = ["assignment:30"]
        application["patch"]["timing"] = event("date", mode="deadline", kind="action", all_day=True, start=None, end="2026-10-01")["timing"]
        self.state["announcement_applications"] = [application]
        self.assertIn({"identity": "101:assignment:30", "reason": "deadline_time_unspecified"}, self.prepare()["excluded"])

    def test_invalid_known_time_fails_instead_of_empty_output(self):
        self.data["assignments"][0]["schedule"]["effective"]["due_at"] = boundary("2026-09-10Tbad")
        self.replace_payload(self.data)
        with self.assertRaises(hs.HylmsError):
            self.prepare()

    def test_invalid_known_boundaries_preserve_existing_ics(self):
        self.assertEqual("written", ics.write_ics(self.prepare())["status"])
        path = self.term / "HY-LMS.ics"
        original = path.read_bytes()
        state_path = Path(self.temp.name) / "state.json"
        hs.atomic_write_json(state_path, self.state)
        args = ["build", "--term-dir", str(self.term), "--state", str(state_path)]
        for name, known_state in (
            ("opens_at", "known"), ("due_at", "known"), ("late_until_at", "known"),
            ("late_until_at", "same_as_closes_at"), ("closes_at", "known"),
        ):
            for value in (123, None, False, {}, [], "", "2026-09-10", "2026-09-10T12:00:00", "2026-09-31T12:00:00+09:00"):
                with self.subTest(boundary=name, state=known_state, value=value):
                    path.write_bytes(original)
                    data = payload()
                    data["assignments"][0]["schedule"]["effective"][name] = {"state": known_state, "value": value}
                    self.replace_payload(data)
                    output = []
                    self.assertEqual(1, ics.main(args, out=output.append, clock=lambda: NOW))
                    result = json.loads(output[0])
                    self.assertEqual("failed", result["status"])
                    self.assertTrue(result["previous_preserved"])
                    self.assertEqual(original, path.read_bytes())
        self.assertEqual([], list(self.term.glob(".HY-LMS.ics.*.tmp")))

    def test_nonvideo_page_with_schedule_not_exported(self):
        self.data["weekly_learning"][0]["kind"] = "pdf"
        self.replace_payload(self.data)
        self.assertIn({"identity": "101:weekly_learning:10", "reason": "not_calendar_content"}, self.prepare()["excluded"])

    def test_natural_deadline_with_start_preserves_full_period(self):
        self.state["natural_events"] = [
            event("report", kind="submission", mode="deadline", all_day=True, start="2026-11-02", end="2026-11-27"),
            event("submission", kind="submission", mode="deadline", start="2026-11-03T00:00:00+09:00", end="2026-11-18T23:59:00+09:00"),
        ]
        natural = self.natural_events(self.prepare())
        self.assertEqual(("2026-11-02", "2026-11-28"), (natural[0]["start"], natural[0]["end"]))
        self.assertEqual(("2026-11-03T00:00:00+09:00", "2026-11-18T23:59:00+09:00"), (natural[1]["start"], natural[1]["end"]))

    def test_natural_period_window_and_point_deadlines(self):
        self.state["natural_events"] = [
            event("period", kind="activity", mode="period", all_day=True, start="2026-09-01", end="2026-11-27"),
            event("window", kind="application", mode="action_window", all_day=True, start="2026-09-01", end="2026-09-07"),
            event("date", kind="action", mode="deadline", all_day=True, start=None, end="2026-12-31"),
            event("time", kind="action", mode="deadline", start=None, end="2026-12-31T23:59:30+09:00"),
        ]
        items = {item["identity"]: item for item in self.natural_events(self.prepare())}
        self.assertEqual("2026-11-28", items["natural:period"]["end"])
        self.assertEqual("2026-09-08", items["natural:window"]["end"])
        self.assertEqual("2027-01-01", items["natural:date"]["end"])
        self.assertEqual("2027-01-01T00:00:30+09:00", items["natural:time"]["end"])

    def test_cancelled_excluded_but_active_cancellation_notice_and_done_retained(self):
        cancelled = event("cancelled")
        cancelled["status"] = "cancelled"
        notice = event("notice")
        notice["title"] = "[휴강] 수업"
        done = event("done", kind="action", mode="deadline", start=None)
        done["action_state"] = "done"
        self.state["natural_events"] = [cancelled, notice, done]
        result = self.prepare()
        self.assertEqual({"natural:notice", "natural:done"}, {item["identity"] for item in self.natural_events(result)})

    def test_removed_source_and_conflict_do_not_change_confirmed_event(self):
        self.state["natural_events"] = [event("confirmed")]
        conflict = pending()
        conflict["context"] = {"kind": "user_confirmed_conflict", "event_id": "confirmed", "field": "timing.start", "candidates": [{"value": "2026-10-01T10:00:00+09:00"}]}
        self.state["pending"] = [conflict]
        self.data["announcements"] = []
        write_ntfy_run(self.term, "second", NOW + dt.timedelta(minutes=1), self.data)
        self.state["last_processed_run_id"] = "second"
        result = self.prepare()
        natural = self.natural_events(result)
        self.assertEqual(1, len(natural))
        self.assertEqual("2026-09-01T10:00:00+09:00", natural[0]["start"])
        self.assertIn("https://example.test/announcements/1", natural[0]["source_urls"])

    def test_natural_and_structured_shared_source_remain_distinct(self):
        natural = event("class")
        natural["source_record_ids"] = ["weekly_learning:10"]
        self.state["natural_events"] = [natural]
        ids = {item["identity"] for item in self.prepare()["events"]}
        self.assertIn("natural:class", ids)
        self.assertIn("101:weekly_learning:10", ids)

    def test_partial_failure_and_cursor_lag_use_last_usable_record(self):
        self.data["assignments"][0]["detail_state"] = "unavailable"
        self.data["assignments"][0]["schedule"] = schedule(due="2026-12-01T01:00:00+09:00")
        write_ntfy_run(self.term, "partial", NOW + dt.timedelta(minutes=1), self.data, course_status="updated_with_warnings")
        result = self.prepare()
        self.assertEqual("2026-09-10T23:59:30+09:00", self.assignment(result)["start"])
        self.assertIn("state_cursor_behind", result["warnings"])
        self.assertIn("course_101_updated_with_warnings", result["warnings"])

    def test_kept_course_uses_prior_success(self):
        write_ntfy_run(self.term, "kept", NOW + dt.timedelta(minutes=1), self.data, course_status="kept")
        self.assertEqual("2026-09-10T23:59:30+09:00", self.assignment(self.prepare())["start"])

    def test_normal_empty_calendar(self):
        for key in ("assignments", "quizzes", "discussions", "weekly_learning"):
            self.data[key] = []
        self.replace_payload(self.data)
        plan = self.prepare()
        self.assertEqual([], read_events(ics.render_ics(plan)))
        self.assertEqual("written", ics.write_ics(plan)["status"])

    def test_no_courses_is_empty_but_all_failed_without_success_is_error(self):
        status_path = self.run / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["courses"] = []
        status["overall_status"] = "no_courses"
        hs.atomic_write_json(status_path, status)
        self.assertEqual([], self.prepare()["events"])
        status["overall_status"] = "failure"
        hs.atomic_write_json(status_path, status)
        with self.assertRaises(hs.HylmsError) as caught:
            self.prepare()
        self.assertEqual("ics_snapshot_missing", caught.exception.code)

    def write_no_courses(self, run_id="empty", *, courses=None):
        status = json.loads((self.run / "status.json").read_text(encoding="utf-8"))
        status.update(
            courses=[] if courses is None else courses, overall_status="no_courses",
            started_at=(NOW + dt.timedelta(minutes=1)).isoformat(),
        )
        hs.atomic_write_json(self.term / "runs" / run_id / "status.json", status)
        hs.atomic_write_json(self.term / "status.json", status)
        self.state["last_processed_run_id"] = run_id

    def test_no_courses_after_success_clears_structured_events(self):
        self.assertEqual(2, self.prepare()["counts"]["events"])
        self.write_no_courses()
        result = self.prepare()
        self.assertEqual([], result["events"])
        self.assertEqual([], result["warnings"])
        self.assertEqual("written", ics.write_ics(result)["status"])
        self.assertEqual([], read_events((self.term / "HY-LMS.ics").read_bytes()))

    def test_no_courses_preserves_confirmed_natural_event_and_source_url(self):
        self.state["natural_events"] = [event("confirmed")]
        self.write_no_courses()
        result = self.prepare()
        self.assertEqual(["natural:confirmed"], [item["identity"] for item in result["events"]])
        self.assertEqual(["https://example.test/announcements/1"], result["events"][0]["source_urls"])

    def test_no_courses_then_partial_success_does_not_resurrect_old_records(self):
        self.write_no_courses()
        data = payload()
        data["assignments"] = []
        write_ntfy_run(self.term, "third", NOW + dt.timedelta(minutes=2), data, course_status="updated_with_warnings")
        self.state["last_processed_run_id"] = "third"
        result = self.prepare()
        self.assertEqual(["101:weekly_learning:10"], [item["identity"] for item in result["events"]])

    def test_no_courses_with_nonempty_courses_is_invalid(self):
        status = json.loads((self.run / "status.json").read_text(encoding="utf-8"))
        self.write_no_courses(courses=status["courses"])
        with self.assertRaises(hs.HylmsError):
            self.prepare()

    def test_failed_run_with_empty_courses_preserves_prior_records(self):
        self.write_no_courses(run_id="failed")
        path = self.term / "runs" / "failed" / "status.json"
        status = json.loads(path.read_text(encoding="utf-8"))
        status["overall_status"] = "failure"
        hs.atomic_write_json(path, status)
        hs.atomic_write_json(self.term / "status.json", status)
        result = self.prepare()
        self.assertEqual(2, result["counts"]["events"])
        self.assertIn("snapshot_failure", result["warnings"])

    def test_invalid_state_schema_term_cursor_and_missing_snapshot(self):
        for field, value in (("schema_version", 4), ("term", "26-1"), ("last_processed_run_id", "missing")):
            original = self.state[field]
            self.state[field] = value
            with self.subTest(field=field), self.assertRaises(hs.HylmsError):
                self.prepare()
            self.state[field] = original
        with self.assertRaises(hs.HylmsError):
            ics.prepare_calendar(Path(self.temp.name) / "missing" / "26-2", self.state, now=NOW)

    def test_snapshot_path_escape_rejected(self):
        path = self.run / "status.json"
        status = json.loads(path.read_text(encoding="utf-8"))
        status["courses"][0]["path"] = "../outside.json"
        hs.atomic_write_json(path, status)
        with self.assertRaises(hs.HylmsError):
            self.prepare()

    def test_stable_uid_after_rename_date_and_completion_changes(self):
        original = self.assignment(self.prepare())
        self.data["assignments"][0]["title"] = "변경된 제목"
        self.data["assignments"][0]["schedule"] = schedule(due="2026-12-20T19:00:00+09:00")
        self.data["assignments"][0]["progress"]["submitted"] = True
        self.replace_payload(self.data)
        changed = self.assignment(self.prepare())
        self.assertEqual(original["uid"], changed["uid"])
        self.assertNotEqual(original["start"], changed["start"])

    def test_repeated_generation_semantics_equal_except_dtstamp(self):
        before = ics.render_ics(self.prepare())
        after = ics.render_ics(self.prepare(now=NOW + dt.timedelta(minutes=1)))
        self.assertNotEqual(before, after)
        semantic = lambda value: [line for line in unfolded(value) if not line.startswith("DTSTAMP:")]
        self.assertEqual(semantic(before), semantic(after))

    def test_wire_format_unicode_escaping_timezone_and_location(self):
        natural = event("unicode")
        natural["title"] = "한글😀,;\\\n" * 25
        natural["location"] = "서울, 강의실; A\\B\r\n2층"
        self.state["natural_events"] = [natural]
        content = ics.render_ics(self.prepare())
        self.assertTrue(content.endswith(b"END:VCALENDAR\r\n"))
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
        for line in content.split(b"\r\n"):
            self.assertLessEqual(len(line), 75)
            line.decode("utf-8")
        lines = unfolded(content)
        self.assertIn("DTSTART;TZID=Asia/Seoul:20260901T100000", lines)
        self.assertIn("DTSTAMP:20260904T030000Z", lines)
        self.assertIn("TZOFFSETTO:+0900", lines)
        self.assertIn("LOCATION:서울\\, 강의실\\; A\\\\B\\n2층", lines)
        self.assertIn("SUMMARY:" + "한글😀\\,\\;\\\\\\n" * 25, lines)
        self.assertNotIn("BEGIN:VALARM", lines)
        self.assertFalse(any(line.startswith("METHOD:") for line in lines))

    def test_inclusive_dates_and_zero_duration_wire_format(self):
        self.state["natural_events"] = [
            event("single", kind="action", mode="deadline", all_day=True, start=None, end="2026-12-31"),
            event("point", start="2026-09-01T10:00:00+09:00", end="2026-09-01T10:00:00+09:00"),
        ]
        wire = read_events(ics.render_ics(self.prepare()))
        dated = next(item for item in wire if "DTSTART;VALUE=DATE" in item)
        self.assertEqual("20261231", dated["DTSTART;VALUE=DATE"])
        self.assertEqual("20270101", dated["DTEND;VALUE=DATE"])
        point = next(item for item in wire if item.get("DTSTART;TZID=Asia/Seoul") == "20260901T100000")
        self.assertNotIn("DTEND;TZID=Asia/Seoul", point)

    def test_private_fields_and_credentials_not_exported(self):
        self.data["assignments"][0]["source_url"] += "?access_token=SECRET&foo=ok"
        self.replace_payload(self.data)
        natural = event("private")
        natural["details"] = {"note": "PRIVATE-CODE"}
        natural["evidence"] = ["PRIVATE-EVIDENCE"]
        self.state["natural_events"] = [natural]
        result = self.prepare()
        output = json.dumps(result, ensure_ascii=False) + ics.render_ics(result).decode("utf-8")
        for private in ("SECRET", "PRIVATE-CODE", "PRIVATE-EVIDENCE", "must not leak", "private-submission-body"):
            self.assertNotIn(private, output)
        self.assertIn("foo=ok", output)

    def test_atomic_write_failures_preserve_prior_file_and_cleanup(self):
        plan = self.prepare()
        path = self.term / "HY-LMS.ics"
        before = b"previous valid bytes"
        path.write_bytes(before)
        for function in ("mkstemp", "fsync", "replace"):
            target = "hylms.ics.tempfile.mkstemp" if function == "mkstemp" else f"hylms.ics.os.{function}"
            with self.subTest(function=function), mock.patch(target, side_effect=OSError("fixture")):
                result = ics.write_ics(plan)
            self.assertEqual("failed", result["status"])
            self.assertEqual("ics_write_failed", result["error"]["code"])
            self.assertTrue(result["previous_preserved"])
            self.assertEqual(before, path.read_bytes())
            self.assertEqual([], list(self.term.glob(".HY-LMS.ics.*.tmp")))

    def test_write_failure_without_prior_file_does_not_publish(self):
        plan = self.prepare()
        with mock.patch("hylms.ics.os.replace", side_effect=OSError):
            result = ics.write_ics(plan)
        self.assertFalse(result["previous_preserved"])
        self.assertFalse((self.term / "HY-LMS.ics").exists())

    def test_serialization_validation_precedes_any_write(self):
        plan = self.prepare()
        path = self.term / "HY-LMS.ics"
        path.write_bytes(b"previous")
        plan["events"][0]["title"] = "bad\x00title"
        result = ics.write_ics(plan)
        self.assertEqual("ics_input_invalid", result["error"]["code"])
        self.assertEqual(b"previous", path.read_bytes())
        plan = self.prepare()
        plan["events"].append(copy.deepcopy(plan["events"][0]))
        with self.assertRaises(hs.HylmsError):
            ics.render_ics(plan)

    def test_invalid_event_interval_and_naive_time_rejected(self):
        for start, end in (("2026-09-01T10:00:00", "2026-09-01T11:00:00+09:00"), ("2026-09-01T11:00:00+09:00", "2026-09-01T10:00:00+09:00")):
            plan = self.prepare()
            plan["events"][0].update(start=start, end=end)
            with self.subTest(start=start), self.assertRaises(hs.HylmsError):
                ics.render_ics(plan)

    def test_output_cannot_overwrite_json(self):
        plan = self.prepare()
        source = self.run / "course__c101.json"
        before = source.read_bytes()
        result = ics.write_ics(plan, source)
        self.assertEqual("ics_input_invalid", result["error"]["code"])
        self.assertEqual(before, source.read_bytes())

    def test_cli_plan_no_writes_or_network_build_and_exit_codes(self):
        state_path = Path(self.temp.name) / "state.json"
        hs.atomic_write_json(state_path, self.state)
        base = ["--term-dir", str(self.term), "--state", str(state_path)]
        tracked = {p: p.read_bytes() for p in Path(self.temp.name).rglob("*.json")}
        output = []
        with mock.patch("socket.socket", side_effect=AssertionError("network forbidden")):
            self.assertEqual(0, ics.main(["plan", *base], out=output.append, clock=lambda: NOW))
            self.assertFalse((self.term / "HY-LMS.ics").exists())
            self.assertEqual(0, ics.main(["build", *base], out=output.append, clock=lambda: NOW))
        self.assertEqual("written", json.loads(output[-1])["status"])
        self.assertEqual(tracked, {p: p.read_bytes() for p in Path(self.temp.name).rglob("*.json")})
        before = (self.term / "HY-LMS.ics").read_bytes()
        with mock.patch("hylms.ics.os.replace", side_effect=OSError):
            self.assertEqual(2, ics.main(["build", *base], out=output.append, clock=lambda: NOW))
        state_path.write_bytes(b"\xff invalid")
        self.assertEqual(1, ics.main(["build", *base], out=output.append, clock=lambda: NOW))
        self.assertTrue(json.loads(output[-1])["previous_preserved"])
        self.assertEqual(before, (self.term / "HY-LMS.ics").read_bytes())


if __name__ == "__main__":
    unittest.main()
