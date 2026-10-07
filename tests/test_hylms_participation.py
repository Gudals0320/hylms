from __future__ import annotations

import copy
import json
import unittest

import hylms_snapshot as hs


def discussion(
    *,
    discussion_id="20",
    assignment_id="30",
    workflow_state="unsubmitted",
    own_count=0,
    entries_state="collected",
):
    items = [
        {
            "id": str(100 + index),
            "body": {"text": f"private-own-body-{index}"},
            "created_at": f"2026-09-0{index + 1}T10:00:00+09:00",
            "updated_at": f"2026-09-0{index + 1}T11:00:00+09:00",
            "feedback_entries": [
                {
                    "body": {"text": "private-peer-feedback"},
                    "author_display_name": "Private Student",
                }
            ],
        }
        for index in range(own_count)
    ]
    if entries_state != "collected":
        items = []
    submission = None
    if assignment_id is not None and workflow_state is not None:
        submission = {
            "workflow_state": workflow_state,
            "attempt": 1,
            "submitted_at": "2026-09-01T01:00:00Z",
            "graded_at": None,
            "excused": False,
            "late": False,
            "missing": False,
            "seconds_late": 0,
            "body": {"text": "private-submission-body"},
            "comments": [{"comment": "private-comment"}],
            "history": [{"workflow_state": workflow_state}],
        }
    return {
        "id": discussion_id,
        "title": "토론",
        "assignment_id": assignment_id,
        "entries": {"state": entries_state, "reason": None, "items": items},
        "submission": submission,
        "reply_count": 999,
        "unread_count": 999,
        "last_reply_at": "2026-09-09T10:00:00+09:00",
    }


class DiscussionParticipationTest(unittest.TestCase):
    def test_graded_workflow_and_own_reply_matrix(self):
        cases = {
            ("submitted", 0): "conflict",
            ("submitted", 1): "participated",
            ("graded", 0): "conflict",
            ("graded", 1): "participated",
            ("unsubmitted", 0): "not_participated",
            ("unsubmitted", 1): "conflict",
        }
        for (workflow, own_count), expected in cases.items():
            with self.subTest(workflow=workflow, own_count=own_count):
                result = hs.normalize_discussion_participation(
                    "101", discussion(workflow_state=workflow, own_count=own_count)
                )
                self.assertEqual(expected, result["state"])
                self.assertEqual("graded_submission_and_own_reply", result["basis"])
                self.assertEqual(own_count, result["own_entry_count"])

    def test_unknown_graded_submission_is_not_inferred(self):
        for workflow in (None, "pending_review", "complete"):
            with self.subTest(workflow=workflow):
                result = hs.normalize_discussion_participation(
                    "101", discussion(workflow_state=workflow, own_count=1)
                )
                self.assertEqual("unknown", result["state"])

    def test_ungraded_requires_an_exact_approved_rule(self):
        rules = {("101", "20"): "explicit-rule"}
        absent = hs.normalize_discussion_participation(
            "101", discussion(assignment_id=None, own_count=0), rules=rules
        )
        present = hs.normalize_discussion_participation(
            "101", discussion(assignment_id=None, own_count=1), rules=rules
        )
        other_record = hs.normalize_discussion_participation(
            "101", discussion(discussion_id="21", assignment_id=None, own_count=1), rules=rules
        )
        other_course = hs.normalize_discussion_participation(
            "102", discussion(assignment_id=None, own_count=1), rules=rules
        )

        self.assertEqual("not_participated", absent["state"])
        self.assertEqual("participated", present["state"])
        self.assertEqual("approved_own_reply_rule", present["basis"])
        self.assertEqual("explicit-rule", present["rule_id"])
        self.assertEqual("not_evaluated", other_record["state"])
        self.assertEqual("not_evaluated", other_course["state"])
        self.assertEqual("no_approved_rule", other_record["basis"])

    def test_operational_rule_is_an_immutable_exact_fifteen_record_allowlist(self):
        expected_ids = {str(value) for value in range(909071, 909086)}

        self.assertEqual(15, len(hs.APPROVED_OWN_REPLY_RULES))
        self.assertEqual(
            {("611697", discussion_id) for discussion_id in expected_ids},
            set(hs.APPROVED_OWN_REPLY_RULES),
        )
        self.assertEqual(
            {hs.EXAMPLE_OWN_REPLY_RULE_ID},
            set(hs.APPROVED_OWN_REPLY_RULES.values()),
        )
        with self.assertRaises(TypeError):
            hs.APPROVED_OWN_REPLY_RULES[("611697", "909086")] = "must-not-expand"

    def test_non_collected_entries_are_unknown_not_zero(self):
        for entries_state in ("restricted", "unavailable", "not_available"):
            with self.subTest(entries_state=entries_state):
                graded = hs.normalize_discussion_participation(
                    "101", discussion(workflow_state="submitted", entries_state=entries_state)
                )
                ruled = hs.normalize_discussion_participation(
                    "101",
                    discussion(assignment_id=None, entries_state=entries_state),
                    rules={("101", "20"): "explicit-rule"},
                )
                for result in (graded, ruled):
                    self.assertEqual("unknown", result["state"])
                    self.assertIsNone(result["own_entry_count"])
                    self.assertIsNone(result["last_own_reply_at"])

    def test_projection_excludes_bodies_names_feedback_and_entry_ids(self):
        result = hs.normalize_discussion_participation(
            "101", discussion(workflow_state="submitted", own_count=2)
        )
        encoded = json.dumps(result, ensure_ascii=False)

        self.assertEqual(2, result["own_entry_count"])
        self.assertEqual("2026-09-02T10:00:00+09:00", result["last_own_reply_at"])
        for forbidden in (
            "private-own-body",
            "private-peer-feedback",
            "Private Student",
            "private-submission-body",
            "private-comment",
            "feedback_entries",
            '"id"',
        ):
            self.assertNotIn(forbidden, encoded)

    def test_report_is_sorted_counted_and_privacy_safe(self):
        snapshot = {
            "schema_version": 5,
            "term": {"id": "26-2"},
            "course": {"id": "101", "name": "자료구조"},
            "discussions": [
                discussion(discussion_id="21", workflow_state="graded", own_count=1),
                discussion(discussion_id="20", workflow_state="unsubmitted", own_count=0),
            ],
        }
        report = hs.discussion_participation_report(snapshot)
        encoded = json.dumps(report, ensure_ascii=False)

        self.assertEqual(["20", "21"], [item["discussion_id"] for item in report["records"]])
        self.assertEqual(2, report["counts"]["total"])
        self.assertEqual(1, report["counts"]["participated"])
        self.assertEqual(1, report["counts"]["not_participated"])
        self.assertNotIn("private-own-body", encoded)
        self.assertNotIn("Private Student", encoded)

    def test_report_rejects_unknown_snapshot_schema(self):
        snapshot = {
            "schema_version": 6,
            "term": {"id": "26-2"},
            "course": {"id": "101", "name": "자료구조"},
            "discussions": [],
        }
        with self.assertRaises(hs.HylmsError) as caught:
            hs.discussion_participation_report(snapshot)
        self.assertEqual("discussion_participation_invalid", caught.exception.code)

    def test_validator_rejects_inconsistent_or_malformed_projection(self):
        valid = hs.normalize_discussion_participation(
            "101", discussion(workflow_state="submitted", own_count=1)
        )
        invalid_values = []
        extra = copy.deepcopy(valid)
        extra["body"] = "forbidden"
        invalid_values.append(extra)
        negative = copy.deepcopy(valid)
        negative["own_entry_count"] = -1
        invalid_values.append(negative)
        inconsistent = copy.deepcopy(valid)
        inconsistent["state"] = "not_participated"
        invalid_values.append(inconsistent)
        noncanonical_time = copy.deepcopy(valid)
        noncanonical_time["last_own_reply_at"] = "2026-09-01T01:00:00Z"
        invalid_values.append(noncanonical_time)
        date_only = copy.deepcopy(valid)
        date_only["last_own_reply_at"] = "2026-09-01"
        invalid_values.append(date_only)
        fractional_attempt = copy.deepcopy(valid)
        fractional_attempt["submission"]["attempt"] = 1.5
        invalid_values.append(fractional_attempt)

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(hs.HylmsError) as caught:
                    hs.validate_discussion_participation(value)
                self.assertEqual("discussion_participation_invalid", caught.exception.code)

    def test_duplicate_own_entry_ids_and_items_under_restriction_fail_closed(self):
        duplicate = discussion(workflow_state="submitted", own_count=2)
        duplicate["entries"]["items"][1]["id"] = duplicate["entries"]["items"][0]["id"]
        restricted = discussion(entries_state="restricted")
        restricted["entries"]["items"] = [{"id": "1", "created_at": None}]
        for value in (duplicate, restricted):
            with self.subTest(value=value):
                with self.assertRaises(hs.HylmsError):
                    hs.normalize_discussion_participation("101", value)


if __name__ == "__main__":
    unittest.main()
