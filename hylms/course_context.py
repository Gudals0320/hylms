"""Durable personal course context and internal review ledger (not calendars)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager, closing

from .core import HylmsError

DB_NAME = "hylms_context.sqlite3"
POLICY = {
    "materials": "PDF/recordings without deadline or attendance obligations are resources, not user questions. Download PDF using the existing document collector; incomplete progress alone is not an obligation.",
    "unknown": "A new kind or missing technical evidence requires internal classification/review, not an invented task or a question asking the user to perform Schema QA.",
    "precedence": "The user-confirmed timetable is the term baseline. An explicit dated LMS exception applies to that occurrence without replacing the baseline. Conflicts must remain explicit.",
    "timing": "Use the correct course/date/weekday for 'before class'. Class time is not office-hours time. Do not infer semester week dates, attendance, holidays or exceptional lectures from a weekly slot alone.",
    "resolution": "Optional information is not an obligation. Missing facts that must come from the instructor/LMS use pending.context.resolution_owner=source and wait for source updates; only facts or choices genuinely requiring the user use resolution_owner=user and a specific question. Never ask the user to invent unpublished dates.",
}


def technical_pending(item):
    return (item.get("context") or {}).get("kind") in {"qa_pending", "source_removed"}


@contextmanager
def database(root):
    connection = sqlite3.connect(Path(root) / DB_NAME)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS courses (
            term TEXT NOT NULL, course_id TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY(term, course_id));
        CREATE TABLE IF NOT EXISTS reviews (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL);
    """)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def save_courses(root, term, courses):
    with database(root) as connection:
        for course in courses:
            if course["mode"] not in {"in_person", "online_only", "online_with_special_lectures", "reference_only"}:
                raise HylmsError("course_context_invalid", "Invalid delivery mode")
            for slot in course["slots"]:
                if slot["weekday"] not in range(7) or not ("00:00" <= slot["start"] < slot["end"] <= "23:59"):
                    raise HylmsError("course_context_invalid", "Invalid timetable slot")
                dt.time.fromisoformat(slot["start"])
                dt.time.fromisoformat(slot["end"])
            connection.execute("INSERT OR REPLACE INTO courses VALUES(?,?,?)",
                (term, course["course_id"], json.dumps(course, ensure_ascii=False)))


def read_context(root, term):
    path = Path(root) / DB_NAME
    if not path.exists():
        return {"term": term, "timezone": "Asia/Seoul", "courses": [], "policy": POLICY}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            rows = connection.execute("SELECT data FROM courses WHERE term=? ORDER BY course_id", (term,)).fetchall()
        return {"term": term, "timezone": "Asia/Seoul", "courses": [json.loads(r[0]) for r in rows], "policy": POLICY}
    except (sqlite3.Error, ValueError):
        raise HylmsError("course_context_invalid", "Course context database cannot be read") from None


def record_review(root, identity, status, data):
    # The ledger preserves original evidence and outcomes rather than turning
    # machine failures into student-facing questions.
    with database(root) as connection:
        connection.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?)",
                           (identity, status, json.dumps(data, ensure_ascii=False)))


def unresolved_reviews(root):
    path = Path(root) / DB_NAME
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return [{"id": row[0], "status": row[1], "details": json.loads(row[2])}
                for row in connection.execute("SELECT id,status,data FROM reviews WHERE status!='resolved' ORDER BY id")]


def class_slot(context, course_id, date, *, special_lecture=False):
    """Return contextual evidence, never create a lecture or attendance duty."""
    try:
        day = dt.date.fromisoformat(date)
    except (TypeError, ValueError):
        return None
    course = next((c for c in context["courses"] if c["course_id"] == str(course_id) or str(course_id) in c.get("aliases", [])), None)
    if not course or course["mode"] in {"online_only", "reference_only"}:
        return None
    if course["mode"] == "online_with_special_lectures" and not special_lecture:
        return None
    slots = [s for s in course["slots"] if s["weekday"] == day.weekday()]
    if len(slots) != 1:
        return None
    return {**slots[0], "date": date, "timezone": "Asia/Seoul", "basis": "user_confirmed_timetable"}


def classify_record(record):
    section = record.get("section")
    fields = record.get("structured", {}).get("compare", {})
    kind = fields.get("kind")
    boundaries = (fields.get("schedule") or {}).get("effective") or {}
    attendance = fields.get("attendance") or {}
    due = any((boundaries.get(k) or {}).get("value") is not None for k in ("due_at", "closes_at", "late_until_at"))
    if section in {"assignment", "quiz", "discussion"}:
        return "assess_obligation"
    if due or attendance.get("targeted") is True:
        return "manage_obligation"
    if section == "weekly_learning" and kind in {"pdf", "conference", "video", "file", "page", "embed", "link", "text", "resource"}:
        no_deadline = all((boundaries.get(k) or {}).get("state") in {"unbounded", "not_applicable"}
                          for k in ("due_at", "closes_at"))
        if no_deadline and attendance.get("targeted") is False:
            return "resource_only"
    return "internal_review"
