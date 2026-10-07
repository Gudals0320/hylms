"""Course-level Canvas collection orchestration and canonical validation."""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Sequence

from .collectors.announcements import AnnouncementCollectorMixin
from .collectors.assignments import AssignmentCollectorMixin
from .collectors.discussions import DiscussionCollectorMixin
from .collectors.quizzes import QuizCollectorMixin
from .content import clean_html
from .core import CANVAS_ORIGIN, KST, SCHEMA_VERSION, SOURCE_NOT_COLLECTED, HylmsError, TermSelection, source_status
from .http import CanvasClient


class CanvasCollector(
    AnnouncementCollectorMixin,
    DiscussionCollectorMixin,
    AssignmentCollectorMixin,
    QuizCollectorMixin,
):
    """Collect and normalize the Build #1 Canvas surface for one course."""

    def __init__(self, client: CanvasClient, *, now: dt.datetime, user_id: str) -> None:
        self.client = client
        self.now = now.astimezone(KST)
        self.user_id = str(user_id)

    def list_courses(self) -> list[Mapping[str, Any]]:
        values = self.client.get_paginated(
            "/api/v1/courses",
            {
                "enrollment_state": "active",
                "include[]": ["term", "syllabus_body", "concluded"],
                "per_page": 100,
            },
        )
        if not all(isinstance(value, dict) and value.get("id") is not None for value in values):
            raise HylmsError("courses_invalid", "Canvas 과목 목록 형식이 올바르지 않습니다.")
        return values

    def collect_course(
        self, course: Mapping[str, Any], term: TermSelection
    ) -> tuple[dict[str, Any], list[str]]:
        course_id = str(course.get("id"))
        base_url = f"{CANVAS_ORIGIN}/courses/{course_id}"
        collected_at = self.now.isoformat(timespec="seconds")
        syllabus, _ = clean_html(course.get("syllabus_body"), base_url)

        announcements, document_candidates = self._announcement_candidates(
            course_id, term, base_url
        )
        (
            assignment_groups,
            assignments,
            assignment_raw_by_id,
            assignment_by_quiz_id,
            assignment_records_by_id,
        ) = self._collect_assignments(course_id, base_url)
        discussions, discussion_source, discussion_warnings = self._collect_discussions(
            course_id,
            base_url,
            assignment_raw_by_id,
            assignment_records_by_id,
        )
        assignments = self._reconcile_discussions(
            assignment_groups, assignments, discussions
        )
        self._assignment_document_candidates(assignments, document_candidates, course_id, base_url)
        documents, document_source, document_warnings = self._resolve_documents(course_id, document_candidates)
        self._filter_document_refs(announcements, documents)
        self._filter_document_refs(assignments, documents)
        classic_quizzes, classic_source, classic_warnings = self._collect_classic_quizzes(
            course_id, base_url, assignment_by_quiz_id
        )
        new_quizzes, new_source, new_quiz_warnings = self._collect_new_quizzes(
            course_id, base_url, assignment_raw_by_id
        )
        quizzes_by_assignment = {
            quiz["assignment_id"]: quiz["id"]
            for quiz in [*classic_quizzes, *new_quizzes]
            if quiz.get("assignment_id")
        }
        for group in assignment_groups:
            for item in group["items"]:
                if item["kind"] == "quiz" and item.get("assignment_id") in quizzes_by_assignment:
                    item["id"] = quizzes_by_assignment[item["assignment_id"]]
        all_quizzes = classic_quizzes + new_quizzes
        self._validate_canonical_references(
            assignment_groups, assignments, discussions, all_quizzes, documents, announcements
        )

        assessment_restricted = int(classic_source.get("restricted_count") or 0) + int(
            new_source.get("restricted_count") or 0
        )
        assessment_detail_failed = int(classic_source.get("detail_failed_count") or 0) + int(
            new_source.get("detail_failed_count") or 0
        )
        assessment_count = len(assignments) + len(all_quizzes) + int(
            discussion_source.get("graded_count") or 0
        )
        assessment_source = {
            "status": "collected" if assessment_count else "empty",
            "discovered_count": assessment_count,
            "normalized_count": assessment_count,
            "assignment_count": len(assignments),
            "quiz_count": len(all_quizzes),
            "graded_discussion_count": int(discussion_source.get("graded_count") or 0),
            "informational_count": sum(1 for item in assignments if item.get("informational") is True),
            "restricted_count": assessment_restricted,
            "detail_failed_count": assessment_detail_failed,
            "completeness": "with_warnings"
            if assessment_detail_failed
            else "restricted"
            if assessment_restricted
            else "complete",
        }

        sources = {
            "canvas_course": source_status(1),
            "syllabus": source_status(1 if syllabus["text"] or syllabus["links"] or syllabus["images"] else 0),
            "announcements": source_status(len(announcements)),
            "assignment_groups": source_status(len(assignment_groups)),
            "assignments": source_status(len(assignments)),
            "classic_quizzes": classic_source,
            "new_quizzes": new_source,
            "canvas_discussions": discussion_source,
            "canvas_assessments": assessment_source,
            "documents": document_source,
            "weekly_learning": dict(SOURCE_NOT_COLLECTED),
            "course_resources": dict(SOURCE_NOT_COLLECTED),
        }
        return (
            {
                "schema_version": SCHEMA_VERSION,
                "collected_at": collected_at,
                "term": {"id": term.term_id, "name": term.name, "canvas_id": term.canvas_id},
                "course": {
                    "id": course_id,
                    "name": course.get("name"),
                    "course_code": course.get("course_code"),
                    "url": base_url,
                    "last_success_at": collected_at,
                },
                "sources": sources,
                "syllabus": syllabus,
                "announcements": announcements,
                "assignment_groups": assignment_groups,
                "assignments": assignments,
                "discussions": discussions,
                "quizzes": sorted(all_quizzes, key=lambda item: (item.get("position") or 0, item["id"])),
                "weekly_learning": [],
                "course_resources": [],
                "documents": documents,
            },
            list(
                dict.fromkeys(
                    document_warnings
                    + discussion_warnings
                    + classic_warnings
                    + new_quiz_warnings
                )
            ),
        )

    @staticmethod
    def _validate_canonical_references(
        groups: Sequence[Mapping[str, Any]],
        assignments: Sequence[Mapping[str, Any]],
        discussions: Sequence[Mapping[str, Any]],
        quizzes: Sequence[Mapping[str, Any]],
        documents: Mapping[str, Any],
        announcements: Sequence[Mapping[str, Any]],
    ) -> None:
        valid = {
            "assignment": {str(item["id"]) for item in assignments},
            "discussion": {str(item["id"]) for item in discussions},
            "quiz": {str(item["id"]) for item in quizzes},
        }
        for group in groups:
            for item in group.get("items") or []:
                kind = item.get("kind")
                if kind not in valid or str(item.get("id")) not in valid[kind]:
                    raise HylmsError(
                        "canonical_reference_invalid",
                        "assignment group canonical reference가 존재하지 않는 record를 가리킵니다.",
                    )
        document_ids = set(documents)
        for record in [*announcements, *assignments]:
            if any(str(reference) not in document_ids for reference in record.get("document_refs") or []):
                raise HylmsError(
                    "canonical_reference_invalid",
                    "공지/과제 document reference가 존재하지 않는 record를 가리킵니다.",
                )
