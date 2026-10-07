"""Assignment-group collection and assignment record normalization."""

from __future__ import annotations

from typing import Any, Mapping
import re
from urllib.parse import urljoin, urlsplit

from ..content import clean_html, normalize_attachment, normalize_rules, normalize_submission, sanitize_url
from ..core import CANVAS_ORIGIN, CanvasHTTPError, HylmsError
from ..schedule import evaluate_summary, normalize_schedule
from ..http import canonical_origin


def external_tool_kind(attributes: Any) -> str:
    """Identify a known content viewer without retaining or launching its URL."""
    if not isinstance(attributes, Mapping):
        return "unknown"
    url = attributes.get("url")
    if not isinstance(url, str) or not url or url != url.strip() or any(ord(c) < 32 for c in url):
        return "unknown"
    try:
        parsed = urlsplit(urljoin(CANVAS_ORIGIN + "/", url))
        if (parsed.scheme == "https" and parsed.hostname == urlsplit(CANVAS_ORIGIN).hostname
                and parsed.port in {None, 443} and parsed.username is None and parsed.password is None
                and parsed.path == "/learningx/lti/coursebuilder/view/text"):
            return "learningx_text"
    except ValueError:
        pass
    return "unknown"


class AssignmentCollectorMixin:
    def _assignment_document_candidates(self, assignments, candidates, course_id, base_url):
        """Collect teacher-provided file links independently of actionability."""
        for assignment in assignments:
            refs = []
            links = assignment.get("links") or {}
            for attachment in links.get("attachments") or []:
                file_id = str(attachment.get("id") or "")
                if not file_id.isdecimal():
                    continue
                hint = attachment.get("display_name") or attachment.get("filename")
                self._add_document_candidate(candidates, file_id, hint, assignment["id"], "attachment", source="assignment")
                refs.append(f"canvas-file:{file_id}")
            for link in links.get("body") or []:
                url = link.get("url")
                if not isinstance(url, str):
                    continue
                try:
                    parsed = urlsplit(urljoin(base_url + "/", url))
                    if canonical_origin(parsed.geturl()) != canonical_origin(CANVAS_ORIGIN) or parsed.username or parsed.password:
                        continue
                except ValueError:
                    continue
                match = re.fullmatch(r"/courses/(\d+)/files/(\d+)(?:/download)?/?", parsed.path)
                if match:
                    if match.group(1) != course_id:
                        continue
                    file_id = match.group(2)
                else:
                    match = re.fullmatch(r"/files/(\d+)(?:/download)?/?", parsed.path)
                    if not match:
                        continue
                    file_id = match.group(1)
                self._add_document_candidate(candidates, file_id, link.get("text"), assignment["id"], "body_link", source="assignment")
                refs.append(f"canvas-file:{file_id}")
            assignment["document_refs"] = list(dict.fromkeys(refs))

    def _collect_assignments(
        self, course_id: str, base_url: str
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Mapping[str, Any]],
        dict[str, Mapping[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        raw_groups = self.client.get_paginated(
            f"/api/v1/courses/{course_id}/assignment_groups",
            {
                "include[]": ["assignments", "submission", "all_dates", "overrides", "discussion_topic"],
                "override_assignment_dates": "true",
                "per_page": 100,
            },
        )
        groups: list[dict[str, Any]] = []
        raw_by_id: dict[str, Mapping[str, Any]] = {}
        by_quiz_id: dict[str, Mapping[str, Any]] = {}
        ordered_assignment_ids: list[str] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict) or raw_group.get("id") is None:
                raise HylmsError("assignment_groups_invalid", "Canvas assignment group 형식이 올바르지 않습니다.")
            items: list[dict[str, Any]] = []
            for raw_assignment in raw_group.get("assignments") or []:
                if not isinstance(raw_assignment, dict) or raw_assignment.get("id") is None:
                    raise HylmsError("assignments_invalid", "Canvas 과제 형식이 올바르지 않습니다.")
                assignment_id = str(raw_assignment["id"])
                raw_by_id.setdefault(assignment_id, raw_assignment)
                if assignment_id not in ordered_assignment_ids:
                    ordered_assignment_ids.append(assignment_id)
                quiz_id = raw_assignment.get("quiz_id")
                is_new_quiz = bool(raw_assignment.get("is_quiz_assignment") and quiz_id is None)
                if quiz_id is not None:
                    quiz_key = str(quiz_id)
                    by_quiz_id[quiz_key] = raw_assignment
                    items.append({"kind": "quiz", "id": quiz_key, "assignment_id": assignment_id})
                elif is_new_quiz:
                    items.append({"kind": "quiz", "id": assignment_id, "assignment_id": assignment_id})
                else:
                    items.append({"kind": "assignment", "id": assignment_id})
            groups.append(
                {
                    "id": str(raw_group["id"]),
                    "name": raw_group.get("name"),
                    "position": raw_group.get("position"),
                    "group_weight": raw_group.get("group_weight"),
                    "sis_source_id": str(raw_group["sis_source_id"])
                    if raw_group.get("sis_source_id") is not None
                    else None,
                    "rules": normalize_rules(raw_group.get("rules")),
                    "items": items,
                }
            )

        assignments: list[dict[str, Any]] = []
        normalized_by_id: dict[str, dict[str, Any]] = {}
        for assignment_id in ordered_assignment_ids:
            raw = raw_by_id[assignment_id]
            if raw.get("quiz_id") is not None or raw.get("is_quiz_assignment") is True:
                continue
            submission, restricted = self._assignment_submission(course_id, assignment_id, raw.get("submission"))
            normalized = self._normalize_assignment(raw, submission, restricted, base_url)
            assignments.append(normalized)
            normalized_by_id[assignment_id] = normalized
        return groups, assignments, raw_by_id, by_quiz_id, normalized_by_id

    def _assignment_submission(
        self, course_id: str, assignment_id: str, embedded: Any
    ) -> tuple[Mapping[str, Any] | None, bool]:
        try:
            payload = self.client.get_json(
                f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self",
                {"include[]": ["submission_history", "submission_comments", "rubric_assessment"]},
            )
            if payload is None:
                return embedded if isinstance(embedded, dict) else None, False
            if not isinstance(payload, dict):
                raise HylmsError("submission_invalid", "Canvas 제출물 형식이 올바르지 않습니다.")
            return payload, False
        except CanvasHTTPError as exc:
            if exc.status in {401, 403, 404}:
                return embedded if isinstance(embedded, dict) else None, True
            raise

    def _normalize_assignment(
        self,
        raw: Mapping[str, Any],
        submission_raw: Mapping[str, Any] | None,
        submission_restricted: bool,
        base_url: str,
    ) -> dict[str, Any]:
        assignment_id = str(raw["id"])
        source_url = sanitize_url(raw.get("html_url") or f"{base_url}/assignments/{assignment_id}", base_url)
        description, _ = clean_html(raw.get("description"), source_url or base_url)
        attachment_values = [
            normalize_attachment(item, source_url or base_url)
            for item in raw.get("attachments") or []
            if isinstance(item, dict)
        ]
        schedule = normalize_schedule(raw, raw.get("all_dates"), self.user_id)
        submission = normalize_submission(submission_raw, source_url or base_url)
        locked = bool(raw.get("locked_for_user"))
        access_state = "locked" if locked else "restricted" if submission_restricted else "available"
        reason = raw.get("lock_explanation") if locked else "submission_restricted" if submission_restricted else None
        submission_types = list(raw.get("submission_types") or [])
        submission_required = any(value not in {"none", "not_graded"} for value in submission_types)
        omit_from_final_grade = raw.get("omit_from_final_grade")
        tool = {"external_tool_kind": external_tool_kind(raw.get("external_tool_tag_attributes"))} if "external_tool" in submission_types else {}
        points = raw.get("points_possible")
        if (set(submission_types) == {"external_tool"}
                and tool.get("external_tool_kind") == "learningx_text"
                and omit_from_final_grade is True and type(points) in {int, float} and points == 0):
            submission_required = False
        informational = bool(
            omit_from_final_grade is True
            and raw.get("points_possible") == 0
            and not submission_required
        )
        allowed_attempts = raw.get("allowed_attempts")
        if "allowed_attempts" not in raw:
            attempts_state = "provider_omitted"
        elif allowed_attempts in {None, -1}:
            attempts_state = "unbounded"
        else:
            attempts_state = "known"
        return {
            "id": assignment_id,
            "assignment_group_id": str(raw["assignment_group_id"])
            if raw.get("assignment_group_id") is not None
            else None,
            "title": raw.get("name"),
            "description": description,
            "source_url": source_url,
            "position": raw.get("position"),
            "points_possible": raw.get("points_possible"),
            "grading_type": raw.get("grading_type"),
            "omit_from_final_grade": omit_from_final_grade,
            "submission_types": submission_types,
            "allowed_extensions": list(raw.get("allowed_extensions") or []),
            "submission_required": submission_required,
            "informational": informational,
            **tool,
            "attempts": {
                "allowed": allowed_attempts,
                "unlimited": allowed_attempts in {None, -1} if "allowed_attempts" in raw else None,
                "state": attempts_state,
            },
            "peer_review": {
                "enabled": raw.get("peer_reviews"),
                "automatic_assignment": raw.get("automatic_peer_reviews"),
                "anonymous": raw.get("anonymous_peer_reviews"),
            },
            "group_assignment": {
                "enabled": raw.get("group_category_id") is not None,
                "group_category_id": str(raw["group_category_id"])
                if raw.get("group_category_id") is not None
                else None,
            },
            "links": {"body": list(description["links"]), "attachments": attachment_values},
            "schedule": schedule,
            "access": {"state": access_state, "reason": reason},
            "progress": {
                "workflow_state": (submission_raw or {}).get("workflow_state"),
                "submitted": bool(
                    (submission_raw or {}).get("submitted_at")
                    or (submission_raw or {}).get("workflow_state") in {"submitted", "graded"}
                ),
                "graded": (submission_raw or {}).get("workflow_state") == "graded",
            },
            "summary_state": evaluate_summary(schedule, submission_raw, self.now),
            "submission": submission,
        }
