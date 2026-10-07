"""Classic and New Quiz settings, visibility, and score normalization."""

from __future__ import annotations

from typing import Any, Mapping

from .core import kst_iso, parse_iso_datetime

def _requirement(value: Any, *, present: bool = True) -> dict[str, Any]:
    return {
        "required": bool(value) if present else None,
        "state": "known" if present else "provider_omitted",
    }


def normalize_classic_quiz_settings(raw: Mapping[str, Any]) -> dict[str, Any]:
    allowed_attempts = raw.get("allowed_attempts")
    if "allowed_attempts" not in raw:
        attempts_state = "provider_omitted"
    elif allowed_attempts == -1:
        attempts_state = "unbounded"
    else:
        attempts_state = "known"
    if "hide_results" not in raw:
        result_state = "provider_omitted"
    elif raw.get("hide_results") == "always":
        result_state = "hidden"
    elif raw.get("hide_results") == "until_after_last_attempt":
        result_state = "after_last_attempt"
    else:
        result_state = "visible"
    if "show_correct_answers" not in raw:
        correct_state = "provider_omitted"
    elif raw.get("show_correct_answers") is False:
        correct_state = "hidden"
    elif raw.get("show_correct_answers_at") or raw.get("hide_correct_answers_at"):
        correct_state = "scheduled"
    else:
        correct_state = "visible"
    return {
        "quiz_type": raw.get("quiz_type"),
        "points_possible": raw.get("points_possible"),
        "question_count": raw.get("question_count"),
        "time_limit_minutes": raw.get("time_limit"),
        "attempts": {
            "allowed": allowed_attempts,
            "unlimited": allowed_attempts == -1 if "allowed_attempts" in raw else None,
            "scoring_policy": raw.get("scoring_policy"),
            "state": attempts_state,
        },
        "navigation": {
            "shuffle_answers": raw.get("shuffle_answers"),
            "one_question_at_a_time": raw.get("one_question_at_a_time"),
            "cant_go_back": raw.get("cant_go_back"),
        },
        "result_visibility": {
            "state": result_state,
            "correct_answers_state": correct_state,
            "show_correct_answers_at": kst_iso(raw.get("show_correct_answers_at")),
            "hide_correct_answers_at": kst_iso(raw.get("hide_correct_answers_at")),
            "one_time_results": raw.get("one_time_results"),
        },
        "access_requirements": {
            "access_code": _requirement(raw.get("access_code"), present="access_code" in raw),
            "ip_filter": _requirement(raw.get("ip_filter"), present="ip_filter" in raw),
            "lockdown_browser": _requirement(
                raw.get("require_lockdown_browser"), present="require_lockdown_browser" in raw
            ),
        },
    }


def normalize_new_quiz_settings(raw: Mapping[str, Any]) -> dict[str, Any]:
    settings = raw.get("quiz_settings") if isinstance(raw.get("quiz_settings"), dict) else {}
    multiple = settings.get("multiple_attempts") if isinstance(settings.get("multiple_attempts"), dict) else {}
    results = settings.get("result_view_settings") if isinstance(settings.get("result_view_settings"), dict) else {}
    multiple_enabled = settings.get("multiple_attempts", {}).get("multiple_attempts_enabled") if isinstance(settings.get("multiple_attempts"), dict) else None
    attempt_limit = multiple.get("attempt_limit")
    max_attempts = multiple.get("max_attempts")
    if not settings:
        attempts_state = "provider_omitted"
    elif multiple_enabled is False:
        attempts_state = "known"
        max_attempts = 1
    elif attempt_limit is False or max_attempts is None:
        attempts_state = "unbounded"
    else:
        attempts_state = "known"
    one_at_a_time = settings.get("one_at_a_time_type")
    display_items = results.get("display_items")
    if display_items is None:
        result_state = "provider_omitted"
    elif display_items is False:
        result_state = "hidden"
    else:
        result_state = "visible"
    display_correctness = results.get("display_item_response_correctness")
    if display_correctness is None:
        correct_state = "provider_omitted"
    elif display_correctness is False:
        correct_state = "hidden"
    elif results.get("show_item_response_correctness_at") or results.get("hide_item_response_correctness_at"):
        correct_state = "scheduled"
    else:
        correct_state = "visible"
    return {
        "quiz_type": "new_quiz",
        "points_possible": raw.get("points_possible"),
        "question_count": raw.get("question_count"),
        "time_limit_seconds": settings.get("session_time_limit_in_seconds"),
        "attempts": {
            "allowed": max_attempts,
            "unlimited": attempts_state == "unbounded" if attempts_state != "provider_omitted" else None,
            "scoring_policy": multiple.get("score_to_keep"),
            "state": attempts_state,
        },
        "navigation": {
            "shuffle_answers": settings.get("shuffle_answers"),
            "shuffle_questions": settings.get("shuffle_questions"),
            "one_question_at_a_time": bool(one_at_a_time) if one_at_a_time is not None else None,
            "cant_go_back": not settings.get("allow_backtracking")
            if settings.get("allow_backtracking") is not None
            else None,
        },
        "result_visibility": {
            "state": result_state,
            "correct_answers_state": correct_state,
            "show_correct_answers_at": kst_iso(results.get("show_item_response_correctness_at")),
            "hide_correct_answers_at": kst_iso(results.get("hide_item_response_correctness_at")),
            "one_time_results": results.get("result_restriction"),
        },
        "access_requirements": {
            "access_code": _requirement(
                settings.get("require_student_access_code"),
                present="require_student_access_code" in settings,
            ),
            "ip_filter": _requirement(
                settings.get("filter_ip_address"), present="filter_ip_address" in settings
            ),
            "lockdown_browser": _requirement(
                settings.get("require_lockdown_browser"),
                present="require_lockdown_browser" in settings,
            ),
        },
    }


def safe_quiz_lock_reason(
    raw: Mapping[str, Any], settings: Mapping[str, Any], locked: bool
) -> str | None:
    requirements = settings.get("access_requirements") or {}
    reasons = [
        f"{name}_required"
        for name, value in requirements.items()
        if isinstance(value, dict) and value.get("required") is True
    ]
    if reasons:
        return ",".join(reasons)
    return "locked_for_user" if locked else None


def quiz_score_state(
    submission: Mapping[str, Any] | None, result_state: str
) -> str:
    if submission is None:
        return "not_applicable"
    if submission.get("score") is not None:
        return "known"
    workflow = submission.get("workflow_state")
    if workflow in {"pending_review", "untaken", "settings_only"}:
        return "pending"
    if result_state in {"hidden", "after_last_attempt"}:
        return "hidden"
    return "provider_omitted"


def elapsed_seconds(started_at: Any, finished_at: Any) -> int | None:
    try:
        start = parse_iso_datetime(started_at)
        finish = parse_iso_datetime(finished_at)
    except ValueError:
        return None
    if start is None or finish is None:
        return None
    return max(0, int((finish - start).total_seconds()))
