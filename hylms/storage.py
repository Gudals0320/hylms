"""Snapshot paths, atomic JSON persistence, and course-level failure isolation."""

from __future__ import annotations

import datetime as dt
import copy
import json
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from .collector import CanvasCollector
from .content import sanitize_filename_component
from .core import HylmsError, KST, SCHEMA_VERSION, now_kst, select_current_term, stable_error_code
from .documents import DocumentDownloader
from .http import CanvasClient
from .learningx import LearningXBootstrap, LearningXCollector

DOCUMENT_SUMMARY_KEYS = (
    "discovered_count", "eligible_count", "registered_count",
    "downloaded_count", "existing_count", "failed_count", "restricted_count",
    "skipped_extension_count", "completeness",
)


def course_stem(course: Mapping[str, Any]) -> str:
    return f"{sanitize_filename_component(str(course.get('name') or 'course'))}__c{course['id']}"


def finalize_document_paths(course_data: dict[str, Any]) -> None:
    stem = course_stem(course_data["course"])
    for document in course_data.get("documents", {}).values():
        document["saved_path"] = f"files/{stem}/{document['saved_filename']}"


def preserve_document_paths(course_data: dict[str, Any], previous_path: Path | None) -> None:
    if previous_path is None:
        return
    try:
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        previous_documents = previous.get("documents") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return
    if not isinstance(previous_documents, dict):
        return
    for document_id, document in (course_data.get("documents") or {}).items():
        old_document = previous_documents.get(document_id)
        if not isinstance(old_document, dict):
            continue
        if old_document.get("saved_filename") and old_document.get("saved_path"):
            document["saved_filename"] = old_document["saved_filename"]
            document["saved_path"] = old_document["saved_path"]
            if old_document.get("download_state") in {"downloaded", "existing", "failed", "restricted"}:
                document["download_state"] = old_document["download_state"]


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def snapshot_run_id(started_at: str) -> str:
    try:
        value = dt.datetime.fromisoformat(started_at)
    except (TypeError, ValueError) as exc:
        raise HylmsError("snapshot_archive_invalid", "snapshot 시작 시각을 확인할 수 없습니다.") from exc
    if value.tzinfo is None:
        raise HylmsError("snapshot_archive_invalid", "snapshot 시작 시각에 시간대가 없습니다.")
    return value.astimezone(KST).strftime("%Y%m%dT%H%M%S%z")


def _snapshot_json_paths(term_directory: Path, status: Mapping[str, Any]) -> list[Path]:
    root = term_directory.resolve()
    paths: list[Path] = []
    for course in status.get("courses") or []:
        relative = Path(str(course.get("path") or ""))
        source = (term_directory / relative).resolve()
        if not relative.name or source.parent != root or source.suffix.lower() != ".json":
            raise HylmsError("snapshot_archive_invalid", "과목 snapshot 경로가 올바르지 않습니다.")
        if source.exists():
            paths.append(source)
        elif course.get("status") != "failed":
            raise HylmsError("snapshot_archive_missing", "보존할 과목 snapshot이 없습니다.")
    return paths


def _matching_snapshot_run(term_directory: Path, status: Mapping[str, Any]) -> Path | None:
    runs = term_directory / "runs"
    if not runs.exists():
        return None
    sources = _snapshot_json_paths(term_directory, status)
    for candidate in sorted(runs.iterdir()):
        if not candidate.is_dir() or candidate.name.startswith("."):
            continue
        try:
            archived = json.loads((candidate / "status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if archived.get("started_at") != status.get("started_at"):
            continue
        fields = ("id", "path", "status", "last_success_at", "snapshot_schema_version")
        if [tuple(item.get(field) for field in fields) for item in archived.get("courses") or []] != [
            tuple(item.get(field) for field in fields) for item in status.get("courses") or []
        ]:
            continue
        if all((candidate / source.name).is_file() and (candidate / source.name).read_bytes() == source.read_bytes()
               for source in sources):
            return candidate
    return None


def _available_run_id(term_directory: Path, status: Mapping[str, Any]) -> str:
    runs = term_directory / "runs"
    requested = (status.get("run_archive") or {}).get("id")
    base = str(requested or snapshot_run_id(str(status.get("started_at") or "")))
    if not (runs / base).exists():
        return base
    index = 2
    while (runs / f"{base}__{index:02d}").exists():
        index += 1
    return f"{base}__{index:02d}"


def _staging_run_directory(runs: Path, run_id: str) -> Path:
    for _ in range(100):
        staging = runs / f".{run_id}.{secrets.token_hex(6)}.tmp"
        try:
            staging.mkdir()
            return staging
        except FileExistsError:
            continue
    raise OSError("snapshot run staging directory collision")


def archive_snapshot_run(
    term_directory: Path,
    status: Mapping[str, Any],
    *,
    reuse_existing: bool = False,
    run_id: str | None = None,
) -> tuple[dict[str, Any], Path, bool]:
    # Windows temp paths can use an 8.3 alias; always return the canonical path.
    term_directory = Path(term_directory).resolve()
    matching = _matching_snapshot_run(term_directory, status) if reuse_existing else None
    if matching is not None:
        committed = copy.deepcopy(dict(status))
        committed["schema_version"] = SCHEMA_VERSION
        committed["run_archive"] = {
            "id": matching.name,
            "path": f"runs/{matching.name}",
            "status": "committed",
            "error_code": None,
        }
        return committed, matching, False

    runs = term_directory / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_id = run_id or _available_run_id(term_directory, status)
    target = runs / run_id
    committed = copy.deepcopy(dict(status))
    committed["schema_version"] = SCHEMA_VERSION
    committed["run_archive"] = {
        "id": run_id,
        "path": f"runs/{run_id}",
        "status": "committed",
        "error_code": None,
    }
    staging = _staging_run_directory(runs, run_id)
    try:
        for source in _snapshot_json_paths(term_directory, status):
            shutil.copy2(source, staging / source.name)
        atomic_write_json(staging / "status.json", committed)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return committed, target, True


def archive_current_snapshot(term_directory: Path) -> Path | None:
    term_directory = Path(term_directory).resolve()
    status_path = term_directory / "status.json"
    if not status_path.exists():
        return None
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HylmsError("snapshot_archive_invalid", "기존 status.json을 보존할 수 없습니다.") from exc
    archive = status.get("run_archive") or {}
    if archive.get("status") == "committed":
        target = (term_directory / str(archive.get("path") or "")).resolve()
        runs = (term_directory / "runs").resolve()
        if target.parent == runs and (target / "status.json").is_file():
            return target
    committed, target, _ = archive_snapshot_run(term_directory, status, reuse_existing=True)
    if committed != status:
        atomic_write_json(status_path, committed)
    return target


def read_previous_success(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        course = payload.get("course") or {}
        return course.get("last_success_at") or payload.get("collected_at")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def read_previous_schema_version(path: Path) -> int | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload.get("schema_version")
        return int(value) if value is not None else None
    except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return None


def find_previous_course(term_directory: Path, course_id: str) -> Path | None:
    matches = sorted(term_directory.glob(f"*__c{course_id}.json")) if term_directory.exists() else []
    return matches[0] if matches else None


def validate_snapshot(data: Mapping[str, Any]) -> None:
    def fail() -> None:
        raise HylmsError(
            "snapshot_integrity_failed",
            "과목 snapshot의 canonical 참조 또는 count가 일치하지 않습니다.",
        )

    section_names = (
        "announcements", "assignment_groups", "assignments", "discussions",
        "quizzes", "weekly_learning", "course_resources",
    )
    sections: dict[str, list[Mapping[str, Any]]] = {}
    ids: dict[str, set[str]] = {}
    for name in section_names:
        values = data.get(name)
        if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
            fail()
        sections[name] = values
        item_ids = [str(item.get("id")) for item in values if item.get("id") is not None]
        if len(item_ids) != len(values) or len(set(item_ids)) != len(item_ids):
            fail()
        ids[name] = set(item_ids)

    canonical = {
        "assignment": ids["assignments"],
        "discussion": ids["discussions"],
        "quiz": ids["quizzes"],
    }
    for group in sections["assignment_groups"]:
        items = group.get("items")
        if not isinstance(items, list):
            fail()
        for item in items:
            if not isinstance(item, dict):
                fail()
            kind = item.get("kind")
            if kind not in canonical or str(item.get("id")) not in canonical[kind]:
                fail()
    for item in sections["weekly_learning"]:
        linked = item.get("linked_entity") or {}
        if not isinstance(linked, dict):
            fail()
        if linked.get("state") == "linked":
            kind = linked.get("kind")
            if kind not in canonical or str(linked.get("id")) not in canonical[kind]:
                fail()

    documents = data.get("documents")
    if not isinstance(documents, dict) or not all(isinstance(item, dict) for item in documents.values()):
        fail()
    document_ids = set(documents)
    source_sections = {
        "announcement": sections["announcements"],
        "assignment": sections["assignments"],
        "weekly_learning": sections["weekly_learning"],
        "course_resource": sections["course_resources"],
    }
    source_records = {
        name: {str(item["id"]): item for item in values}
        for name, values in source_sections.items()
    }
    for source_name, values in source_sections.items():
        for item in values:
            # Older v5 assignment records predate document discovery.
            references = item.get("document_refs", [] if source_name == "assignment" else None)
            if not isinstance(references, list):
                fail()
            if any(str(reference) not in document_ids for reference in references):
                fail()
    saved_paths: list[str] = []
    for document_id, document in documents.items():
        references = document.get("references")
        if str(document.get("id")) != document_id or not isinstance(references, list) or not references:
            fail()
        saved_path = document.get("saved_path")
        if not isinstance(saved_path, str):
            fail()
        saved_paths.append(saved_path)
        for reference in references:
            if not isinstance(reference, dict):
                fail()
            source = reference.get("source")
            record = source_records.get(source, {}).get(str(reference.get("source_id")))
            if record is None or document_id not in (record.get("document_refs") or []):
                fail()
    if len(saved_paths) != len(set(saved_paths)):
        fail()

    sources = data.get("sources")
    if not isinstance(sources, dict):
        fail()

    def source(name: str, *, optional: bool = False) -> Mapping[str, Any]:
        value = sources.get(name)
        if value is None and optional:
            return {"status": "not_collected"}
        if not isinstance(value, dict):
            fail()
        for key, count in value.items():
            if key.endswith("_count") and (type(count) is not int or count < 0):
                fail()
        discovered = value.get("discovered_count")
        normalized = value.get("normalized_count")
        if (
            normalized is not None
            and discovered is not None
            and (type(normalized) is not int or type(discovered) is not int or normalized > discovered)
        ):
            fail()
        return value

    canvas_course = source("canvas_course")
    syllabus_source = source("syllabus")
    announcements_source = source("announcements")
    groups_source = source("assignment_groups")
    assignments_source = source("assignments")
    classic_source = source("classic_quizzes")
    new_source = source("new_quizzes")
    discussion_source = source("canvas_discussions")
    assessment_source = source("canvas_assessments")
    weekly_source = source("weekly_learning")
    resources_source = source("course_resources")
    learningx_source = source("learningx", optional=True)

    syllabus = data.get("syllabus")
    if not isinstance(syllabus, dict):
        fail()
    syllabus_count = int(any(syllabus.get(key) for key in ("text", "links", "images")))
    direct_counts = (
        (canvas_course, 1),
        (syllabus_source, syllabus_count),
        (announcements_source, len(sections["announcements"])),
        (groups_source, len(sections["assignment_groups"])),
        (assignments_source, len(sections["assignments"])),
        (discussion_source, len(sections["discussions"])),
    )
    for item_source, expected in direct_counts:
        if (
            item_source.get("discovered_count") != expected
            or item_source.get("normalized_count") != expected
        ):
            fail()

    quiz_kinds = [item.get("source_kind") for item in sections["quizzes"]]
    if any(kind not in {"classic_quiz", "new_quiz"} for kind in quiz_kinds):
        fail()
    classic_count = quiz_kinds.count("classic_quiz")
    new_count = quiz_kinds.count("new_quiz")
    if (
        classic_source.get("discovered_count") != classic_count
        or classic_source.get("normalized_count") != classic_count
        or new_source.get("discovered_count") != new_count
        or new_source.get("normalized_count") != new_count
        or classic_count + new_count != len(sections["quizzes"])
    ):
        fail()

    discussion_ids = ids["discussions"]
    linked_discussion_ids = {
        str(item.get("id"))
        for group in sections["assignment_groups"]
        for item in group.get("items") or []
        if item.get("kind") == "discussion"
    }
    graded_count = sum(item.get("assignment_id") is not None for item in sections["discussions"])
    linked_count = len(discussion_ids & linked_discussion_ids)
    discussion_restricted = sum(item.get("detail_state") == "restricted" for item in sections["discussions"])
    discussion_failed = sum(item.get("detail_state") == "unavailable" for item in sections["discussions"])
    if (
        discussion_source.get("graded_count") != graded_count
        or discussion_source.get("discovered_count") != len(sections["discussions"])
        or discussion_source.get("linked_count") != linked_count
        or discussion_source.get("restricted_count") != discussion_restricted
        or discussion_source.get("detail_failed_count") != discussion_failed
    ):
        fail()

    assessment_count = len(sections["assignments"]) + len(sections["quizzes"]) + graded_count
    if (
        assessment_source.get("assignment_count") != len(sections["assignments"])
        or assessment_source.get("quiz_count") != len(sections["quizzes"])
        or assessment_source.get("graded_discussion_count") != graded_count
        or assessment_source.get("informational_count")
        != sum(item.get("informational") is True for item in sections["assignments"])
        or assessment_source.get("discovered_count") != assessment_count
        or assessment_source.get("normalized_count") != assessment_count
        or assessment_source.get("restricted_count")
        != int(classic_source.get("restricted_count") or 0) + int(new_source.get("restricted_count") or 0)
        or assessment_source.get("detail_failed_count")
        != int(classic_source.get("detail_failed_count") or 0) + int(new_source.get("detail_failed_count") or 0)
    ):
        fail()

    if weekly_source.get("status") != "not_collected":
        weekly_restricted = sum(item.get("detail_state") == "restricted" for item in sections["weekly_learning"])
        if (
            weekly_source.get("discovered_count") != len(sections["weekly_learning"])
            or weekly_source.get("normalized_count") != len(sections["weekly_learning"])
            or weekly_source.get("restricted_count") != weekly_restricted
        ):
            fail()
    if resources_source.get("status") != "not_collected":
        resource_restricted = sum(item.get("detail_state") == "restricted" for item in sections["course_resources"])
        resource_unknown = sum(item.get("provider_type") == "unknown" for item in sections["course_resources"])
        if (
            resources_source.get("discovered_count") != len(sections["course_resources"])
            or resources_source.get("normalized_count") != len(sections["course_resources"])
            or resources_source.get("restricted_count") != resource_restricted
            or resources_source.get("unknown_type_count") != resource_unknown
        ):
            fail()
    if learningx_source.get("status") != "not_collected":
        weekly_unknown = sum(item.get("kind") == "unknown" for item in sections["weekly_learning"])
        resource_unknown = sum(item.get("provider_type") == "unknown" for item in sections["course_resources"])
        weekly_restricted = sum(item.get("detail_state") == "restricted" for item in sections["weekly_learning"])
        resource_restricted = sum(item.get("detail_state") == "restricted" for item in sections["course_resources"])
        detail_failed = sum(
            item.get("detail_state") == "unavailable"
            for item in [*sections["weekly_learning"], *sections["course_resources"]]
        )
        unresolved = sum(
            (item.get("linked_entity") or {}).get("state") == "unresolved"
            for item in sections["weekly_learning"]
        )
        if (
            learningx_source.get("discovered_count") != len(sections["weekly_learning"])
            or learningx_source.get("normalized_count") != len(sections["weekly_learning"])
            or learningx_source.get("resource_count") != len(sections["course_resources"])
            or learningx_source.get("unknown_type_count") != weekly_unknown + resource_unknown
            or learningx_source.get("restricted_count") != weekly_restricted + resource_restricted
            or learningx_source.get("detail_failed_count") != detail_failed
            or learningx_source.get("unresolved_count") != unresolved
        ):
            fail()

    document_source = sources.get("documents") or {}
    if not isinstance(document_source, dict) or document_source.get("registered_count") != len(documents):
        fail()
    states = [document.get("download_state") for document in documents.values()]
    outcomes = ("downloaded", "existing", "failed", "restricted")
    if states and all(state in outcomes for state in states):
        for state in outcomes:
            if document_source.get(f"{state}_count") != states.count(state):
                fail()


def document_summary(data: Mapping[str, Any]) -> dict[str, Any]:
    source = (data.get("sources") or {}).get("documents") or {}
    return {key: source.get(key) for key in DOCUMENT_SUMMARY_KEYS}


def document_summary_text(summary: Mapping[str, Any]) -> str:
    return (
        f"문서 {summary['completeness']} | 후보 {summary['discovered_count']}, "
        f"적격 {summary['eligible_count']}, 등록 {summary['registered_count']}, "
        f"신규 {summary['downloaded_count']}, 기존 {summary['existing_count']}, "
        f"실패 {summary['failed_count']}, 제한 {summary['restricted_count']}, "
        f"제외 {summary['skipped_extension_count']}"
    )


class SnapshotRunner:
    def __init__(
        self,
        client: CanvasClient,
        *,
        output_root: Path,
        now: dt.datetime,
        clock: Callable[[], dt.datetime] = now_kst,
        out: Callable[[str], None] = print,
        learningx_bootstrap: LearningXBootstrap | None = None,
    ) -> None:
        self.client = client
        self.output_root = output_root
        self.now = now.astimezone(KST)
        self.clock = clock
        self.out = out
        self.learningx_bootstrap = learningx_bootstrap

    def run(self, user_id: str) -> int:
        collector = CanvasCollector(self.client, now=self.now, user_id=user_id)
        courses = collector.list_courses()
        term = select_current_term(courses, self.now)
        term_directory = self.output_root / term.term_id
        started_at = self.now.isoformat(timespec="seconds")
        try:
            archive_current_snapshot(term_directory)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.out("오류[snapshot_archive_failed]: 기존 snapshot을 보존하지 못해 새 수집을 중단했습니다.")
            return 2
        if not term.courses:
            ended_at = self.clock().astimezone(KST).isoformat(timespec="seconds")
            status = {
                "schema_version": SCHEMA_VERSION,
                "term": {"id": term.term_id, "name": term.name, "canvas_id": term.canvas_id},
                "started_at": started_at,
                "ended_at": ended_at,
                "overall_status": "no_courses",
                "exit_code": 0,
                "courses": [],
            }
            status = self._finish_run(term_directory, status)
            self.out(f"[{term.term_id}] 수집 대상 과목이 없습니다.")
            self._summarize(status)
            return int(status["exit_code"])

        learningx_session = None
        learningx_error: BaseException | None = None
        if self.learningx_bootstrap is not None:
            try:
                learningx_session = self.learningx_bootstrap.prepare(term.courses)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                learningx_error = exc
        document_downloader = (
            DocumentDownloader(self.client, learningx_session)
            if learningx_session is not None
            else None
        )

        pending: list[tuple[Mapping[str, Any], dict[str, Any] | None, list[str], BaseException | None]] = []
        for course in term.courses:
            try:
                if learningx_error is not None:
                    raise learningx_error
                data, warnings = collector.collect_course(course, term)
                if self.learningx_bootstrap is not None:
                    if learningx_session is None:
                        raise HylmsError("learningx_session_missing", "LearningX session을 만들지 못했습니다.")
                    warnings.extend(LearningXCollector(learningx_session, now=self.now).enrich(data))
                finalize_document_paths(data)
                validate_snapshot(data)
                pending.append((course, data, warnings, None))
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                pending.append((course, None, [], exc))

        status_courses: list[dict[str, Any]] = []
        successful = 0
        warnings_present = False
        failures = 0
        for course, data, warnings, error in pending:
            course_id = str(course.get("id"))
            name = str(course.get("name") or "course")
            filename = f"{course_stem({'id': course_id, 'name': name})}.json"
            target = term_directory / filename
            previous = find_previous_course(term_directory, course_id)
            if data is not None:
                try:
                    preserve_document_paths(data, previous)
                    if document_downloader is not None:
                        warnings.extend(document_downloader.download(data, term_directory))
                        validate_snapshot(data)
                    atomic_write_json(target, data)
                    successful += 1
                    warning_codes = list(dict.fromkeys(warnings))
                    documents = document_summary(data)
                    failed_detail_count = int(
                        (data.get("sources", {}).get("learningx") or {}).get("detail_failed_count") or 0
                    )
                    state = "updated_with_warnings" if warning_codes else "updated"
                    warnings_present = warnings_present or bool(warning_codes)
                    status_courses.append(
                        {
                            "id": course_id,
                            "name": name,
                            "path": filename,
                            "status": state,
                            "last_success_at": data["course"]["last_success_at"],
                            "snapshot_schema_version": data["schema_version"],
                            "error_code": None,
                            "warning_codes": warning_codes,
                            "failed_detail_count": failed_detail_count,
                            "documents": documents,
                        }
                    )
                    label = "갱신(경고)" if warning_codes else "갱신"
                    self.out(f"[{label}] {name} | {document_summary_text(documents)}")
                    continue
                except OSError as exc:
                    error = exc
            failures += 1
            old_path = previous or (target if target.exists() else None)
            state = "kept" if old_path else "failed"
            last_success = read_previous_success(old_path) if old_path else None
            previous_schema_version = read_previous_schema_version(old_path) if old_path else None
            error_code = stable_error_code(error or RuntimeError())
            status_courses.append(
                {
                    "id": course_id,
                    "name": name,
                    "path": old_path.name if old_path else filename,
                    "status": state,
                    "last_success_at": last_success,
                    "snapshot_schema_version": previous_schema_version,
                    "error_code": error_code,
                    "warning_codes": [],
                    "failed_detail_count": 0,
                    "documents": None,
                }
            )
            label = "기존 파일 유지" if state == "kept" else "실패"
            self.out(f"[{label}] {name} ({error_code})")

        if successful == 0:
            exit_code, overall = 1, "failed"
        elif failures or warnings_present:
            exit_code, overall = 2, "partial_failure"
        else:
            exit_code, overall = 0, "success"
        status = {
            "schema_version": SCHEMA_VERSION,
            "term": {"id": term.term_id, "name": term.name, "canvas_id": term.canvas_id},
            "started_at": started_at,
            "ended_at": self.clock().astimezone(KST).isoformat(timespec="seconds"),
            "overall_status": overall,
            "exit_code": exit_code,
            "courses": status_courses,
        }
        status = self._finish_run(term_directory, status)
        self._summarize(status)
        return int(status["exit_code"])

    def _finish_run(self, term_directory: Path, status: dict[str, Any]) -> dict[str, Any]:
        run_id = snapshot_run_id(str(status.get("started_at") or ""))
        try:
            run_id = _available_run_id(term_directory, status)
            committed, target, created = archive_snapshot_run(term_directory, status, run_id=run_id)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            failed = copy.deepcopy(status)
            failed["schema_version"] = SCHEMA_VERSION
            failed["overall_status"] = "partial_failure"
            failed["exit_code"] = 2
            failed["run_archive"] = {
                "id": run_id,
                "path": f"runs/{run_id}",
                "status": "failed",
                "error_code": "snapshot_archive_failed",
            }
            atomic_write_json(term_directory / "status.json", failed)
            self.out("[보존 실패] snapshot run을 만들지 못했습니다. 다음 실행 전에 다시 보존합니다.")
            return failed
        atomic_write_json(term_directory / "status.json", committed)
        if created:
            self.out(f"[보존] {target.relative_to(term_directory.resolve()).as_posix()}")
        return committed

    def _summarize(self, status: Mapping[str, Any]) -> None:
        self.out(
            f"[완료] {status['overall_status']} | 종료 코드 {status['exit_code']} | "
            f"{status['started_at']} → {status['ended_at']}"
        )
