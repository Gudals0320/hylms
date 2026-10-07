"""Classic and New Quiz collection and normalized assessment records."""

from __future__ import annotations

from typing import Any, Mapping

from ..assessments import (
    elapsed_seconds,
    normalize_classic_quiz_settings,
    normalize_new_quiz_settings,
    quiz_score_state,
    safe_quiz_lock_reason,
)
from ..content import clean_html, sanitize_url
from ..core import CanvasHTTPError, HylmsError, kst_iso, source_status, unwrap_collection
from ..schedule import evaluate_summary, normalize_schedule


class QuizCollectorMixin:
    def _collect_classic_quizzes(
        self,
        course_id: str,
        base_url: str,
        assignment_by_quiz_id: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
        endpoint_state = "collected"
        try:
            raw_quizzes = self.client.get_paginated(
                f"/api/v1/courses/{course_id}/quizzes", {"per_page": 100}
            )
        except CanvasHTTPError as exc:
            if exc.status == 404:
                endpoint_state = "not_available"
                raw_quizzes = []
            elif exc.status in {401, 403}:
                endpoint_state = "restricted"
                raw_quizzes = []
            else:
                raise

        quizzes: list[dict[str, Any]] = []
        restricted_count = 0
        detail_failed_count = 0
        warnings: list[str] = []
        seen_quiz_ids: set[str] = set()
        for raw in raw_quizzes:
            if not isinstance(raw, dict) or raw.get("id") is None:
                raise HylmsError("quizzes_invalid", "Canvas Classic Quiz 형식이 올바르지 않습니다.")
            quiz_id = str(raw["id"])
            seen_quiz_ids.add(quiz_id)
            assignment_raw = assignment_by_quiz_id.get(quiz_id)
            quiz, restricted, warning = self._normalize_classic_quiz(
                course_id, base_url, raw, assignment_raw
            )
            restricted_count += int(restricted)
            if warning:
                detail_failed_count += 1
                warnings.append(warning)
            quizzes.append(quiz)
        for quiz_id, assignment_raw in assignment_by_quiz_id.items():
            if quiz_id in seen_quiz_ids:
                continue
            placeholder_state = endpoint_state if endpoint_state != "collected" else "restricted"
            quizzes.append(
                self._classic_quiz_placeholder(quiz_id, assignment_raw, base_url, placeholder_state)
            )
            restricted_count += 1
        source = source_status(len(quizzes), restricted_count=0)
        source["restricted_count"] = restricted_count
        source["detail_failed_count"] = detail_failed_count
        if endpoint_state == "not_available" and not quizzes:
            source.update({"status": "empty", "reason": "endpoint_404", "completeness": "complete"})
        elif endpoint_state != "collected":
            source.update({"status": endpoint_state, "completeness": endpoint_state})
        else:
            source["completeness"] = "restricted" if restricted_count else "complete"
        if detail_failed_count:
            source["completeness"] = "with_warnings"
        return quizzes, source, list(dict.fromkeys(warnings))

    def _classic_quiz_placeholder(
        self,
        quiz_id: str,
        assignment_raw: Mapping[str, Any],
        base_url: str,
        state: str,
    ) -> dict[str, Any]:
        assignment_id = str(assignment_raw["id"])
        source_url = sanitize_url(
            assignment_raw.get("html_url") or f"{base_url}/assignments/{assignment_id}", base_url
        )
        description, _ = clean_html(assignment_raw.get("description"), source_url or base_url)
        schedule = normalize_schedule(
            assignment_raw, assignment_raw.get("all_dates"), self.user_id
        )
        settings = normalize_classic_quiz_settings(assignment_raw)
        return {
            "id": quiz_id,
            "assignment_id": assignment_id,
            "source_kind": "classic_quiz",
            "title": assignment_raw.get("name"),
            "description": description,
            "source_url": source_url,
            "position": assignment_raw.get("position"),
            "schedule": schedule,
            "access": {"state": state, "reason": f"endpoint_{state}"},
            "progress": {"workflow_state": None, "attempt": None},
            "summary_state": evaluate_summary(schedule, None, self.now),
            "submission": None,
            "questions": [],
            "detail_state": state,
            "score_state": "not_applicable",
            **settings,
        }

    def _normalize_classic_quiz(
        self,
        course_id: str,
        base_url: str,
        raw: Mapping[str, Any],
        assignment_raw: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], bool, str | None]:
        quiz_id = str(raw["id"])
        detail_state = "collected"
        detail_reason = None
        detail_warning = None
        try:
            payload = self.client.get_json(
                f"/api/v1/courses/{course_id}/quizzes/{quiz_id}"
            )
            if not isinstance(payload, dict):
                raise HylmsError("quiz_detail_invalid", "Canvas Quiz 상세 형식이 올바르지 않습니다.")
            raw = {**raw, **payload}
        except CanvasHTTPError as exc:
            if exc.status in {401, 403}:
                detail_state = "restricted"
                detail_reason = "provider_restricted"
            elif exc.status == 404:
                detail_state = "not_available"
                detail_reason = "endpoint_404"
            else:
                detail_state = "unavailable"
                detail_reason = "detail_request_failed"
                detail_warning = "quiz_detail_failed"
        except HylmsError:
            detail_state = "unavailable"
            detail_reason = "detail_response_invalid"
            detail_warning = "quiz_detail_failed"
        source_url = sanitize_url(raw.get("html_url") or f"{base_url}/quizzes/{quiz_id}", base_url)
        description, _ = clean_html(raw.get("description"), source_url or base_url)
        submission_raw: Mapping[str, Any] | None = None
        submission_restricted = False
        try:
            payload = self.client.get_json(
                f"/api/v1/courses/{course_id}/quizzes/{quiz_id}/submission",
                {"include[]": ["submission"]},
            )
            candidates = payload.get("quiz_submissions", []) if isinstance(payload, dict) else []
            if isinstance(candidates, list) and candidates:
                submission_raw = max(
                    (item for item in candidates if isinstance(item, dict)),
                    key=lambda item: item.get("attempt") or 0,
                    default=None,
                )
        except CanvasHTTPError as exc:
            if exc.status in {401, 403}:
                submission_restricted = True
            elif exc.status != 404:
                raise

        questions_raw: list[Mapping[str, Any]] = []
        questions_restricted = False
        try:
            values = self.client.get_paginated(
                f"/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions", {"per_page": 100}
            )
            questions_raw = [value for value in values if isinstance(value, dict)]
        except CanvasHTTPError as exc:
            if exc.status in {401, 403, 404}:
                questions_restricted = True
            else:
                raise

        submission_questions: dict[str, Mapping[str, Any]] = {}
        if submission_raw and submission_raw.get("id") is not None:
            try:
                payload = self.client.get_json(
                    f"/api/v1/quiz_submissions/{submission_raw['id']}/questions",
                    {"include[]": ["quiz_question"]},
                )
                values = unwrap_collection(
                    payload,
                    "quiz_submission_questions",
                    error_code="quiz_submission_questions_invalid",
                    label="Canvas Quiz 제출 문항",
                )
                submission_questions = {
                    str(value["id"]): value
                    for value in values
                    if isinstance(value, dict) and value.get("id") is not None
                }
                known_question_ids = {
                    str(value.get("id")) for value in questions_raw if value.get("id") is not None
                }
                for value in submission_questions.values():
                    embedded = value.get("quiz_question")
                    if (
                        isinstance(embedded, dict)
                        and embedded.get("id") is not None
                        and str(embedded["id"]) not in known_question_ids
                    ):
                        questions_raw.append(embedded)
                        known_question_ids.add(str(embedded["id"]))
            except CanvasHTTPError as exc:
                if exc.status in {401, 403, 404}:
                    questions_restricted = True
                else:
                    raise

        summary_submission = dict(submission_raw or {})
        nested_submission = summary_submission.get("submission")
        if isinstance(nested_submission, dict):
            for key in ("submitted_at", "graded_at", "late", "missing", "workflow_state"):
                if summary_submission.get(key) is None and nested_submission.get(key) is not None:
                    summary_submission[key] = nested_submission[key]
        questions = [
            self._normalize_quiz_question(question, submission_questions.get(str(question.get("id"))), source_url or base_url,
                                          questions_restricted, submission_raw is not None)
            for question in questions_raw
        ]
        schedule_source = assignment_raw or raw
        schedule = normalize_schedule(
            schedule_source,
            (assignment_raw or {}).get("all_dates") if assignment_raw else None,
            self.user_id,
        )
        locked = bool(raw.get("locked_for_user") or (assignment_raw or {}).get("locked_for_user"))
        restricted = submission_restricted or questions_restricted or detail_state in {"restricted", "not_available"}
        access_state = "locked" if locked else "restricted" if restricted else "available"
        submission = None
        if submission_raw:
            nested = submission_raw.get("submission") if isinstance(submission_raw.get("submission"), dict) else {}
            duration_value = submission_raw.get("time_spent")
            duration_basis = "provider" if duration_value is not None else None
            if duration_value is None:
                duration_value = elapsed_seconds(
                    submission_raw.get("started_at"), submission_raw.get("finished_at")
                )
                duration_basis = "derived_elapsed" if duration_value is not None else None
            overdue = submission_raw.get("overdue_and_needs_submission")
            overdue_basis = "quiz_submission" if "overdue_and_needs_submission" in submission_raw else None
            if overdue is None and "late" in nested:
                overdue = nested.get("late")
                overdue_basis = "assignment_submission"
            settings = normalize_classic_quiz_settings(raw)
            submission = {
                "id": str(submission_raw["id"]) if submission_raw.get("id") is not None else None,
                "attempt": submission_raw.get("attempt"),
                "workflow_state": submission_raw.get("workflow_state"),
                "started_at": kst_iso(submission_raw.get("started_at")),
                "finished_at": kst_iso(submission_raw.get("finished_at")),
                "score": submission_raw.get("score"),
                "kept_score": submission_raw.get("kept_score"),
                "late": submission_raw.get("late"),
                "missing": submission_raw.get("missing"),
                "attempts_remaining": submission_raw.get("attempts_left"),
                "end_at": kst_iso(submission_raw.get("end_at")),
                "duration_seconds": duration_value,
                "duration_basis": duration_basis,
                "overdue": overdue,
                "overdue_basis": overdue_basis,
                "score_state": quiz_score_state(
                    submission_raw, settings["result_visibility"]["state"]
                ),
            }
        settings = normalize_classic_quiz_settings(raw)
        return (
            {
                "id": quiz_id,
                "assignment_id": str(raw["assignment_id"])
                if raw.get("assignment_id") is not None
                else str(assignment_raw["id"]) if assignment_raw and assignment_raw.get("id") is not None else None,
                "source_kind": "classic_quiz",
                "assignment_group_id": str(raw["assignment_group_id"])
                if raw.get("assignment_group_id") is not None
                else str(assignment_raw["assignment_group_id"])
                if assignment_raw and assignment_raw.get("assignment_group_id") is not None
                else None,
                "title": raw.get("title"),
                "description": description,
                "source_url": source_url,
                "position": raw.get("position"),
                "schedule": schedule,
                "access": {
                    "state": access_state,
                    "reason": safe_quiz_lock_reason(raw, settings, locked)
                    if locked
                    else detail_reason or "provider_restricted"
                    if restricted
                    else None,
                },
                "progress": {
                    "workflow_state": (submission_raw or {}).get("workflow_state"),
                    "attempt": (submission_raw or {}).get("attempt"),
                },
                "summary_state": evaluate_summary(schedule, summary_submission or None, self.now),
                "submission": submission,
                "questions": questions,
                "detail_state": detail_state,
                "detail_reason": detail_reason,
                "score_state": quiz_score_state(
                    submission_raw, settings["result_visibility"]["state"]
                ),
                **settings,
            },
            restricted,
            detail_warning,
        )

    @staticmethod
    def _normalize_quiz_question(
        question: Mapping[str, Any],
        submission_question: Mapping[str, Any] | None,
        base_url: str,
        restricted: bool,
        has_submission: bool,
    ) -> dict[str, Any]:
        prompt, _ = clean_html(question.get("question_text"), base_url)
        possible_answers: list[dict[str, Any]] = []
        for answer in question.get("answers") or []:
            if not isinstance(answer, dict):
                continue
            answer_content, _ = clean_html(answer.get("html") or answer.get("text"), base_url)
            possible_answers.append(
                {
                    "id": str(answer["id"]) if answer.get("id") is not None else None,
                    "content": answer_content,
                    "correct": answer.get("correct"),
                }
            )
        if submission_question is not None and "answer" in submission_question:
            answer_value = submission_question.get("answer")
            option_ids = {item["id"] for item in possible_answers if item.get("id") is not None}
            if isinstance(answer_value, (str, int)) and str(answer_value) in option_ids:
                answer_value = str(answer_value)
            elif isinstance(answer_value, list):
                answer_value = [str(item) if str(item) in option_ids else item for item in answer_value]
            user_answer = {"state": "provided", "reason": None, "value": answer_value}
        elif has_submission:
            user_answer = {"state": "restricted", "reason": "provider_omitted", "value": None}
        elif restricted:
            user_answer = {"state": "restricted", "reason": "provider_restricted", "value": None}
        else:
            user_answer = {"state": "not_applicable", "reason": "no_submission", "value": None}
        correctness = None
        if submission_question is not None:
            correctness = submission_question.get("correct")
        return {
            "id": str(question["id"]) if question.get("id") is not None else None,
            "position": question.get("position"),
            "question_type": question.get("question_type"),
            "prompt": prompt,
            "points_possible": question.get("points_possible") or question.get("points"),
            "possible_answers": possible_answers,
            "correct": correctness,
            "user_answer": user_answer,
        }

    def _collect_new_quizzes(
        self,
        course_id: str,
        base_url: str,
        assignment_raw_by_id: Mapping[str, Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
        flagged = {
            assignment_id: raw
            for assignment_id, raw in assignment_raw_by_id.items()
            if raw.get("is_quiz_assignment") is True and raw.get("quiz_id") is None
        }
        endpoint_state = "collected"
        raw_values: list[Mapping[str, Any]] = []
        try:
            values = self.client.get_paginated(
                f"/api/quiz/v1/courses/{course_id}/quizzes", {"per_page": 100}
            )
            raw_values = [value for value in values if isinstance(value, dict)]
        except CanvasHTTPError as exc:
            if exc.status == 404:
                endpoint_state = "not_available"
            elif exc.status in {401, 403}:
                endpoint_state = "restricted"
            else:
                raise

        quizzes: list[dict[str, Any]] = []
        warnings: list[str] = []
        detail_failed_count = 0
        detail_restricted_count = 0
        seen_assignments: set[str] = set()
        for raw in raw_values:
            quiz_id = str(raw.get("id")) if raw.get("id") is not None else None
            assignment_id = str(raw.get("assignment_id") or raw.get("id")) if raw.get("assignment_id") is not None or raw.get("id") is not None else None
            if not quiz_id or not assignment_id:
                continue
            seen_assignments.add(assignment_id)
            assignment = flagged.get(assignment_id)
            detail_state = "collected"
            detail_reason = None
            detail = dict(raw)
            try:
                payload = self.client.get_json(
                    f"/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}"
                )
                if not isinstance(payload, dict):
                    raise HylmsError("new_quiz_detail_invalid", "Canvas New Quiz 상세 형식이 올바르지 않습니다.")
                detail.update(payload)
            except CanvasHTTPError as exc:
                if exc.status == 404:
                    detail_state = "not_available"
                    detail_reason = "endpoint_404"
                elif exc.status in {401, 403}:
                    detail_state = "restricted"
                    detail_reason = "provider_restricted"
                    detail_restricted_count += 1
                else:
                    detail_state = "unavailable"
                    detail_reason = "detail_request_failed"
                    detail_failed_count += 1
                    warnings.append("new_quiz_detail_failed")
            except HylmsError:
                detail_state = "unavailable"
                detail_reason = "detail_response_invalid"
                detail_failed_count += 1
                warnings.append("new_quiz_detail_failed")
            quizzes.append(
                self._new_quiz_record(
                    detail, assignment, base_url, detail_state, detail_reason
                )
            )
        for assignment_id, assignment in flagged.items():
            if assignment_id in seen_assignments:
                continue
            raw = {"id": assignment_id, "assignment_id": assignment_id, "title": assignment.get("name")}
            placeholder_state = endpoint_state if endpoint_state != "collected" else "restricted"
            if placeholder_state == "restricted":
                detail_restricted_count += 1
            quizzes.append(
                self._new_quiz_record(
                    raw,
                    assignment,
                    base_url,
                    placeholder_state,
                    f"endpoint_{placeholder_state}",
                )
            )

        if endpoint_state == "collected":
            source = source_status(len(quizzes))
            source["restricted_count"] = detail_restricted_count
            if detail_restricted_count:
                source["completeness"] = "restricted"
        else:
            source = {
                "status": endpoint_state,
                "discovered_count": len(flagged),
                "normalized_count": len(quizzes),
                "restricted_count": len(flagged) if endpoint_state == "restricted" else 0,
                "completeness": "restricted" if endpoint_state == "restricted" else "not_available",
            }
        source["detail_failed_count"] = detail_failed_count
        if detail_failed_count:
            source["completeness"] = "with_warnings"
        return quizzes, source, list(dict.fromkeys(warnings))

    def _new_quiz_record(
        self,
        raw: Mapping[str, Any],
        assignment: Mapping[str, Any] | None,
        base_url: str,
        detail_state: str,
        detail_reason: str | None,
    ) -> dict[str, Any]:
        quiz_id = str(raw.get("id"))
        assignment_id = str(raw.get("assignment_id") or (assignment or {}).get("id") or quiz_id)
        source_url = sanitize_url((assignment or {}).get("html_url") or f"{base_url}/assignments/{assignment_id}", base_url)
        description, _ = clean_html(raw.get("instructions") or (assignment or {}).get("description"), source_url or base_url)
        schedule_source = assignment or raw
        schedule = normalize_schedule(schedule_source, schedule_source.get("all_dates"), self.user_id)
        access_state = "available" if detail_state == "collected" else detail_state
        settings = normalize_new_quiz_settings(raw)
        provider_omitted = detail_state == "collected"
        return {
            "id": quiz_id,
            "assignment_id": assignment_id,
            "source_kind": "new_quiz",
            "title": raw.get("title") or (assignment or {}).get("name"),
            "description": description,
            "source_url": source_url,
            "position": (assignment or {}).get("position"),
            "schedule": schedule,
            "access": {"state": access_state, "reason": detail_reason},
            "progress": {"workflow_state": None, "attempt": None},
            "summary_state": evaluate_summary(schedule, None, self.now),
            "submission": None,
            "submission_state": "provider_omitted" if provider_omitted else detail_state,
            "questions": [],
            "detail_state": detail_state,
            "detail_reason": detail_reason,
            "score_state": "provider_omitted" if provider_omitted else "not_applicable",
            **settings,
        }
