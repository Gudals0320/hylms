from __future__ import annotations

import copy
import datetime as dt
import http.cookiejar
import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import hylms_snapshot as hs
import hylms.documents as document_module
import hylms.storage as storage_module


NOW = dt.datetime(2026, 9, 2, 12, 0, tzinfo=hs.KST)


class QueueTransport:
    def __init__(self, values):
        self.values = list(values)
        self.requests = []

    def request(self, method, url, headers, timeout):
        self.requests.append((method, url, dict(headers), timeout))
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeDownloadResponse:
    def __init__(self, body=b"", *, status=200, url="https://files.example.test/file", headers=None):
        self.body = io.BytesIO(body)
        self.status = status
        self.url = url
        self.headers = headers or {}

    def read(self, size=-1):
        return self.body.read(size)

    def geturl(self):
        return self.url

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class FakeDownloadOpener:
    def __init__(self, callback):
        self.callback = callback
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request.full_url, dict(request.header_items()), timeout))
        return self.callback(request)


class FakeCanvasDownloadClient:
    token = "canvas-secret"
    origin = hs.CANVAS_ORIGIN

    def __init__(self, *, metadata=None, error=None):
        self.metadata = metadata
        self.error = error
        self.calls = []

    def get_json(self, path, params=None):
        self.calls.append((path, params))
        if self.error is not None:
            raise self.error
        file_id = path.rsplit("/", 1)[-1]
        return self.metadata or {
            "url": f"{self.origin}/files/{file_id}/download?download_frd=1&verifier=canvas-verifier"
        }


class NoopDocumentDownloader:
    def __init__(self, canvas, learningx):
        pass

    def download(self, data, term_directory):
        return []


class FakeStore:
    def __init__(self, record=None):
        self.record = record
        self.self_tests = 0
        self.writes = []
        self.deleted = False
        self.read_override = None
        self.fail_self_test = False
        self.fail_write = False

    def read(self):
        if self.read_override is not None:
            value = self.read_override
            self.read_override = None
            return value
        return self.record

    def write(self, record):
        if self.fail_write:
            raise hs.CredentialStoreError("credential_write_failed", "write failed")
        self.record = record
        self.writes.append(record)

    def delete(self):
        self.record = None
        self.deleted = True

    def self_test(self):
        self.self_tests += 1
        if self.fail_self_test:
            raise hs.CredentialStoreError("credential_self_test_failed", "self test failed")


class AuthOnlyClient:
    def __init__(self, token, *, invalid=False, revoke_fails=False, revoke_log=None, no_courses=False):
        self.token = token
        self.invalid = invalid
        self.revoke_fails = revoke_fails
        self.revoke_log = revoke_log if revoke_log is not None else []
        self.no_courses = no_courses

    def validate_user(self):
        if self.invalid:
            raise hs.CanvasHTTPError(401, "/api/v1/users/self")
        return {"id": 999, "name": "must-not-be-saved", "email": "secret@example.com"}

    def revoke_self(self):
        self.revoke_log.append(self.token)
        if self.revoke_fails:
            raise hs.CanvasHTTPError(401, "/login/oauth2/token")

    def get_paginated(self, path, params=None):
        if path == "/api/v1/courses" and self.no_courses:
            return []
        raise AssertionError(path)


class FixtureCanvasClient:
    def __init__(self):
        self.calls = []

    def validate_user(self):
        return {"id": 999, "name": "Private User", "email": "private@example.com"}

    def get_paginated(self, path, params=None):
        self.calls.append(("list", path, params))
        if path == "/api/v1/courses":
            return [
                {
                    "id": 101,
                    "name": "자료구조: 실습",
                    "course_code": "CSE101",
                    "concluded": False,
                    "syllabus_body": '<p>강의계획 <a href="/courses/101">보기</a></p>',
                    "term": {"id": 500, "name": "2026년 2학기", "start_at": None, "end_at": None},
                },
                {
                    "id": 1,
                    "name": "old",
                    "term": {"id": 100, "name": "2022년 겨울학기", "start_at": None, "end_at": None},
                },
                {"id": 2, "name": "mooc", "term": {"id": 200, "name": "HY-MOOC"}},
            ]
        if path == "/api/v1/announcements":
            return [
                {
                    "id": 300,
                    "title": "첫 공지",
                    "posted_at": "2026-09-01T00:00:00Z",
                    "html_url": "https://learning.hanyang.ac.kr/courses/101/discussion_topics/300?user_id=999",
                    "message": (
                        '<p>자료 <a href="/courses/101/files/700/download?wrap=1&verifier=secret">받기</a>'
                        '<a href="/courses/101/files/700/download?wrap=1">다시</a></p>'
                    ),
                    "attachments": [{"id": 700, "display_name": "강의자료.docx"}],
                }
            ]
        if path == "/api/v1/courses/101/discussion_topics":
            return []
        if path == "/api/v1/courses/101/assignment_groups":
            base_dates = [
                {
                    "base": True,
                    "unlock_at": "2026-09-01T00:00:00Z",
                    "due_at": "2026-09-10T00:00:00Z",
                    "lock_at": "2026-09-12T00:00:00Z",
                }
            ]
            return [
                {
                    "id": 400,
                    "name": "평가",
                    "position": 1,
                    "group_weight": 50,
                    "rules": {"drop_lowest": 1, "never_drop": [501]},
                    "assignments": [
                        {
                            "id": 501,
                            "name": "과제 1",
                            "assignment_group_id": 400,
                            "position": 1,
                            "points_possible": 10,
                            "submission_types": ["online_text_entry"],
                            "description": "<p>답을 제출하세요.</p>",
                            "html_url": "https://learning.hanyang.ac.kr/courses/101/assignments/501",
                            "unlock_at": "2026-09-01T00:00:00Z",
                            "due_at": "2026-09-10T00:00:00Z",
                            "lock_at": "2026-09-12T00:00:00Z",
                            "all_dates": base_dates,
                        },
                        {
                            "id": 502,
                            "name": "퀴즈 1",
                            "assignment_group_id": 400,
                            "position": 2,
                            "quiz_id": 601,
                            "unlock_at": "2026-09-01T00:00:00Z",
                            "due_at": "2026-09-10T00:00:00Z",
                            "lock_at": "2026-09-12T00:00:00Z",
                            "all_dates": base_dates,
                        },
                        {
                            "id": 503,
                            "name": "New Quiz",
                            "assignment_group_id": 400,
                            "position": 3,
                            "is_quiz_assignment": True,
                            "html_url": "https://learning.hanyang.ac.kr/courses/101/assignments/503",
                            "unlock_at": None,
                            "due_at": None,
                            "lock_at": None,
                            "all_dates": [{"base": True, "unlock_at": None, "due_at": None, "lock_at": None}],
                        },
                    ],
                },
                {
                    "id": 401,
                    "name": "빈 SIS 그룹",
                    "position": 2,
                    "group_weight": 0,
                    "sis_source_id": "sis-401",
                    "assignments": [],
                },
            ]
        if path == "/api/v1/courses/101/quizzes":
            return [
                {
                    "id": 601,
                    "assignment_id": 502,
                    "title": "퀴즈 1",
                    "position": 2,
                    "description": "<p>선택하세요.</p>",
                    "html_url": "https://learning.hanyang.ac.kr/courses/101/quizzes/601",
                    "unlock_at": "2026-09-01T00:00:00Z",
                    "due_at": "2026-09-10T00:00:00Z",
                    "lock_at": "2026-09-12T00:00:00Z",
                }
            ]
        if path == "/api/v1/courses/101/quizzes/601/questions":
            return [
                {
                    "id": 801,
                    "position": 1,
                    "question_type": "multiple_choice_question",
                    "question_text": "<p>2+2?</p>",
                    "points_possible": 1,
                    "answers": [
                        {"id": 1, "text": "3", "correct": False},
                        {"id": 2, "text": "4", "correct": True},
                    ],
                }
            ]
        if path == "/api/quiz/v1/courses/101/quizzes":
            raise hs.CanvasHTTPError(403, path)
        raise AssertionError(path)

    def get_json(self, path, params=None):
        self.calls.append(("get", path, params))
        if path == "/api/v1/files/700":
            return {
                "id": 700,
                "display_name": "강의자료.docx",
                "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size": 1234,
                "url": "https://storage.example/file?Signature=secret",
            }
        if path == "/api/v1/courses/101/assignments/501/submissions/self":
            return {
                "workflow_state": "graded",
                "attempt": 1,
                "submitted_at": "2026-09-02T00:00:00Z",
                "graded_at": "2026-09-02T01:00:00Z",
                "score": 9,
                "grade": "9",
                "late": False,
                "missing": False,
                "body": "<p>내 답안</p>",
                "submission_comments": [
                    {"id": 900, "author_id": 888, "author_name": "담당 교수", "created_at": "2026-09-02T01:00:00Z", "comment": "<p>좋습니다.</p>"}
                ],
                "submission_history": [{"attempt": 1, "workflow_state": "graded", "score": 9, "late": False}],
                "rubric_assessment": {"criterion-1": {"points": 4, "rating_id": 5, "comments": "좋음", "assessor_id": 888}},
            }
        if path == "/api/v1/courses/101/quizzes/601/submission":
            return {
                "quiz_submissions": [
                    {
                        "id": 701,
                        "attempt": 1,
                        "workflow_state": "complete",
                        "finished_at": "2026-09-02T02:00:00Z",
                        "score": 1,
                        "kept_score": 1,
                        "late": False,
                        "user_id": 999,
                    }
                ]
            }
        if path == "/api/v1/courses/101/quizzes/601":
            return {
                "id": 601,
                "assignment_id": 502,
                "assignment_group_id": 400,
                "title": "퀴즈 1",
                "quiz_type": "assignment",
                "points_possible": 1,
                "question_count": 1,
                "time_limit": 10,
                "allowed_attempts": 2,
                "scoring_policy": "keep_highest",
                "shuffle_answers": True,
                "one_question_at_a_time": True,
                "cant_go_back": True,
                "hide_results": None,
                "show_correct_answers": True,
                "one_time_results": False,
                "access_code": "must-not-leak",
                "ip_filter": "10.0.0.0/8",
                "require_lockdown_browser": False,
                "unlock_at": "2026-09-01T00:00:00Z",
                "due_at": "2026-09-10T00:00:00Z",
                "lock_at": "2026-09-12T00:00:00Z",
            }
        if path == "/api/v1/quiz_submissions/701/questions":
            return {"quiz_submission_questions": [{"id": 801, "correct": True}]}
        raise AssertionError(path)


class BuildTwoDiscussionClient(FixtureCanvasClient):
    def get_paginated(self, path, params=None):
        if path == "/api/v1/courses/101/discussion_topics":
            return [
                {
                    "id": 900,
                    "title": "독립 토론",
                    "message": "<p>목록 요약</p>",
                    "html_url": "https://learning.hanyang.ac.kr/courses/101/discussion_topics/900",
                    "discussion_type": "threaded",
                    "posted_at": "2026-09-01T00:00:00Z",
                    "lock_at": "2026-09-30T00:00:00Z",
                    "published": True,
                    "discussion_subentry_count": 5,
                    "unread_count": 2,
                    "read_state": "unread",
                    "subscribed": True,
                },
                {
                    "id": 901,
                    "assignment_id": 504,
                    "title": "채점 토론",
                    "message": "<p>채점 토론 prompt</p>",
                    "html_url": "https://learning.hanyang.ac.kr/courses/101/discussion_topics/901",
                    "discussion_type": "side_comment",
                    "published": True,
                    "discussion_subentry_count": 0,
                },
            ]
        if path == "/api/v1/courses/101/assignment_groups":
            groups = copy.deepcopy(super().get_paginated(path, params))
            groups[0]["assignments"].append(
                {
                    "id": 504,
                    "name": "채점 토론",
                    "assignment_group_id": 400,
                    "position": 4,
                    "points_possible": 5,
                    "submission_types": ["discussion_topic"],
                    "discussion_topic": {"id": 901},
                    "description": "<p>토론 설명 <a href='/courses/101/files/999/download'>PDF</a></p>",
                    "html_url": "https://learning.hanyang.ac.kr/courses/101/assignments/504",
                    "unlock_at": "2026-09-01T00:00:00Z",
                    "due_at": "2026-09-20T00:00:00Z",
                    "lock_at": "2026-09-30T00:00:00Z",
                    "all_dates": [
                        {
                            "base": True,
                            "unlock_at": "2026-09-01T00:00:00Z",
                            "due_at": "2026-09-20T00:00:00Z",
                            "lock_at": "2026-09-30T00:00:00Z",
                        }
                    ],
                }
            )
            return groups
        return super().get_paginated(path, params)

    def get_json(self, path, params=None):
        if path == "/api/v1/courses/101/assignments/504/submissions/self":
            return {
                "workflow_state": "submitted",
                "submitted_at": "2026-09-10T00:00:00Z",
                "late": False,
                "missing": False,
                "score": 5,
            }
        if path == "/api/v1/courses/101/discussion_topics/900":
            return {
                "id": 900,
                "title": "독립 토론",
                "message": "<p>전체 prompt <a href='/courses/101/files/998/download'>자료</a></p>",
                "html_url": "https://learning.hanyang.ac.kr/courses/101/discussion_topics/900",
                "discussion_type": "threaded",
                "posted_at": "2026-09-01T00:00:00Z",
                "lock_at": "2026-09-30T00:00:00Z",
                "published": True,
                "discussion_subentry_count": 5,
                "unread_count": 2,
                "read_state": "unread",
                "subscribed": True,
                "sections": [{"id": 71, "name": "01분반"}],
                "attachments": [{"id": 998, "display_name": "토론.pdf", "url": "/files/998/download"}],
            }
        if path == "/api/v1/courses/101/discussion_topics/901":
            return {
                "id": 901,
                "assignment_id": 504,
                "title": "채점 토론",
                "message": "<p>채점 토론 prompt</p>",
                "html_url": "https://learning.hanyang.ac.kr/courses/101/discussion_topics/901",
                "discussion_type": "side_comment",
                "published": True,
                "discussion_subentry_count": 0,
            }
        if path == "/api/v1/courses/101/discussion_topics/900/view":
            return {
                "participants": [
                    {"id": 200, "display_name": "Peer Root", "avatar_url": "private-avatar"},
                    {"id": 201, "display_name": "Instructor", "avatar_url": "private-avatar"},
                    {"id": 202, "display_name": "Nested Peer", "avatar_url": "private-avatar"},
                    {"id": 999, "display_name": "Self Name", "avatar_url": "private-avatar"},
                ],
                "unread_entries": [1002],
                "view": [
                    {
                        "id": 1000,
                        "user_id": 200,
                        "message": "peer-root-private",
                        "created_at": "2026-09-01T00:00:00Z",
                        "replies": [
                            {
                                "id": 1001,
                                "user_id": 999,
                                "parent_id": 1000,
                                "message": "<p>my-reply</p>",
                                "created_at": "2026-09-02T00:00:00Z",
                                "replies": [
                                    {
                                        "id": 1002,
                                        "user_id": 201,
                                        "parent_id": 1001,
                                        "message": "<p>direct-feedback</p>",
                                        "created_at": "2026-09-03T00:00:00Z",
                                        "replies": [
                                            {
                                                "id": 1003,
                                                "user_id": 202,
                                                "parent_id": 1002,
                                                "message": "nested-peer-private",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "id": 1004,
                        "user_id": 999,
                        "message": "<p>my-top-level</p>",
                        "created_at": "2026-09-04T00:00:00Z",
                    },
                ],
                "new_entries": [
                    {"id": 1004, "user_id": 999, "message": "duplicate-self"},
                    {
                        "id": 1005,
                        "user_id": 201,
                        "parent_id": 1004,
                        "message": "new-direct-feedback",
                        "created_at": "2026-09-05T00:00:00Z",
                    },
                ],
            }
        return super().get_json(path, params)


class FakeLearningXSession:
    token = "xn-secret-must-not-leak"
    user_id = "private-user-id"
    user_login = "private-login"
    role = "student"
    request_type = "module"

    def __init__(self, *, fail_detail=False, invalid_bulk=False, unresolved=False, unknown=False,
                 locked=False, unknown_resource=False):
        self.calls = []
        self.fail_detail = fail_detail
        self.invalid_bulk = invalid_bulk
        self.unresolved = unresolved
        self.unknown = unknown
        self.locked = locked
        self.unknown_resource = unknown_resource

    def get_json(self, path, params=None):
        self.calls.append((path, params))
        if path.endswith("/sections_db"):
            if self.invalid_bulk:
                return {"unexpected": "shape"}
            return {"sections": []}
        if path.endswith("/modules"):
            if self.locked:
                return {
                    "modules": [{
                        "id": "week-2",
                        "position": 2,
                        "section_title": "2주차",
                        "module_items": [
                            {
                                "module_item_id": f"locked-{index}",
                                "content_id": "not_open",
                                "content_type": "attendance_item",
                                "title": f"잠긴 {'PDF' if subtype == 'pdf' else '영상'} {index}",
                                "url": f"/courses/101/modules/items/locked-{index}",
                                "content_data": {
                                    "item_content_type": "commons",
                                    "week_position": 2,
                                    "unlock_at": "2026-09-10T00:00:00Z",
                                    "item_content_data": {
                                        "content_id": "not_open",
                                        "content_type": subtype,
                                        "duration": 60 * index,
                                        "file_name": "잠긴.pdf" if subtype == "pdf" else None,
                                    },
                                },
                            }
                            for index, subtype in ((1, "mp4"), (2, "pdf"))
                        ],
                    }]
                }
            module_items = [
                {
                    "module_item_id": "canvas-mi-1",
                    "content_id": "lx-1",
                    "content_type": "attendance_item",
                    "attendance_status": "ATTENDANCE",
                    "title": "영상",
                    "url": "/courses/101/modules/items/canvas-mi-1",
                    "content_data": {
                        "item_content_type": "commons",
                        "week_position": 1,
                        "item_content_data": {"content_type": "mp4", "title": "덮어쓰면 안 됨"},
                    },
                },
                {
                    "module_item_id": "canvas-mi-2",
                    "content_id": "501",
                    "content_type": "assignment",
                    "title": "연결 과제",
                },
                {
                    "module_item_id": "canvas-mi-3",
                    "content_id": "601",
                    "content_type": "quiz",
                    "title": "연결 퀴즈",
                },
                {
                    "module_item_id": "canvas-mi-4",
                    "content_id": "text-1",
                    "content_type": "module_builder_text",
                    "title": "안내문",
                },
                {
                    "module_item_id": "canvas-mi-5",
                    "content_id": "lx-5",
                    "content_type": "attendance_item",
                    "title": "PDF",
                    "content_data": {
                        "item_content_type": "commons",
                        "item_content_data": {
                            "content_id": "hc-55",
                            "content_type": "pdf",
                            "file_name": "주차자료.pdf",
                        },
                    },
                },
            ]
            if self.unresolved:
                module_items.append({
                    "module_item_id": "canvas-mi-6",
                    "content_id": "missing",
                    "content_type": "discussion",
                    "title": "연결 실패",
                })
            if self.unknown:
                module_items.append({
                    "module_item_id": "canvas-mi-unknown",
                    "content_id": "unknown",
                    "content_type": "mystery",
                    "title": "알 수 없음",
                })
            return {
                "modules": [
                    {
                        "id": "week-1",
                        "position": 1,
                        "section_title": "1주차",
                        "module_items": module_items,
                    }
                ]
            }
        if path.endswith("/allcomponents_db"):
            if self.locked:
                return {"components": []}
            values = [
                {"id": "501", "component_type": "assignment", "title": "연결 과제", "assignment_id": "501"},
                {"id": "601", "component_type": "quiz", "title": "연결 퀴즈", "quiz_id": "601"},
                {"id": "lx-5", "component_type": "commons/pdf", "title": "PDF", "content_id": "hc-55", "filename": "주차자료.pdf"},
                {"id": "unplaced", "component_type": "assignment", "title": "배치되지 않은 항목", "assignment_id": "999"},
            ]
            return {"components": values}
        if "/attendance_items/" in path:
            component_id = path.rsplit("/", 1)[-1]
            if self.fail_detail and component_id == "lx-5":
                raise hs.LearningXHTTPError(500, path)
            common = {
                "unlock_at": "2026-09-01T00:00:00Z",
                "due_at": "2026-09-10T00:00:00Z",
                "late_at": "2026-09-12T00:00:00Z",
                "lock_at": "2026-09-15T00:00:00Z",
                "completed": component_id == "lx-1",
                "use_attendance": component_id == "lx-1",
                "attendance_status": "ATTENDANCE" if component_id == "lx-1" else "NONE",
            }
            return common
        if path.endswith("/resources"):
            return {"resources": [{"resource_id": "r-1", "title": "강의자료", "submitted": False}]}
        if path.endswith("/resources/r-1"):
            return {
                "id": "r-1",
                "title": "강의자료",
                "commons_content": {
                    "content_id": "hc-55",
                    "content_type": "99" if self.unknown_resource else "10",
                    "file_name": "주차자료.pdf",
                    "size": 123,
                    "view_url": "https://learning.hanyang.ac.kr/viewer?token=must-not-leak",
                },
            }
        raise AssertionError(path)


class HelpersTest(unittest.TestCase):
    def test_credential_record_round_trip(self):
        record = hs.CredentialRecord("123~secret", "123", "2026-12-01T23:59:59+09:00")
        self.assertEqual(record, hs.CredentialRecord.from_bytes(record.to_bytes()))
        self.assertNotIn(b" ", record.to_bytes())

    def test_invalid_credential_expiry_is_wrapped(self):
        value = json.dumps({"version": 1, "token": "secret", "token_id": None, "expires_at": "bad"}).encode()
        with self.assertRaises(hs.CredentialStoreError) as caught:
            hs.CredentialRecord.from_bytes(value)
        self.assertEqual("credential_invalid", caught.exception.code)

    def test_collection_envelope_and_legacy_list_are_supported(self):
        values = [{"id": 1}]
        self.assertEqual(
            values,
            hs.unwrap_collection(
                {"quiz_submission_questions": values},
                "quiz_submission_questions",
                error_code="invalid",
                label="quiz",
            ),
        )
        self.assertEqual(
            values,
            hs.unwrap_collection(values, "quiz_submission_questions", error_code="invalid", label="quiz"),
        )
        with self.assertRaises(hs.HylmsError) as caught:
            hs.unwrap_collection({}, "quiz_submission_questions", error_code="invalid", label="quiz")
        self.assertEqual("invalid", caught.exception.code)

    def test_extract_token_id(self):
        self.assertEqual("123", hs.extract_token_id("123~secret"))
        self.assertIsNone(hs.extract_token_id("opaque-secret"))

    def test_clean_html_and_sensitive_query_sanitizer(self):
        content, raw_links = hs.clean_html(
            '<p>Hello <a href="/go?v=1&token=secret&user_id=9">world</a></p>'
            '<script>steal()</script><img src="/img.png?Signature=nope&size=2" alt="x">'
        )
        self.assertEqual("Hello world", content["text"])
        self.assertEqual(["/go?v=1&token=secret&user_id=9"], raw_links)
        self.assertEqual("https://learning.hanyang.ac.kr/go?v=1", content["links"][0]["url"])
        self.assertEqual("https://learning.hanyang.ac.kr/img.png?size=2", content["images"][0]["url"])
        self.assertNotIn("steal", content["text"])
        self.assertEqual(
            "https://example.test/path?video=1",
            hs.sanitize_url("https://example.test/path?video=1&user_login=secret&upload_token=secret"),
        )

    def test_filename_safety(self):
        self.assertEqual("CON_", hs.sanitize_filename_component("CON"))
        self.assertEqual("a_b_c", hs.sanitize_filename_component('a<b>c. '))
        self.assertEqual(100, len(hs.sanitize_filename_component("x" * 200)))

    def test_atomic_json_replaces_with_utf8_and_no_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            hs.atomic_write_json(path, {"text": "한글", "value": 1})
            hs.atomic_write_json(path, {"text": "새 값", "value": 2})
            self.assertEqual("새 값", json.loads(path.read_text(encoding="utf-8"))["text"])
            self.assertEqual([], list(Path(directory).glob("*.tmp")))
            self.assertTrue(path.read_bytes().endswith(b"\n"))


class TermSelectionTest(unittest.TestCase):
    def test_name_fallback_selects_current_and_filters_other_terms(self):
        courses = FixtureCanvasClient().get_paginated("/api/v1/courses")
        selection = hs.select_current_term(courses, NOW)
        self.assertEqual("26-2", selection.term_id)
        self.assertEqual([101], [course["id"] for course in selection.courses])

    def test_unique_expected_name_wins_over_unreliable_other_term_dates(self):
        courses = [
            {"id": 1, "term": {"id": 1, "name": "2026년 1학기", "start_at": "2026-01-01T00:00:00Z", "end_at": "2026-12-31T00:00:00Z"}},
            {"id": 2, "term": {"id": 2, "name": "2026년 2학기", "start_at": None, "end_at": None}},
        ]
        self.assertEqual("2026년 2학기", hs.select_current_term(courses, NOW).name)

    def test_reported_open_ended_old_and_current_terms_selects_current_name(self):
        courses = [
            {"id": 1, "term": {"id": 1, "name": "2022년 겨울학기", "start_at": "2022-12-01T00:00:00Z", "end_at": None}},
            {"id": 2, "term": {"id": 2, "name": "2026년 2학기", "start_at": "2026-09-01T00:00:00Z", "end_at": None}},
        ]
        selection = hs.select_current_term(courses, NOW)
        self.assertEqual("2026년 2학기", selection.name)
        self.assertEqual([2], [course["id"] for course in selection.courses])

    def test_ended_dated_term_does_not_fall_back_by_name(self):
        courses = [
            {"id": 1, "term": {"id": 1, "name": "2026년 2학기", "start_at": "2026-01-01T00:00:00Z", "end_at": "2026-08-01T00:00:00Z"}},
        ]
        with self.assertRaises(hs.TermSelectionError) as caught:
            hs.select_current_term(courses, NOW)
        self.assertEqual("term_not_found", caught.exception.code)

    def test_winter_uses_previous_year(self):
        name, term_id = hs.expected_term(dt.datetime(2027, 1, 10, tzinfo=hs.KST))
        self.assertEqual(("2026년 겨울학기", "26-W"), (name, term_id))

    def test_unknown_term_fails(self):
        with self.assertRaises(hs.TermSelectionError) as caught:
            hs.select_current_term([{"id": 1, "term": {"name": "특별학기"}}], NOW)
        self.assertEqual("term_unknown", caught.exception.code)

    def test_multiple_expected_terms_fail(self):
        courses = [
            {"id": 1, "term": {"id": 1, "name": "2026년 2학기"}},
            {"id": 2, "term": {"id": 2, "name": "2026년 2학기"}},
        ]
        with self.assertRaises(hs.TermSelectionError) as caught:
            hs.select_current_term(courses, NOW)
        self.assertEqual("term_ambiguous", caught.exception.code)

    def test_only_hymooc_is_no_courses(self):
        selection = hs.select_current_term([{"id": 1, "term": {"name": "HY-MOOC"}}], NOW)
        self.assertEqual([], selection.courses)
        self.assertEqual("26-2", selection.term_id)


class ScheduleTest(unittest.TestCase):
    def window(self, opens=None, due=None, lock=None, all_dates=None):
        source = {}
        if opens != "absent":
            source["unlock_at"] = opens
        if due != "absent":
            source["due_at"] = due
        if lock != "absent":
            source["lock_at"] = lock
        return hs.normalize_schedule(source, all_dates, "99")

    def test_course_default_and_same_as_close(self):
        raw = {"unlock_at": "2026-09-01T00:00:00Z", "due_at": "2026-09-02T00:00:00Z", "lock_at": "2026-09-03T00:00:00Z"}
        schedule = hs.normalize_schedule(raw, [{"base": True, **raw}], "99")
        self.assertEqual("course_default", schedule["basis"])
        self.assertEqual("same_as_closes_at", schedule["effective"]["late_until_at"]["state"])

    def test_user_and_section_overrides(self):
        base = {"base": True, "unlock_at": None, "due_at": "2026-09-01T00:00:00Z", "lock_at": None}
        user = {"student_ids": [99], "unlock_at": None, "due_at": "2026-09-03T00:00:00Z", "lock_at": None}
        section = {"course_section_id": 8, "unlock_at": None, "due_at": "2026-09-04T00:00:00Z", "lock_at": None}
        self.assertEqual("user_override", hs.normalize_schedule(user, [base, user], "99")["basis"])
        self.assertEqual("section_override", hs.normalize_schedule(section, [base, section], "99")["basis"])
        same_as_base_user = {**base, "base": False, "student_ids": [99]}
        self.assertEqual(
            "user_override", hs.normalize_schedule(base, [base, same_as_base_user], "99")["basis"]
        )

    def test_boundary_states(self):
        schedule = self.window(opens="absent", due=None, lock="not-a-date")
        self.assertEqual("provider_omitted", schedule["effective"]["opens_at"]["state"])
        self.assertEqual("unbounded", schedule["effective"]["due_at"]["state"])
        self.assertEqual("unknown", schedule["effective"]["closes_at"]["state"])
        restricted = hs.normalize_schedule({}, None, None, restricted=True)
        self.assertEqual("restricted", restricted["effective"]["due_at"]["state"])

    def test_all_summary_states_without_inferring_attendance(self):
        def summary(opens, due, lock, submission=None, at=NOW):
            schedule = self.window(opens=opens, due=due, lock=lock)
            return hs.evaluate_summary(schedule, submission, at)["state"]

        self.assertEqual("not_open", summary("2026-09-03T00:00:00+09:00", "2026-09-04T00:00:00+09:00", "2026-09-05T00:00:00+09:00"))
        self.assertEqual("open_on_time", summary("2026-09-01T00:00:00+09:00", "2026-09-03T00:00:00+09:00", "2026-09-05T00:00:00+09:00"))
        self.assertEqual("open_late", summary("2026-09-01T00:00:00+09:00", "2026-09-02T00:00:00+09:00", "2026-09-03T00:00:00+09:00"))
        open_after_schedule = self.window(
            opens="2026-09-01T00:00:00+09:00",
            due="2026-09-02T00:00:00+09:00",
            lock="2026-09-10T00:00:00+09:00",
        )
        open_after_schedule["effective"]["late_until_at"] = {
            "value": "2026-09-03T00:00:00+09:00",
            "state": "known",
        }
        self.assertEqual(
            "open_after_due",
            hs.evaluate_summary(open_after_schedule, None, dt.datetime(2026, 9, 5, tzinfo=hs.KST))["state"],
        )
        self.assertEqual("closed_unconfirmed", summary("2026-08-01T00:00:00+09:00", "2026-08-02T00:00:00+09:00", "2026-08-03T00:00:00+09:00"))
        self.assertEqual("closed_absent", summary("2026-08-01T00:00:00+09:00", "2026-08-02T00:00:00+09:00", "2026-08-03T00:00:00+09:00", {"missing": True}))
        self.assertEqual("present", summary(None, None, None, {"workflow_state": "complete", "finished_at": "x", "late": False}))
        self.assertEqual("late", summary(None, None, None, {"workflow_state": "complete", "finished_at": "x", "late": True}))
        self.assertEqual("unknown", summary("absent", "absent", "absent"))


class BuildTwoNormalizationTest(unittest.TestCase):
    def test_all_classic_quiz_types_and_visibility_settings(self):
        for quiz_type in ("practice_quiz", "assignment", "graded_survey", "survey"):
            with self.subTest(quiz_type=quiz_type):
                settings = hs.normalize_classic_quiz_settings(
                    {
                        "quiz_type": quiz_type,
                        "allowed_attempts": -1,
                        "scoring_policy": "keep_latest",
                        "hide_results": "until_after_last_attempt",
                        "show_correct_answers": True,
                        "show_correct_answers_at": "2026-09-01T00:00:00Z",
                        "access_code": "must-not-leak",
                        "ip_filter": "10.0.0.0/8",
                        "require_lockdown_browser": True,
                    }
                )
                self.assertEqual(quiz_type, settings["quiz_type"])
                self.assertTrue(settings["attempts"]["unlimited"])
                self.assertEqual("after_last_attempt", settings["result_visibility"]["state"])
                self.assertEqual("scheduled", settings["result_visibility"]["correct_answers_state"])
                self.assertTrue(settings["access_requirements"]["access_code"]["required"])
                encoded = json.dumps(settings)
                self.assertNotIn("must-not-leak", encoded)
                self.assertNotIn("10.0.0.0", encoded)

    def test_score_states_do_not_infer_hidden_or_pending_scores(self):
        self.assertEqual("not_applicable", hs.quiz_score_state(None, "visible"))
        self.assertEqual("known", hs.quiz_score_state({"score": 3}, "hidden"))
        self.assertEqual("pending", hs.quiz_score_state({"workflow_state": "pending_review"}, "visible"))
        self.assertEqual("hidden", hs.quiz_score_state({"workflow_state": "complete"}, "hidden"))
        self.assertEqual("provider_omitted", hs.quiz_score_state({"workflow_state": "complete"}, "visible"))

    def test_new_quiz_settings_drop_access_code_and_ip_ranges(self):
        settings = hs.normalize_new_quiz_settings(
            {
                "points_possible": 20,
                "quiz_settings": {
                    "require_student_access_code": True,
                    "student_access_code": "must-not-leak",
                    "filter_ip_address": True,
                    "filters": {"ips": [["10.0.0.1", "10.0.0.9"]]},
                    "has_time_limit": True,
                    "session_time_limit_in_seconds": 600,
                    "multiple_attempts": {
                        "multiple_attempts_enabled": True,
                        "attempt_limit": False,
                        "max_attempts": None,
                        "score_to_keep": "highest",
                    },
                    "result_view_settings": {
                        "display_items": True,
                        "display_item_response_correctness": False,
                    },
                },
            }
        )
        self.assertTrue(settings["access_requirements"]["access_code"]["required"])
        self.assertTrue(settings["access_requirements"]["ip_filter"]["required"])
        self.assertTrue(settings["attempts"]["unlimited"])
        encoded = json.dumps(settings)
        self.assertNotIn("must-not-leak", encoded)
        self.assertNotIn("10.0.0", encoded)

    def test_information_assignment_requires_all_three_provider_facts(self):
        collector = hs.CanvasCollector(FixtureCanvasClient(), now=NOW, user_id="999")
        raw = {
            "id": 1,
            "name": "정보",
            "points_possible": 0,
            "omit_from_final_grade": True,
            "submission_types": [],
        }
        informational = collector._normalize_assignment(raw, None, False, hs.CANVAS_ORIGIN)
        self.assertTrue(informational["informational"])
        self.assertFalse(informational["submission_required"])
        normal = collector._normalize_assignment(
            {**raw, "submission_types": ["online_text_entry"]}, None, False, hs.CANVAS_ORIGIN
        )
        self.assertFalse(normal["informational"])
        self.assertTrue(normal["submission_required"])

    def test_new_quiz_collection_200_404_403_and_detail_failure(self):
        assignment = {
            "503": {
                "id": 503,
                "name": "New Quiz",
                "is_quiz_assignment": True,
                "assignment_group_id": 400,
            }
        }

        class Client:
            def __init__(self, mode):
                self.mode = mode

            def get_paginated(self, path, params=None):
                if self.mode == "list_404":
                    raise hs.CanvasHTTPError(404, path)
                if self.mode == "list_403":
                    raise hs.CanvasHTTPError(403, path)
                return [{"id": 700, "assignment_id": 503, "title": "New Quiz"}]

            def get_json(self, path, params=None):
                if self.mode == "detail_404":
                    raise hs.CanvasHTTPError(404, path)
                if self.mode == "detail_403":
                    raise hs.CanvasHTTPError(403, path)
                if self.mode == "detail_500":
                    raise hs.CanvasHTTPError(500, path)
                return {
                    "id": 700,
                    "assignment_id": 503,
                    "title": "New Quiz",
                    "quiz_settings": {
                        "require_student_access_code": True,
                        "student_access_code": "must-not-leak",
                        "filter_ip_address": True,
                    },
                }

        for mode, expected_state, warning_count in (
            ("ok", "collected", 0),
            ("list_404", "not_available", 0),
            ("list_403", "restricted", 0),
            ("detail_404", "not_available", 0),
            ("detail_403", "restricted", 0),
            ("detail_500", "unavailable", 1),
        ):
            with self.subTest(mode=mode):
                collector = hs.CanvasCollector(Client(mode), now=NOW, user_id="999")
                quizzes, source, warnings = collector._collect_new_quizzes(
                    "101", hs.CANVAS_ORIGIN + "/courses/101", assignment
                )
                self.assertEqual(1, len(quizzes))
                self.assertEqual(expected_state, quizzes[0]["detail_state"])
                self.assertEqual(warning_count, len(warnings))
                self.assertNotIn("must-not-leak", json.dumps(quizzes))
                if mode == "ok":
                    self.assertTrue(quizzes[0]["access_requirements"]["access_code"]["required"])
                if warning_count:
                    self.assertEqual("with_warnings", source["completeness"])


class LearningXTest(unittest.TestCase):
    def canvas_data(self):
        client = FixtureCanvasClient()
        selection = hs.select_current_term(client.get_paginated("/api/v1/courses"), NOW)
        return hs.CanvasCollector(client, now=NOW, user_id="999").collect_course(
            selection.courses[0], selection
        )[0]

    def test_lti_bootstrap_uses_one_launch_and_drops_canvas_authorization_on_post(self):
        class Canvas:
            token = "canvas-secret"

            def __init__(self):
                self.launches = 0
                self.tab_calls = []
                self.launch_params = None

            def get_paginated(self, path, params=None):
                self.tab_calls.append(path)
                if "/courses/101/" in path:
                    return [{"id": "home", "html_url": "/courses/101"}]
                return [
                    {
                        "id": "context_external_tool_140",
                        "html_url": "/courses/102/external_tools/140",
                        "type": "external",
                    }
                ]

            def get_json(self, path, params=None):
                self.launches += 1
                self.launch_params = params
                return {"url": "https://learning.hanyang.ac.kr/api/v1/lti/launch"}

        class Bootstrap(hs.LearningXBootstrap):
            def __init__(self, canvas):
                super().__init__(canvas, sleep=lambda _: None)
                self.requests = []

            def _open(self, opener, url, **kwargs):
                self.requests.append((url, kwargs))
                if kwargs.get("method") == "POST":
                    jar = next(handler.cookiejar for handler in opener.handlers if hasattr(handler, "cookiejar"))
                    jar.set_cookie(
                        http.cookiejar.Cookie(
                            0, "xn_api_token", "xn-secret", None, False,
                            "learning.hanyang.ac.kr", True, False,
                            "/learningx", True, True, None, True,
                            None, None, {}, False,
                        )
                    )
                    return b'<div data-user_id="u1" data-user_login="login" data-role="1"></div>'
                return (
                    b'<form action="/learningx/lti/modulebuilder" method="post">'
                    b'<input name="oauth_signature" value="signature">'
                    b'<input name="custom_canvas_user_id" value="u1">'
                    b'<input name="custom_user_login" value="login">'
                    b'<input name="custom_membership_roles" value="Learner">'
                    b'</form>'
                )

        canvas = Canvas()
        bootstrap = Bootstrap(canvas)
        session = bootstrap.prepare([{"id": 101}, {"id": 102}])
        self.assertEqual(1, canvas.launches)
        self.assertEqual(2, len(canvas.tab_calls))
        self.assertEqual({"id": "140", "launch_type": "course_navigation"}, canvas.launch_params)
        self.assertEqual("xn-secret", session.token)
        self.assertEqual("1", session.role)
        self.assertEqual("", session.request_type)
        self.assertEqual(2, len(bootstrap.requests))
        self.assertIn("Authorization", bootstrap.requests[0][1]["headers"])
        self.assertNotIn("Authorization", bootstrap.requests[1][1]["headers"])

    def test_missing_tool_cookie_and_bad_form_fail_closed(self):
        class Canvas:
            token = "canvas-secret"

            def __init__(self, has_tool=True):
                self.has_tool = has_tool

            def get_paginated(self, path, params=None):
                course_id = path.split("/")[4]
                return (
                    [{"id": "context_external_tool_140", "html_url": f"/courses/{course_id}/external_tools/140"}]
                    if self.has_tool else []
                )

            def get_json(self, path, params=None):
                return {"url": "https://learning.hanyang.ac.kr/api/v1/lti/launch"}

        class Bootstrap(hs.LearningXBootstrap):
            action = "/learningx/lti/modulebuilder"

            def _open(self, opener, url, **kwargs):
                if kwargs.get("method") == "POST":
                    return b'<div data-user_id="u1" data-user_login="login" data-role="1"></div>'
                return (
                    f'<form action="{self.action}"><input name="user_id" value="u1">'
                    '<input name="custom_canvas_user_login_id" value="login">'
                    '<input name="roles" value="Student"></form>'
                ).encode()

        with self.assertRaises(hs.HylmsError) as missing_tool:
            Bootstrap(Canvas(False)).prepare([{"id": 101}])
        self.assertEqual("learningx_tool_not_found", missing_tool.exception.code)
        with self.assertRaises(hs.HylmsError) as missing_cookie:
            Bootstrap(Canvas()).prepare([{"id": 101}])
        self.assertEqual("learningx_context_missing", missing_cookie.exception.code)
        bad = Bootstrap(Canvas())
        bad.action = "/learningx/lti/not-modulebuilder"
        with self.assertRaises(hs.HylmsError) as bad_form:
            bad.prepare([{"id": 101}])
        self.assertEqual("learningx_form_origin", bad_form.exception.code)

    def test_bulk_detail_linking_attendance_and_document_dedupe(self):
        data = self.canvas_data()
        session = FakeLearningXSession()
        warnings = hs.LearningXCollector(session, now=NOW).enrich(data)
        hs.finalize_document_paths(data)
        self.assertEqual([], warnings)
        self.assertEqual(5, len(data["weekly_learning"]))
        self.assertEqual(1, len(data["course_resources"]))
        resource = data["course_resources"][0]
        self.assertEqual("commons/pdf", resource["provider_type"])
        self.assertFalse(resource["completed"])
        self.assertEqual(["hycms-content:hc-55"], resource["document_refs"])
        video = next(item for item in data["weekly_learning"] if item["id"] == "canvas-mi-1")
        self.assertEqual("video", video["kind"])
        self.assertEqual("영상", video["title"])
        self.assertEqual("1", str(video["position"]["week"]))
        self.assertEqual("present", video["summary_state"]["state"])
        self.assertTrue(video["source_url"].endswith("/courses/101/modules/items/canvas-mi-1"))
        self.assertEqual("linked", next(item for item in data["weekly_learning"] if item["id"] == "canvas-mi-2")["linked_entity"]["state"])
        self.assertEqual(1, next(item for item in data["weekly_learning"] if item["id"] == "canvas-mi-2")["position"]["week"])
        self.assertEqual("linked", next(item for item in data["weekly_learning"] if item["id"] == "canvas-mi-3")["linked_entity"]["state"])
        self.assertEqual(0, data["sources"]["learningx"]["unknown_type_count"])
        self.assertEqual(0, data["sources"]["learningx"]["section_component_count"])
        self.assertEqual(5, data["sources"]["learningx"]["module_item_count"])
        self.assertEqual(4, data["sources"]["learningx"]["component_count"])
        document = data["documents"]["hycms-content:hc-55"]
        self.assertEqual(2, len(document["references"]))
        self.assertEqual("not_collected", document["download_state"])
        self.assertIn("hycms-content-hc-55", document["saved_path"])
        self.assertEqual("collected", data["sources"]["documents"]["status"])
        details = [path for path, _ in session.calls if "/attendance_items/" in path]
        self.assertEqual(2, len(details))
        self.assertTrue(all("/courses/101/attendance_items/" in path for path in details))
        self.assertIn("/learningx/api/v1/courses/101/attendance_items/lx-1", details)
        self.assertEqual(1, sum(path.endswith("/sections_db") for path, _ in session.calls))
        self.assertEqual(1, sum(path.endswith("/allcomponents_db") for path, _ in session.calls))
        module_calls = [(path, params) for path, params in session.calls if path.endswith("/modules")]
        self.assertEqual(
            [("/learningx/api/v1/courses/101/modules", {"include_detail": "true"})],
            module_calls,
        )
        self.assertEqual(
            [("/learningx/api/v1/courses/101/resources", {"user_login": session.user_login})],
            [(path, params) for path, params in session.calls if path.endswith("/resources")],
        )
        encoded = json.dumps(data, ensure_ascii=False)
        self.assertNotIn(session.token, encoded)
        self.assertNotIn(session.user_id, encoded)
        self.assertNotIn(session.user_login, encoded)
        self.assertNotIn("must-not-leak", encoded)

    def test_canvas_document_warning_survives_learningx_enrichment(self):
        data = self.canvas_data()
        data["sources"]["documents"]["completeness"] = "with_warnings"
        hs.LearningXCollector(FakeLearningXSession(), now=NOW).enrich(data)
        self.assertEqual("with_warnings", data["sources"]["documents"]["completeness"])

    def test_detail_failure_and_unresolved_link_are_warnings_without_data_loss(self):
        data = self.canvas_data()
        session = FakeLearningXSession(fail_detail=True, unresolved=True)
        warnings = hs.LearningXCollector(session, now=NOW).enrich(data)
        self.assertIn("learningx_detail_failed", warnings)
        self.assertIn("learningx_link_unresolved", warnings)
        self.assertEqual(6, len(data["weekly_learning"]))
        self.assertEqual("unavailable", next(item for item in data["weekly_learning"] if item["id"] == "canvas-mi-5")["detail_state"])
        self.assertEqual("unresolved", next(item for item in data["weekly_learning"] if item["id"] == "canvas-mi-6")["linked_entity"]["state"])

    def test_locked_module_items_keep_unique_ids_without_detail_calls(self):
        data = self.canvas_data()
        session = FakeLearningXSession(locked=True)
        warnings = hs.LearningXCollector(session, now=NOW).enrich(data)
        self.assertEqual([], warnings)
        self.assertEqual(["locked-1", "locked-2"], [item["id"] for item in data["weekly_learning"]])
        self.assertEqual(["video", "pdf"], [item["kind"] for item in data["weekly_learning"]])
        self.assertTrue(all(item["detail_state"] == "restricted" for item in data["weekly_learning"]))
        self.assertTrue(all(item["access"] == {"state": "locked", "reason": "not_open"} for item in data["weekly_learning"]))
        self.assertTrue(all(item["document_refs"] == [] for item in data["weekly_learning"]))
        self.assertNotIn("hycms-content:not_open", data["documents"])
        self.assertEqual([], [path for path, _ in session.calls if "/attendance_items/" in path])
        self.assertEqual(2, data["sources"]["learningx"]["module_item_count"])

    def test_unknown_module_type_is_preserved_with_warning(self):
        data = self.canvas_data()
        warnings = hs.LearningXCollector(FakeLearningXSession(unknown=True), now=NOW).enrich(data)
        self.assertIn("learningx_unknown_type", warnings)
        unknown = next(item for item in data["weekly_learning"] if item["id"] == "canvas-mi-unknown")
        self.assertEqual("unknown", unknown["kind"])
        self.assertEqual("with_warnings", data["sources"]["learningx"]["completeness"])

    def test_wiki_page_is_known_and_omitted_obligations_stay_unknown(self):
        class Session(FakeLearningXSession):
            def get_json(self, path, params=None):
                result = super().get_json(path, params)
                if path.endswith('/modules'):
                    result['modules'][0]['module_items'].append({
                        'module_item_id': '9025378', 'content_type': 'wiki_page',
                        'title': 'Week 5 Lecture 2 - No Lecture (Substitute Public Holiday)'})
                return result
        session = Session()
        data = self.canvas_data()
        warnings = hs.LearningXCollector(session, now=NOW).enrich(data)
        self.assertNotIn('learningx_unknown_type', warnings)
        item = next(i for i in data['weekly_learning'] if i['id'] == '9025378')
        self.assertEqual(item['kind'], 'page')
        self.assertEqual(item['provider_type'], 'wiki_page')
        self.assertEqual(item['detail_state'], 'not_applicable')
        self.assertIsNone(item['attendance']['targeted'])
        self.assertIsNone(item['progress']['completed'])
        self.assertEqual(item['schedule']['effective']['due_at'],
                         {'state': 'provider_omitted', 'value': None})
        self.assertEqual(item['document_refs'], [])
        self.assertFalse(any('/attendance_items/9025378' in p for p, _ in session.calls))

    def test_embedded_weekly_resources_are_known_without_losing_obligations(self):
        from hylms.course_context import classify_record
        from hylms.diff import _record
        class Session(FakeLearningXSession):
            required = False
            deadline = False
            def get_json(self, path, params=None):
                result = super().get_json(path, params)
                if path.endswith('/modules'):
                    result['modules'][0]['module_items'].extend([
                        {'module_item_id': str(9014393 - i), 'content_id': f'embed-{i}',
                         'content_type': 'attendance_item', 'title': f'Embedded resource {i}',
                         'url': '/courses/101/modules/items/embed',
                         'content_data': {'item_content_type': 'commons',
                             'item_content_data': {'content_type': subtype}}}
                        for i, subtype in enumerate(('embed', 5, 'none'))])
                elif '/attendance_items/embed-' in path:
                    result.update(unlock_at=None, due_at='2026-09-30T00:00:00Z' if self.deadline else None, late_at=None, lock_at=None,
                                  use_attendance=self.required, completed=False, attendance_status='NONE')
                return result
        for required, deadline in ((False, False), (True, False), (False, True)):
            data = self.canvas_data()
            session = Session(); session.required = required; session.deadline = deadline
            warnings = hs.LearningXCollector(session, now=NOW).enrich(data)
            self.assertNotIn('learningx_unknown_type', warnings)
            self.assertEqual(data['sources']['learningx']['unknown_type_count'], 0)
            items = [i for i in data['weekly_learning'] if i['id'] in {'9014393','9014392','9014391'}]
            self.assertEqual(len(items), 3)
            for item in items:
                self.assertEqual(item['kind'], 'resource' if item['id']=='9014391' else 'embed')
                self.assertEqual(item['provider_type'], 'commons/none' if item['id']=='9014391' else 'commons/embed')
                self.assertEqual(item['attendance']['targeted'], required)
                self.assertEqual(item['document_refs'], [])
                self.assertEqual(classify_record(_record(data['course'], 'weekly_learning', item['id'], item)),
                                 'manage_obligation' if required or deadline else 'resource_only')
            self.assertTrue(all(path.startswith('/learningx/') for path, _ in session.calls))

    def test_embedded_resource_detail_failure_is_not_silenced(self):
        class Session(FakeLearningXSession):
            def get_json(self, path, params=None):
                if path.endswith('/attendance_items/lx-5'):
                    raise hs.LearningXHTTPError(500, path)
                result = super().get_json(path, params)
                if path.endswith('/modules'):
                    result['modules'][0]['module_items'][-1]['content_data']['item_content_data']['content_type'] = 'embed'
                if path.endswith('/allcomponents_db'):
                    for item in result['components']:
                        if item['id']=='lx-5': item['component_type']='commons/embed'
                return result
        data = self.canvas_data()
        warnings = hs.LearningXCollector(Session(), now=NOW).enrich(data)
        self.assertIn('learningx_detail_failed', warnings)
        item = next(i for i in data['weekly_learning'] if i['id']=='canvas-mi-5')
        self.assertEqual(item['kind'], 'embed')
        self.assertEqual(item['detail_state'], 'unavailable')

    def test_unknown_resource_type_is_preserved_with_warning(self):
        data = self.canvas_data()
        warnings = hs.LearningXCollector(FakeLearningXSession(unknown_resource=True), now=NOW).enrich(data)
        self.assertIn("learningx_resource_unknown_type", warnings)
        self.assertEqual("unknown", data["course_resources"][0]["provider_type"])
        self.assertEqual(1, data["sources"]["course_resources"]["unknown_type_count"])
        self.assertEqual("with_warnings", data["sources"]["course_resources"]["completeness"])
        self.assertEqual(1, data["sources"]["learningx"]["unknown_type_count"])

    def test_invalid_bulk_is_a_course_failure(self):
        with self.assertRaises(hs.HylmsError):
            hs.LearningXCollector(FakeLearningXSession(invalid_bulk=True), now=NOW).enrich(self.canvas_data())

    def test_runner_reuses_one_prepared_session_and_writes_schema_five(self):
        session = FakeLearningXSession()

        class Bootstrap:
            def __init__(self):
                self.calls = 0

            def prepare(self, courses):
                self.calls += 1
                return session

        bootstrap = Bootstrap()
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "hylms.storage.DocumentDownloader", NoopDocumentDownloader
        ):
            code = hs.SnapshotRunner(
                FixtureCanvasClient(), output_root=Path(directory), now=NOW,
                clock=lambda: NOW, out=lambda _: None, learningx_bootstrap=bootstrap,
            ).run("999")
            self.assertEqual(0, code)
            self.assertEqual(1, bootstrap.calls)
            status = json.loads((Path(directory) / "26-2" / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(5, status["schema_version"])
            self.assertEqual(0, status["courses"][0]["failed_detail_count"])

    def test_unknown_resource_makes_runner_partial_success(self):
        class Bootstrap:
            def prepare(self, courses):
                return FakeLearningXSession(unknown_resource=True)

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "hylms.storage.DocumentDownloader", NoopDocumentDownloader
        ):
            code = hs.SnapshotRunner(
                FixtureCanvasClient(), output_root=Path(directory), now=NOW,
                clock=lambda: NOW, out=lambda _: None, learningx_bootstrap=Bootstrap(),
            ).run("999")
            self.assertEqual(2, code)
            status = json.loads((Path(directory) / "26-2" / "status.json").read_text(encoding="utf-8"))
            self.assertEqual("updated_with_warnings", status["courses"][0]["status"])
            self.assertEqual(["learningx_resource_unknown_type"], status["courses"][0]["warning_codes"])

    def test_bootstrap_failure_preserves_existing_schema_two_snapshot(self):
        class Bootstrap:
            def prepare(self, courses):
                raise hs.HylmsError("learningx_context_missing", "safe")

        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            term.mkdir()
            old = term / "자료구조_ 실습__c101.json"
            old.write_text(
                '{"schema_version":2,"course":{"last_success_at":"old"}}\n', encoding="utf-8"
            )
            code = hs.SnapshotRunner(
                FixtureCanvasClient(), output_root=Path(directory), now=NOW,
                clock=lambda: NOW, out=lambda _: None, learningx_bootstrap=Bootstrap(),
            ).run("999")
            self.assertEqual(1, code)
            self.assertEqual(2, json.loads(old.read_text(encoding="utf-8"))["schema_version"])
            status = json.loads((term / "status.json").read_text(encoding="utf-8"))
            self.assertEqual("kept", status["courses"][0]["status"])
            self.assertEqual("learningx_context_missing", status["courses"][0]["error_code"])


class DocumentDownloaderTest(unittest.TestCase):
    @staticmethod
    def document(provider, provider_id, filename):
        prefix = "canvas-file" if provider == "canvas" else "hycms-content"
        document_id = f"{prefix}:{provider_id}"
        return document_id, {
            "id": document_id,
            "provider": provider,
            "provider_id": str(provider_id),
            "provider_filename": filename,
            "extension": Path(filename).suffix.lower(),
            "content_type": None,
            "size": None,
            "saved_filename": f"{prefix}-{provider_id}__{filename}",
            "saved_path": f"files/course__c101/{prefix}-{provider_id}__{filename}",
            "stable_url": (
                f"{hs.CANVAS_ORIGIN}/courses/101/files/{provider_id}/download"
                if provider == "canvas" else None
            ),
            "access_state": "restricted" if provider == "canvas" else "available",
            "download_state": "not_collected",
            "error_code": None,
            "references": [
                {"source": "announcement" if provider == "canvas" else "weekly_learning", "source_id": "1", "relation": "content"},
                {"source": "course_resource", "source_id": "2", "relation": "content"},
            ],
        }

    @staticmethod
    def data(*documents, eligible_count=None):
        values = dict(documents)
        count = len(values) if eligible_count is None else eligible_count
        return {
            "course": {"id": "101", "name": "course"},
            "documents": values,
            "sources": {
                "documents": {
                    "status": "collected" if values else "empty",
                    "discovered_count": count,
                    "eligible_count": count,
                    "registered_count": len(values),
                    "restricted_count": 0,
                    "skipped_extension_count": 0,
                    "downloaded_count": 0,
                    "existing_count": 0,
                    "failed_count": 0,
                    "completeness": "complete",
                }
            },
        }

    @staticmethod
    def client():
        return FakeCanvasDownloadClient()

    def test_canvas_redirect_strips_bearer_and_rejects_insecure_redirect(self):
        handler = document_module._CanvasDownloadRedirectHandler()
        request = urllib.request.Request(
            f"{hs.CANVAS_ORIGIN}/courses/101/files/700/download",
            headers={"Authorization": "Bearer canvas-secret"},
        )
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://object.example.test/file"
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))
        same_origin = handler.redirect_request(
            request, None, 302, "Found", {}, f"{hs.CANVAS_ORIGIN}/files/700/download"
        )
        self.assertEqual("Bearer canvas-secret", same_origin.get_header("Authorization"))
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://object.example.test/file")
        hycms_handler = document_module._SameOriginRedirectHandler("https://hycms.hanyang.ac.kr/viewer")
        hycms_request = urllib.request.Request("https://hycms.hanyang.ac.kr/viewer")
        self.assertIsNotNone(
            hycms_handler.redirect_request(
                hycms_request, None, 307, "Redirect", {}, "https://hycms.hanyang.ac.kr/em/1"
            )
        )
        with self.assertRaises(urllib.error.HTTPError):
            hycms_handler.redirect_request(
                hycms_request, None, 307, "Redirect", {}, "https://other.example.test/em/1"
            )

    def test_canvas_and_hycms_download_once_then_reuse_existing_files(self):
        canvas_id, canvas_document = self.document("canvas", "700", "공지.docx")
        hycms_id, hycms_document = self.document("hycms", "hc-55", "주차자료.pdf")
        data = self.data((canvas_id, canvas_document), (hycms_id, hycms_document))
        canvas_opener = FakeDownloadOpener(
            lambda request: FakeDownloadResponse(b"PK\x03\x04canvas", url="https://object.example.test/file")
        )
        canvas = self.client()
        viewer_seen = []

        def open_hycms(request):
            if "/viewer" in request.full_url:
                viewer_seen.append(True)
                return FakeDownloadResponse(
                    b"<!DOCTYPE html><html>viewer</html>",
                    url="https://hycms.hanyang.ac.kr/em/1?session=must-not-forward",
                    headers={"Content-Type": "text/html"},
                )
            self.assertTrue(viewer_seen)
            return FakeDownloadResponse(
                b"%PDF-hycms", url=request.full_url,
                headers={"Content-Type": "application/pdf"},
            )

        hycms_opener = FakeDownloadOpener(open_hycms)

        class LearningX:
            token = "xn-secret"
            opener = hycms_opener

            def __init__(self):
                self.calls = []

            def get_json(self, path, params=None):
                self.calls.append((path, params))
                return {"result": {
                    "view_url": "https://hycms.hanyang.ac.kr/viewer?token=viewer-secret",
                    "download_url": "https://hycms.hanyang.ac.kr/download?signature=download-secret",
                }}

        learningx = LearningX()
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory)
            downloader = hs.DocumentDownloader(
                canvas, learningx, canvas_opener=canvas_opener,
                hycms_opener=hycms_opener, sleep=lambda _: None,
            )
            self.assertEqual([], downloader.download(data, term))
            self.assertEqual("downloaded", data["documents"][canvas_id]["download_state"])
            self.assertEqual("available", data["documents"][canvas_id]["access_state"])
            self.assertEqual("downloaded", data["documents"][hycms_id]["download_state"])
            self.assertEqual(b"PK\x03\x04canvas", (term / canvas_document["saved_path"]).read_bytes())
            self.assertEqual(b"%PDF-hycms", (term / hycms_document["saved_path"]).read_bytes())
            self.assertEqual(2, data["sources"]["documents"]["downloaded_count"])
            self.assertEqual("Bearer canvas-secret", canvas_opener.requests[0][1]["Authorization"])
            self.assertEqual([("/api/v1/files/700", None)], canvas.calls)
            self.assertIn("verifier=canvas-verifier", canvas_opener.requests[0][0])
            self.assertNotIn("/courses/101/", canvas_opener.requests[0][0])
            self.assertEqual(
                [("/learningx/api/v1/commons/contents", {"content_id": "hc-55"})],
                learningx.calls,
            )
            self.assertEqual(
                "https://hycms.hanyang.ac.kr/em/1",
                hycms_opener.requests[1][1]["Referer"],
            )
            request_count = len(canvas.calls) + len(canvas_opener.requests) + len(hycms_opener.requests) + len(learningx.calls)

            self.assertEqual([], downloader.download(data, term))
            self.assertEqual(2, data["sources"]["documents"]["existing_count"])
            self.assertEqual(request_count, len(canvas.calls) + len(canvas_opener.requests) + len(hycms_opener.requests) + len(learningx.calls))
            encoded = json.dumps(data, ensure_ascii=False)
            self.assertNotIn("canvas-secret", encoded)
            self.assertNotIn("xn-secret", encoded)
            self.assertNotIn("viewer-secret", encoded)
            self.assertNotIn("download-secret", encoded)
            self.assertNotIn("canvas-verifier", encoded)

    def test_html_download_response_is_rejected_without_a_final_file(self):
        document_id, document = self.document("canvas", "700", "공지.pdf")
        data = self.data((document_id, document))
        opener = FakeDownloadOpener(
            lambda request: FakeDownloadResponse(
                b"\r\n<!DOCTYPE html><html>login</html>",
                url="https://auth.example.test/complete",
                headers={"Content-Type": "application/octet-stream"},
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory)
            warnings = hs.DocumentDownloader(
                self.client(), SimpleNamespace(opener=None),
                canvas_opener=opener, sleep=lambda _: None,
            ).download(data, term)
            self.assertFalse((term / document["saved_path"]).exists())
            self.assertEqual([], list(term.rglob("*.tmp")))
        self.assertEqual(["document_download_failed"], warnings)
        self.assertEqual("failed", document["download_state"])
        self.assertEqual("document_login_response", document["error_code"])

    def test_hycms_viewer_html_is_allowed_but_final_html_is_rejected(self):
        document_id, document = self.document("hycms", "hc-55", "주차자료.pdf")
        data = self.data((document_id, document))
        calls = []

        def open_hycms(request):
            calls.append(request.full_url)
            if "/viewer" in request.full_url:
                return FakeDownloadResponse(
                    b"<html>viewer</html>", url=request.full_url,
                    headers={"Content-Type": "text/html"},
                )
            return FakeDownloadResponse(
                b"<html>login</html>", url=request.full_url,
                headers={"Content-Type": "text/html; charset=utf-8"},
            )

        opener = FakeDownloadOpener(open_hycms)

        class LearningX:
            def get_json(self, path, params=None):
                return {"result": {
                    "view_url": "https://hycms.hanyang.ac.kr/viewer?token=secret",
                    "download_url": "https://hycms.hanyang.ac.kr/download?signature=secret",
                }}

        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory)
            warnings = hs.DocumentDownloader(
                self.client(), LearningX(), hycms_opener=opener, sleep=lambda _: None,
            ).download(data, term)
            self.assertFalse((term / document["saved_path"]).exists())
        self.assertEqual(2, len(calls))
        self.assertEqual(["document_download_failed"], warnings)
        self.assertEqual("document_login_response", document["error_code"])

    def test_existing_login_html_is_replaced_or_removed_if_retry_fails(self):
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds), tempfile.TemporaryDirectory() as directory:
                document_id, document = self.document("canvas", "700", "공지.pdf")
                document["download_state"] = "existing"
                data = self.data((document_id, document))
                term = Path(directory)
                target = term / document["saved_path"]
                target.parent.mkdir(parents=True)
                target.write_bytes(b"\r\n<!DOCTYPE html><html>login</html>")

                def open_canvas(request):
                    if not succeeds:
                        raise urllib.error.HTTPError(request.full_url, 500, "Error", {}, None)
                    return FakeDownloadResponse(
                        b"%PDF-real", url="https://object.example.test/file",
                        headers={"Content-Type": "application/pdf"},
                    )

                warnings = hs.DocumentDownloader(
                    self.client(), SimpleNamespace(opener=None),
                    canvas_opener=FakeDownloadOpener(open_canvas), sleep=lambda _: None,
                ).download(data, term)
                if succeeds:
                    self.assertEqual([], warnings)
                    self.assertEqual(b"%PDF-real", target.read_bytes())
                    self.assertEqual("downloaded", document["download_state"])
                else:
                    self.assertEqual(["document_download_failed"], warnings)
                    self.assertFalse(target.exists())
                    self.assertEqual("failed", document["download_state"])
                self.assertEqual([], list(term.rglob("*.tmp")))

    def test_canvas_metadata_http_failures_and_invalid_url_are_classified(self):
        cases = (
            (FakeCanvasDownloadClient(error=hs.CanvasHTTPError(403, "/api/v1/files/700")), "restricted", "document_http_403"),
            (FakeCanvasDownloadClient(error=hs.CanvasHTTPError(500, "/api/v1/files/700")), "failed", "document_http_500"),
            (FakeCanvasDownloadClient(metadata={"url": f"{hs.CANVAS_ORIGIN}/courses/101/files/700/download"}), "failed", "canvas_download_url_invalid"),
        )
        for client, state, error_code in cases:
            with self.subTest(state=state, error_code=error_code), tempfile.TemporaryDirectory() as directory:
                document_id, document = self.document("canvas", "700", "공지.pdf")
                data = self.data((document_id, document))
                opener = FakeDownloadOpener(lambda request: self.fail("download request was not expected"))
                hs.DocumentDownloader(
                    client, SimpleNamespace(opener=None),
                    canvas_opener=opener, sleep=lambda _: None,
                ).download(data, Path(directory))
                self.assertEqual(state, document["download_state"])
                self.assertEqual(error_code, document["error_code"])
                self.assertEqual([], opener.requests)

    def test_unregistered_canvas_login_html_is_removed_without_touching_other_files(self):
        data = self.data()
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory)
            course_directory = term / "files" / "course__c101"
            course_directory.mkdir(parents=True)
            invalid = course_directory / "canvas-file-old__login.pptx"
            valid = course_directory / "canvas-file-old__valid.pdf"
            unrelated = course_directory / "hycms-content-old__login.pdf"
            invalid.write_bytes(b"<!DOCTYPE html><html>login</html>")
            valid.write_bytes(b"%PDF-valid")
            unrelated.write_bytes(b"<!DOCTYPE html><html>login</html>")
            warnings = hs.DocumentDownloader(
                self.client(), SimpleNamespace(opener=None), sleep=lambda _: None
            ).download(data, term)
            self.assertEqual([], warnings)
            self.assertFalse(invalid.exists())
            self.assertTrue(valid.exists())
            self.assertTrue(unrelated.exists())

    def test_success_state_with_missing_file_and_previous_failure_are_retried(self):
        first_id, first = self.document("canvas", "700", "첫째.pdf")
        second_id, second = self.document("canvas", "701", "둘째.pdf")
        first["download_state"] = "downloaded"
        second["download_state"] = "failed"
        data = self.data((first_id, first), (second_id, second))
        opener = FakeDownloadOpener(
            lambda request: FakeDownloadResponse(request.full_url.encode(), url="https://object.example.test/file")
        )
        learningx = SimpleNamespace(opener=None, get_json=lambda path: None)
        with tempfile.TemporaryDirectory() as directory:
            warnings = hs.DocumentDownloader(
                self.client(), learningx, canvas_opener=opener, sleep=lambda _: None
            ).download(data, Path(directory))
        self.assertEqual([], warnings)
        self.assertEqual(2, len(opener.requests))
        self.assertTrue(all(document["download_state"] == "downloaded" for document in data["documents"].values()))

    def test_server_error_retries_at_most_three_times(self):
        document_id, document = self.document("canvas", "700", "자료.pdf")
        data = self.data((document_id, document))
        attempts = []

        def open_canvas(request):
            attempts.append(request.full_url)
            if len(attempts) < 3:
                raise urllib.error.HTTPError(request.full_url, 500, "Error", {}, None)
            return FakeDownloadResponse(b"ok", url="https://object.example.test/file")

        with tempfile.TemporaryDirectory() as directory:
            warnings = hs.DocumentDownloader(
                self.client(), SimpleNamespace(opener=None),
                canvas_opener=FakeDownloadOpener(open_canvas), sleep=lambda _: None,
            ).download(data, Path(directory))
        self.assertEqual([], warnings)
        self.assertEqual(3, len(attempts))
        self.assertEqual("downloaded", document["download_state"])

    def test_restricted_login_and_registry_mismatch_are_explicit(self):
        restricted_id, restricted = self.document("canvas", "700", "제한.pdf")
        login_id, login = self.document("canvas", "701", "로그인.pdf")
        data = self.data((restricted_id, restricted), (login_id, login), eligible_count=3)

        def open_canvas(request):
            if "/700/" in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
            return FakeDownloadResponse(b"login", url=f"{hs.CANVAS_ORIGIN}/login/saml")

        opener = FakeDownloadOpener(open_canvas)
        learningx = SimpleNamespace(opener=None, get_json=lambda path: None)
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory)
            warnings = hs.DocumentDownloader(
                self.client(), learningx, canvas_opener=opener, sleep=lambda _: None
            ).download(data, term)
            self.assertEqual([], list(term.rglob("*.tmp")))
            self.assertEqual([], [path for path in term.rglob("*") if path.is_file()])
        self.assertEqual("restricted", restricted["download_state"])
        self.assertEqual("document_http_403", restricted["error_code"])
        self.assertEqual("failed", login["download_state"])
        self.assertEqual("document_login_redirect", login["error_code"])
        self.assertEqual("incomplete", data["sources"]["documents"]["completeness"])
        self.assertEqual(
            ["document_download_failed", "document_registry_incomplete"], warnings
        )

    def test_partial_transfer_and_filesystem_failure_leave_no_final_file(self):
        class BrokenResponse(FakeDownloadResponse):
            def __init__(self):
                super().__init__(b"partial", url="https://object.example.test/file")
                self.returned = False

            def read(self, size=-1):
                if not self.returned:
                    self.returned = True
                    return super().read(size)
                raise OSError("connection lost")

        for failure in ("transfer", "replace"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                document_id, document = self.document("canvas", "700", "자료.pdf")
                data = self.data((document_id, document))
                opener = FakeDownloadOpener(
                    lambda request: BrokenResponse()
                    if failure == "transfer"
                    else FakeDownloadResponse(b"complete", url="https://object.example.test/file")
                )
                learningx = SimpleNamespace(opener=None, get_json=lambda path: None)
                replace = mock.patch("hylms.documents.os.replace", side_effect=OSError("disk")) if failure == "replace" else mock.patch("hylms.documents.os.replace", wraps=os.replace)
                with replace:
                    warnings = hs.DocumentDownloader(
                        self.client(), learningx, canvas_opener=opener, sleep=lambda _: None
                    ).download(data, Path(directory))
                self.assertEqual(["document_download_failed"], warnings)
                self.assertEqual("failed", document["download_state"])
                self.assertEqual(
                    "document_transport_error" if failure == "transfer" else "filesystem_error",
                    document["error_code"],
                )
                self.assertEqual([], [path for path in Path(directory).rglob("*") if path.is_file()])
                self.assertEqual([], list(Path(directory).rglob("*.tmp")))

    def test_preserved_success_path_keeps_name_and_mtime(self):
        document_id, current = self.document("canvas", "700", "새 이름.pdf")
        data = self.data((document_id, current))
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory)
            old_path = "files/옛 과목__c101/canvas-file-700__옛 이름.pdf"
            previous = term / "옛 과목__c101.json"
            previous.write_text(
                json.dumps({"documents": {document_id: {
                    "saved_filename": "canvas-file-700__옛 이름.pdf",
                    "saved_path": old_path,
                    "download_state": "downloaded",
                }}}, ensure_ascii=False),
                encoding="utf-8",
            )
            target = term / old_path
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            before = target.stat().st_mtime_ns
            hs.preserve_document_paths(data, previous)
            opener = FakeDownloadOpener(lambda request: self.fail("network request was not expected"))
            warnings = hs.DocumentDownloader(
                self.client(), SimpleNamespace(opener=None), canvas_opener=opener, sleep=lambda _: None
            ).download(data, term)
            self.assertEqual([], warnings)
            self.assertEqual("새 이름.pdf", current["provider_filename"])
            self.assertEqual("canvas-file-700__옛 이름.pdf", current["saved_filename"])
            self.assertEqual(old_path, current["saved_path"])
            self.assertEqual("existing", current["download_state"])
            self.assertEqual(before, target.stat().st_mtime_ns)
            self.assertEqual([], opener.requests)

    def test_invalid_preserved_path_is_rejected(self):
        document_id, document = self.document("canvas", "700", "자료.pdf")
        document["saved_path"] = "../outside.pdf"
        data = self.data((document_id, document))
        opener = FakeDownloadOpener(lambda request: self.fail("network request was not expected"))
        with tempfile.TemporaryDirectory() as directory:
            warnings = hs.DocumentDownloader(
                self.client(), SimpleNamespace(opener=None), canvas_opener=opener, sleep=lambda _: None
            ).download(data, Path(directory))
        self.assertEqual(["document_download_failed"], warnings)
        self.assertEqual("document_path_invalid", document["error_code"])
        self.assertEqual([], opener.requests)

    def test_unregistered_extension_is_not_downloaded(self):
        document_id, document = self.document("canvas", "700", "자료.zip")
        data = self.data((document_id, document))
        opener = FakeDownloadOpener(lambda request: self.fail("network request was not expected"))
        with tempfile.TemporaryDirectory() as directory:
            warnings = hs.DocumentDownloader(
                self.client(), SimpleNamespace(opener=None), canvas_opener=opener, sleep=lambda _: None
            ).download(data, Path(directory))
        self.assertEqual(["document_download_failed"], warnings)
        self.assertEqual("document_extension_rejected", document["error_code"])
        self.assertEqual([], opener.requests)

    def test_runner_writes_partial_snapshot_when_one_document_fails(self):
        class PartialDownloader:
            def __init__(self, canvas, learningx):
                pass

            def download(self, data, term_directory):
                documents = list(data["documents"].values())
                first = documents[0]
                first["download_state"] = "failed"
                first["error_code"] = "document_transport_error"
                for document in documents[1:]:
                    document["download_state"] = "existing"
                    document["error_code"] = None
                source = data["sources"]["documents"]
                source.update({
                    "downloaded_count": 0,
                    "existing_count": len(documents) - 1,
                    "failed_count": 1,
                    "restricted_count": 0,
                    "completeness": "with_warnings",
                })
                return ["document_download_failed"]

        class Bootstrap:
            def prepare(self, courses):
                return FakeLearningXSession()

        messages = []
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "hylms.storage.DocumentDownloader", PartialDownloader
        ):
            code = hs.SnapshotRunner(
                FixtureCanvasClient(), output_root=Path(directory), now=NOW,
                clock=lambda: NOW, out=messages.append, learningx_bootstrap=Bootstrap(),
            ).run("999")
            status = json.loads((Path(directory) / "26-2" / "status.json").read_text(encoding="utf-8"))
            course = json.loads(
                (Path(directory) / "26-2" / status["courses"][0]["path"]).read_text(encoding="utf-8")
            )
        self.assertEqual(2, code)
        self.assertEqual("partial_failure", status["overall_status"])
        self.assertEqual("updated_with_warnings", status["courses"][0]["status"])
        self.assertIn("document_download_failed", status["courses"][0]["warning_codes"])
        self.assertEqual(5, course["schema_version"])
        self.assertTrue(any(document["download_state"] == "failed" for document in course["documents"].values()))
        self.assertEqual(hs.document_summary(course), status["courses"][0]["documents"])
        self.assertTrue(any(
            hs.document_summary_text(status["courses"][0]["documents"]) in message
            for message in messages
        ))
        self.assertEqual(
            f"[완료] {status['overall_status']} | 종료 코드 {status['exit_code']} | "
            f"{status['started_at']} → {status['ended_at']}",
            messages[-1],
        )


class HttpClientTest(unittest.TestCase):
    def response(self, status, payload, headers=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        return hs.HttpResponse(status, headers or {}, body)

    def test_opaque_pagination(self):
        next_url = "https://learning.hanyang.ac.kr/api/v1/courses?opaque=abc"
        transport = QueueTransport([
            self.response(200, [{"id": 1}], {"link": f'<{next_url}>; rel="next"'}),
            self.response(200, [{"id": 2}]),
        ])
        client = hs.CanvasClient("secret", transport=transport, sleep=lambda _: None)
        self.assertEqual([{"id": 1}, {"id": 2}], client.get_paginated("/api/v1/courses"))
        self.assertEqual(next_url, transport.requests[1][1])

    def test_cross_origin_pagination_is_blocked(self):
        transport = QueueTransport([
            self.response(200, [], {"link": '<https://evil.example/page>; rel="next"'})
        ])
        client = hs.CanvasClient("secret", transport=transport, sleep=lambda _: None)
        with self.assertRaises(hs.CanvasTransportError) as caught:
            client.get_paginated("/api/v1/courses")
        self.assertEqual("canvas_cross_origin", caught.exception.code)

    def test_429_and_5xx_retry_sequentially(self):
        sleeps = []
        transport = QueueTransport([
            self.response(429, {}, {"retry-after": "2"}),
            self.response(500, {}),
            self.response(200, {"id": 1}),
        ])
        client = hs.CanvasClient("secret", transport=transport, sleep=sleeps.append)
        self.assertEqual({"id": 1}, client.get_json("/api/v1/users/self"))
        self.assertEqual([2.0, 2.0], sleeps)
        self.assertTrue(all(req[2]["Authorization"] == "Bearer secret" for req in transport.requests))

    def test_error_does_not_expose_query(self):
        transport = QueueTransport([self.response(401, {})])
        client = hs.CanvasClient("secret", transport=transport, sleep=lambda _: None)
        with self.assertRaises(hs.CanvasHTTPError) as caught:
            client.get_json("/api/v1/test?token=secret")
        self.assertNotIn("secret", caught.exception.message)

    def test_known_initial_post_marker_is_exposed_without_error_body(self):
        transport = QueueTransport([hs.HttpResponse(403, {}, b"require_initial_post private body")])
        client = hs.CanvasClient("secret", transport=transport, sleep=lambda _: None)
        with self.assertRaises(hs.CanvasHTTPError) as caught:
            client.get_json("/api/v1/courses/1/discussion_topics/2/view")
        self.assertEqual("initial_post_required", caught.exception.reason)
        self.assertNotIn("private body", caught.exception.message)


class CollectorIntegrationTest(unittest.TestCase):
    def test_build_one_schema_and_canonical_links(self):
        client = FixtureCanvasClient()
        selection = hs.select_current_term(client.get_paginated("/api/v1/courses"), NOW)
        collector = hs.CanvasCollector(client, now=NOW, user_id="999")
        data, warnings = collector.collect_course(selection.courses[0], selection)
        hs.finalize_document_paths(data)

        expected_top = {
            "schema_version", "collected_at", "term", "course", "sources", "syllabus", "announcements",
            "assignment_groups", "assignments", "discussions", "quizzes", "weekly_learning", "course_resources", "documents",
        }
        self.assertEqual(expected_top, set(data))
        self.assertEqual([], data["discussions"])
        self.assertEqual([], data["weekly_learning"])
        self.assertEqual([], data["course_resources"])
        self.assertEqual("empty", data["sources"]["canvas_discussions"]["status"])
        self.assertEqual(2, len(data["assignment_groups"]))
        self.assertEqual([], data["assignment_groups"][1]["items"])
        self.assertNotIn("assignment_id", data["assignment_groups"][0]["items"][0])
        self.assertEqual(["501"], [item["id"] for item in data["assignments"]])
        self.assertEqual({"601", "503"}, {item["id"] for item in data["quizzes"]})
        self.assertEqual("restricted", next(item for item in data["quizzes"] if item["id"] == "601")["questions"][0]["user_answer"]["state"])
        self.assertEqual("provider_omitted", next(item for item in data["quizzes"] if item["id"] == "601")["questions"][0]["user_answer"]["reason"])
        self.assertTrue(
            any(
                method == "get" and path == "/api/v1/quiz_submissions/701/questions"
                for method, path, _ in client.calls
            )
        )
        self.assertEqual("assignment", data["quizzes"][0]["quiz_type"])
        self.assertIn("omit_from_final_grade", data["assignments"][0])
        self.assertTrue(data["assignments"][0]["submission_required"])
        self.assertFalse(data["assignments"][0]["informational"])
        self.assertEqual(["canvas-file:700"], list(data["documents"]))
        document = data["documents"]["canvas-file:700"]
        self.assertEqual(2, len(document["references"]))
        self.assertEqual("not_collected", document["download_state"])
        self.assertTrue(document["saved_path"].startswith("files/자료구조_ 실습__c101/"))
        self.assertNotIn("Signature", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("private@example.com", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("must-not-leak", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("10.0.0.0", json.dumps(data, ensure_ascii=False))
        self.assertEqual(5, data["schema_version"])
        self.assertEqual([], warnings)

    def test_discussion_privacy_and_graded_canonical_link(self):
        client = BuildTwoDiscussionClient()
        selection = hs.select_current_term(client.get_paginated("/api/v1/courses"), NOW)
        data, warnings = hs.CanvasCollector(client, now=NOW, user_id="999").collect_course(
            selection.courses[0], selection
        )
        self.assertEqual([], warnings)
        self.assertEqual(2, len(data["discussions"]))
        independent = next(item for item in data["discussions"] if item["id"] == "900")
        graded = next(item for item in data["discussions"] if item["id"] == "901")
        self.assertIsNone(independent["assignment_id"])
        self.assertEqual("504", graded["assignment_id"])
        self.assertEqual("400", graded["assignment_group_id"])
        self.assertEqual("submitted", graded["submission"]["workflow_state"])
        self.assertNotIn("504", {item["id"] for item in data["assignments"]})
        discussion_ref = next(
            item
            for group in data["assignment_groups"]
            for item in group["items"]
            if item["kind"] == "discussion"
        )
        self.assertEqual(
            {"kind": "discussion", "id": "901", "assignment_id": "504"}, discussion_ref
        )
        own_entries = independent["entries"]["items"]
        self.assertEqual({"1001", "1004"}, {item["id"] for item in own_entries})
        first = next(item for item in own_entries if item["id"] == "1001")
        self.assertEqual("1000", first["parent_id"])
        self.assertEqual("direct-feedback", first["feedback_entries"][0]["body"]["text"])
        self.assertEqual("Instructor", first["feedback_entries"][0]["author_display_name"])
        encoded = json.dumps(data, ensure_ascii=False)
        self.assertIn("my-reply", encoded)
        self.assertIn("duplicate-self", encoded)
        self.assertNotIn("my-top-level", encoded)
        self.assertIn("new-direct-feedback", encoded)
        self.assertNotIn("peer-root-private", encoded)
        self.assertNotIn("nested-peer-private", encoded)
        self.assertNotIn("Peer Root", encoded)
        self.assertNotIn("Nested Peer", encoded)
        self.assertNotIn("Self Name", encoded)
        self.assertNotIn("private-avatar", encoded)
        self.assertNotIn('"user_id"', encoded)
        self.assertEqual(1, len(data["documents"]))
        self.assertEqual(1, data["sources"]["canvas_discussions"]["graded_count"])
        self.assertEqual(1, data["sources"]["canvas_discussions"]["linked_count"])
        self.assertEqual(1, data["sources"]["canvas_assessments"]["graded_discussion_count"])

    def test_initial_post_restriction_is_not_a_warning(self):
        class InitialPostClient(FixtureCanvasClient):
            def get_paginated(self, path, params=None):
                if path == "/api/v1/courses/101/discussion_topics":
                    return [
                        {
                            "id": 910,
                            "title": "먼저 작성",
                            "message": "prompt",
                            "require_initial_post": True,
                            "user_can_see_posts": False,
                            "discussion_subentry_count": 3,
                        }
                    ]
                return super().get_paginated(path, params)

            def get_json(self, path, params=None):
                if path == "/api/v1/courses/101/discussion_topics/910":
                    return {
                        "id": 910,
                        "title": "먼저 작성",
                        "message": "prompt",
                        "require_initial_post": True,
                        "user_can_see_posts": False,
                        "discussion_subentry_count": 3,
                    }
                if path.endswith("/discussion_topics/910/view"):
                    raise AssertionError("view must not be called")
                return super().get_json(path, params)

        client = InitialPostClient()
        selection = hs.select_current_term(client.get_paginated("/api/v1/courses"), NOW)
        data, warnings = hs.CanvasCollector(client, now=NOW, user_id="999").collect_course(
            selection.courses[0], selection
        )
        self.assertEqual([], warnings)
        topic = data["discussions"][0]
        self.assertEqual("restricted", topic["entries"]["state"])
        self.assertEqual("initial_post_required", topic["entries"]["reason"])
        self.assertEqual("restricted", data["sources"]["canvas_discussions"]["completeness"])

    def test_discussion_detail_failure_updates_with_warning(self):
        class DetailFailureClient(FixtureCanvasClient):
            def get_paginated(self, path, params=None):
                if path == "/api/v1/courses/101/discussion_topics":
                    return [{"id": 920, "title": "metadata", "message": "prompt", "discussion_subentry_count": 0}]
                return super().get_paginated(path, params)

            def get_json(self, path, params=None):
                if path == "/api/v1/courses/101/discussion_topics/920":
                    raise hs.CanvasHTTPError(503, path)
                return super().get_json(path, params)

        client = DetailFailureClient()
        selection = hs.select_current_term(client.get_paginated("/api/v1/courses"), NOW)
        data, warnings = hs.CanvasCollector(client, now=NOW, user_id="999").collect_course(
            selection.courses[0], selection
        )
        self.assertEqual(["discussion_detail_failed"], warnings)
        self.assertEqual("unavailable", data["discussions"][0]["detail_state"])
        self.assertEqual(1, data["sources"]["canvas_discussions"]["detail_failed_count"])
        with tempfile.TemporaryDirectory() as directory:
            code = hs.SnapshotRunner(
                DetailFailureClient(),
                output_root=Path(directory),
                now=NOW,
                clock=lambda: NOW,
                out=lambda _: None,
            ).run("999")
            self.assertEqual(2, code)
            status = json.loads(
                (Path(directory) / "26-2" / "status.json").read_text(encoding="utf-8")
            )
            self.assertEqual("updated_with_warnings", status["courses"][0]["status"])
            self.assertIn("discussion_detail_failed", status["courses"][0]["warning_codes"])

    def test_quiz_detail_failure_keeps_list_record_with_warning(self):
        class QuizDetailFailureClient(FixtureCanvasClient):
            def get_json(self, path, params=None):
                if path == "/api/v1/courses/101/quizzes/601":
                    raise hs.CanvasHTTPError(503, path)
                return super().get_json(path, params)

        client = QuizDetailFailureClient()
        selection = hs.select_current_term(client.get_paginated("/api/v1/courses"), NOW)
        data, warnings = hs.CanvasCollector(client, now=NOW, user_id="999").collect_course(
            selection.courses[0], selection
        )
        quiz = next(item for item in data["quizzes"] if item["id"] == "601")
        self.assertEqual("unavailable", quiz["detail_state"])
        self.assertIn("quiz_detail_failed", warnings)
        self.assertEqual(1, data["sources"]["canvas_assessments"]["detail_failed_count"])
        self.assertEqual("with_warnings", data["sources"]["canvas_assessments"]["completeness"])

    def test_provided_quiz_answer_keeps_evidence_and_stringifies_option_id(self):
        question = {"id": 1, "question_text": "Q", "answers": [{"id": 2, "text": "A"}]}
        value = hs.CanvasCollector._normalize_quiz_question(
            question, {"id": 1, "answer": 2, "correct": True}, hs.CANVAS_ORIGIN, False, True
        )
        self.assertEqual(
            {"state": "provided", "reason": None, "value": "2"}, value["user_answer"]
        )
        self.assertTrue(value["correct"])

    def test_snapshot_runner_writes_course_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            messages = []
            runner = hs.SnapshotRunner(FixtureCanvasClient(), output_root=Path(directory), now=NOW, out=messages.append)
            code = runner.run("999")
            self.assertEqual(0, code)
            status_path = Path(directory) / "26-2" / "status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual("success", status["overall_status"])
            self.assertEqual("updated", status["courses"][0]["status"])
            self.assertEqual(5, status["schema_version"])
            self.assertEqual(5, status["courses"][0]["snapshot_schema_version"])
            course_path = status_path.parent / status["courses"][0]["path"]
            self.assertTrue(course_path.exists())
            run_path = status_path.parent / status["run_archive"]["path"]
            self.assertEqual("committed", status["run_archive"]["status"])
            self.assertTrue((run_path / "status.json").is_file())
            self.assertEqual(course_path.read_bytes(), (run_path / course_path.name).read_bytes())
            self.assertFalse((run_path / "files").exists())
            payload = json.loads(course_path.read_text(encoding="utf-8"))
            self.assertNotIn("Private User", json.dumps(payload, ensure_ascii=False))
            self.assertEqual(hs.document_summary(payload), status["courses"][0]["documents"])
            self.assertTrue(any(
                hs.document_summary_text(status["courses"][0]["documents"]) in message
                for message in messages
            ))
            self.assertEqual(
                f"[완료] {status['overall_status']} | 종료 코드 {status['exit_code']} | "
                f"{status['started_at']} → {status['ended_at']}",
                messages[-1],
            )

    def test_optional_document_metadata_failure_is_partial_success(self):
        class DocumentFailureClient(FixtureCanvasClient):
            def get_json(self, path, params=None):
                if path == "/api/v1/files/700":
                    raise hs.CanvasHTTPError(500, path)
                return super().get_json(path, params)

        with tempfile.TemporaryDirectory() as directory:
            code = hs.SnapshotRunner(
                DocumentFailureClient(), output_root=Path(directory), now=NOW, clock=lambda: NOW, out=lambda _: None
            ).run("999")
            self.assertEqual(2, code)
            status_path = Path(directory) / "26-2" / "status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual("partial_failure", status["overall_status"])
            self.assertEqual("updated_with_warnings", status["courses"][0]["status"])
            course = json.loads((status_path.parent / status["courses"][0]["path"]).read_text(encoding="utf-8"))
            self.assertEqual("unavailable", course["documents"]["canvas-file:700"]["access_state"])

    def test_quiz_wrapper_gets_placeholder_when_endpoint_is_unavailable(self):
        class QuizUnavailableClient(FixtureCanvasClient):
            def get_paginated(self, path, params=None):
                if path == "/api/v1/courses/101/quizzes":
                    raise hs.CanvasHTTPError(404, path)
                return super().get_paginated(path, params)

        client = QuizUnavailableClient()
        selection = hs.select_current_term(client.get_paginated("/api/v1/courses"), NOW)
        data, _ = hs.CanvasCollector(client, now=NOW, user_id="999").collect_course(
            selection.courses[0], selection
        )
        classic = next(quiz for quiz in data["quizzes"] if quiz["id"] == "601")
        self.assertEqual("not_available", classic["access"]["state"])
        references = [
            item["id"]
            for group in data["assignment_groups"]
            for item in group["items"]
            if item["kind"] == "quiz"
        ]
        self.assertIn(classic["id"], references)

    def test_existing_document_saved_path_survives_provider_rename(self):
        class RenamedDocumentClient(FixtureCanvasClient):
            def get_json(self, path, params=None):
                if path == "/api/v1/files/700":
                    value = dict(super().get_json(path, params))
                    value["display_name"] = "새 이름.docx"
                    return value
                return super().get_json(path, params)

        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            term.mkdir()
            course_path = term / "자료구조_ 실습__c101.json"
            course_path.write_text(
                json.dumps(
                    {
                        "course": {"last_success_at": "old"},
                        "documents": {
                            "canvas-file:700": {
                                "provider_filename": "옛 이름.docx",
                                "saved_filename": "canvas-file-700__옛 이름.docx",
                                "saved_path": "files/자료구조_ 실습__c101/canvas-file-700__옛 이름.docx",
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            code = hs.SnapshotRunner(
                RenamedDocumentClient(), output_root=Path(directory), now=NOW, clock=lambda: NOW, out=lambda _: None
            ).run("999")
            self.assertEqual(0, code)
            updated = json.loads(course_path.read_text(encoding="utf-8"))
            document = updated["documents"]["canvas-file:700"]
            self.assertEqual("새 이름.docx", document["provider_filename"])
            self.assertEqual("canvas-file-700__옛 이름.docx", document["saved_filename"])
            self.assertIn("옛 이름.docx", document["saved_path"])

    def test_course_failure_preserves_existing_file(self):
        class FailingClient(FixtureCanvasClient):
            def get_paginated(self, path, params=None):
                if path == "/api/v1/announcements":
                    raise hs.CanvasTransportError("canvas_transport_error", "offline")
                return super().get_paginated(path, params)

        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            term.mkdir()
            old = term / "자료구조_ 실습__c101.json"
            old.write_text('{"course":{"last_success_at":"old"}}\n', encoding="utf-8")
            messages = []
            code = hs.SnapshotRunner(
                FailingClient(), output_root=Path(directory), now=NOW,
                clock=lambda: NOW, out=messages.append,
            ).run("999")
            self.assertEqual(1, code)
            self.assertEqual('{"course":{"last_success_at":"old"}}\n', old.read_text(encoding="utf-8"))
            status = json.loads((term / "status.json").read_text(encoding="utf-8"))
            self.assertEqual("kept", status["courses"][0]["status"])
            self.assertEqual("canvas_transport_error", status["courses"][0]["error_code"])
            self.assertIsNone(status["courses"][0]["documents"])
            self.assertEqual(
                f"[완료] {status['overall_status']} | 종료 코드 {status['exit_code']} | "
                f"{status['started_at']} → {status['ended_at']}",
                messages[-1],
            )

    def test_discussion_list_failure_preserves_schema_one_snapshot(self):
        class DiscussionListFailureClient(FixtureCanvasClient):
            def get_paginated(self, path, params=None):
                if path == "/api/v1/courses/101/discussion_topics":
                    raise hs.CanvasTransportError("canvas_transport_error", "offline")
                return super().get_paginated(path, params)

        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            term.mkdir()
            old = term / "자료구조_ 실습__c101.json"
            old.write_text(
                '{"schema_version":1,"course":{"last_success_at":"old"}}\n', encoding="utf-8"
            )
            code = hs.SnapshotRunner(
                DiscussionListFailureClient(),
                output_root=Path(directory),
                now=NOW,
                clock=lambda: NOW,
                out=lambda _: None,
            ).run("999")
            self.assertEqual(1, code)
            self.assertEqual(
                '{"schema_version":1,"course":{"last_success_at":"old"}}\n',
                old.read_text(encoding="utf-8"),
            )
            status = json.loads((term / "status.json").read_text(encoding="utf-8"))
            self.assertEqual("kept", status["courses"][0]["status"])
            self.assertEqual(1, status["courses"][0]["snapshot_schema_version"])

    def test_no_courses_writes_no_courses_status(self):
        class EmptyClient:
            def get_paginated(self, path, params=None):
                return []

        with tempfile.TemporaryDirectory() as directory:
            messages = []
            code = hs.SnapshotRunner(
                EmptyClient(), output_root=Path(directory), now=NOW,
                clock=lambda: NOW, out=messages.append,
            ).run("999")
            self.assertEqual(0, code)
            status = json.loads((Path(directory) / "26-2" / "status.json").read_text(encoding="utf-8"))
            self.assertEqual("no_courses", status["overall_status"])
            run = Path(directory) / "26-2" / status["run_archive"]["path"]
            self.assertEqual(["status.json"], sorted(path.name for path in run.iterdir()))
            self.assertEqual(
                f"[완료] {status['overall_status']} | 종료 코드 {status['exit_code']} | "
                f"{status['started_at']} → {status['ended_at']}",
                messages[-1],
            )


class SnapshotRunArchiveTest(unittest.TestCase):
    def test_unarchived_current_snapshot_is_preserved_once(self):
        with tempfile.TemporaryDirectory() as directory:
            term = Path(directory) / "26-2"
            term.mkdir()
            course = term / "course__c101.json"
            course.write_text('{"schema_version":4,"course":{"id":"101"}}\n', encoding="utf-8")
            status = {
                "schema_version": 4,
                "term": {"id": "26-2", "name": "2026년 2학기", "canvas_id": "1"},
                "started_at": NOW.isoformat(timespec="seconds"),
                "ended_at": NOW.isoformat(timespec="seconds"),
                "overall_status": "success",
                "exit_code": 0,
                "courses": [{"id": "101", "path": course.name, "status": "updated"}],
            }
            hs.atomic_write_json(term / "status.json", status)

            original = course.read_bytes()
            first = storage_module.archive_current_snapshot(term)
            course.write_text('{"interrupted":true}\n', encoding="utf-8")
            second = storage_module.archive_current_snapshot(term)
            root_status = json.loads((term / "status.json").read_text(encoding="utf-8"))

            self.assertEqual(first, second)
            self.assertEqual(1, len([path for path in (term / "runs").iterdir() if not path.name.startswith(".")]))
            self.assertEqual(5, root_status["schema_version"])
            self.assertEqual("committed", root_status["run_archive"]["status"])
            self.assertEqual(original, (first / course.name).read_bytes())
            self.assertFalse((first / "files").exists())

    def test_same_second_runs_get_suffix_without_overwriting_first(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first_runner = hs.SnapshotRunner(
                FixtureCanvasClient(), output_root=output, now=NOW, clock=lambda: NOW, out=lambda _: None
            )
            self.assertEqual(0, first_runner.run("999"))
            runs = output / "26-2" / "runs"
            first = runs / "20260902T120000+0900"
            first_hash = (first / "status.json").read_bytes()

            second_runner = hs.SnapshotRunner(
                FixtureCanvasClient(), output_root=output, now=NOW, clock=lambda: NOW, out=lambda _: None
            )
            self.assertEqual(0, second_runner.run("999"))

            self.assertTrue(first.is_dir())
            self.assertTrue((runs / "20260902T120000+0900__02").is_dir())
            self.assertEqual(first_hash, (first / "status.json").read_bytes())

    def test_archive_failure_is_exit_two_and_exposes_no_run(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "hylms.storage.archive_snapshot_run", side_effect=OSError("disk")
        ):
            code = hs.SnapshotRunner(
                FixtureCanvasClient(), output_root=Path(directory), now=NOW,
                clock=lambda: NOW, out=lambda _: None,
            ).run("999")
            term = Path(directory) / "26-2"
            status = json.loads((term / "status.json").read_text(encoding="utf-8"))

            self.assertEqual(2, code)
            self.assertEqual("failed", status["run_archive"]["status"])
            self.assertEqual("snapshot_archive_failed", status["run_archive"]["error_code"])
            self.assertFalse((term / "runs").exists())


class SnapshotIntegrityTest(unittest.TestCase):
    def valid_data(self):
        client = FixtureCanvasClient()
        selection = hs.select_current_term(client.get_paginated("/api/v1/courses"), NOW)
        data = hs.CanvasCollector(client, now=NOW, user_id="999").collect_course(
            selection.courses[0], selection
        )[0]
        hs.LearningXCollector(FakeLearningXSession(), now=NOW).enrich(data)
        hs.finalize_document_paths(data)
        return data

    def test_complete_snapshot_integrity_passes(self):
        hs.validate_snapshot(self.valid_data())

    def test_each_canonical_corruption_fails_closed(self):
        def group_reference(data):
            data["assignment_groups"][0]["items"][0]["id"] = "missing"

        def weekly_link(data):
            next(
                item for item in data["weekly_learning"]
                if item["linked_entity"]["state"] == "linked"
            )["linked_entity"]["id"] = "missing"

        def forward_document_reference(data):
            data["weekly_learning"][0]["document_refs"] = ["missing"]

        def reverse_document_reference(data):
            next(iter(data["documents"].values()))["references"][0]["source_id"] = "missing"

        def duplicate_path(data):
            documents = list(data["documents"].values())
            documents[1]["saved_path"] = documents[0]["saved_path"]

        def duplicate_id(data):
            data["weekly_learning"][1]["id"] = data["weekly_learning"][0]["id"]

        def source_count(data):
            data["sources"]["weekly_learning"]["normalized_count"] += 1

        def outcome_count(data):
            for document in data["documents"].values():
                document["download_state"] = "existing"
            data["sources"]["documents"]["existing_count"] = 0

        for name, corrupt in (
            ("group_reference", group_reference),
            ("weekly_link", weekly_link),
            ("forward_document_reference", forward_document_reference),
            ("reverse_document_reference", reverse_document_reference),
            ("duplicate_path", duplicate_path),
            ("duplicate_id", duplicate_id),
            ("source_count", source_count),
            ("outcome_count", outcome_count),
        ):
            with self.subTest(name=name):
                data = self.valid_data()
                corrupt(data)
                with self.assertRaises(hs.HylmsError) as caught:
                    hs.validate_snapshot(data)
                self.assertEqual("snapshot_integrity_failed", caught.exception.code)

    def test_document_eligibility_mismatch_remains_a_warning_contract(self):
        data = self.valid_data()
        data["sources"]["documents"]["eligible_count"] += 1
        hs.validate_snapshot(data)

    def test_each_source_count_corruption_fails_closed(self):
        cases = (
            ("canvas_course", "normalized_count"),
            ("syllabus", "normalized_count"),
            ("announcements", "normalized_count"),
            ("announcements", "discovered_count"),
            ("assignment_groups", "normalized_count"),
            ("assignments", "normalized_count"),
            ("classic_quizzes", "normalized_count"),
            ("new_quizzes", "normalized_count"),
            ("canvas_discussions", "graded_count"),
            ("canvas_assessments", "assignment_count"),
            ("canvas_assessments", "discovered_count"),
            ("weekly_learning", "normalized_count"),
            ("course_resources", "unknown_type_count"),
            ("learningx", "resource_count"),
            ("documents", "registered_count"),
        )
        for source, field in cases:
            with self.subTest(source=source, field=field):
                data = self.valid_data()
                data["sources"][source][field] += 1
                with self.assertRaises(hs.HylmsError) as caught:
                    hs.validate_snapshot(data)
                self.assertEqual("snapshot_integrity_failed", caught.exception.code)

        for value in (-1, True):
            with self.subTest(invalid_count=value):
                data = self.valid_data()
                data["sources"]["announcements"]["discovered_count"] = value
                with self.assertRaises(hs.HylmsError):
                    hs.validate_snapshot(data)

    def test_quiz_discussion_assessment_and_learningx_warning_counts_are_valid(self):
        client = BuildTwoDiscussionClient()
        selection = hs.select_current_term(client.get_paginated("/api/v1/courses"), NOW)
        discussion_data = hs.CanvasCollector(client, now=NOW, user_id="999").collect_course(
            selection.courses[0], selection
        )[0]
        hs.LearningXCollector(FakeLearningXSession(), now=NOW).enrich(discussion_data)
        hs.finalize_document_paths(discussion_data)
        hs.validate_snapshot(discussion_data)

        data = self.valid_data()
        hs.LearningXCollector(
            FakeLearningXSession(
                fail_detail=True, unresolved=True, unknown=True, unknown_resource=True
            ),
            now=NOW,
        ).enrich(data)
        hs.finalize_document_paths(data)
        hs.validate_snapshot(data)

    def test_integrity_failure_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "hylms.storage.validate_snapshot",
            side_effect=hs.HylmsError("snapshot_integrity_failed", "safe"),
        ):
            term = Path(directory) / "26-2"
            term.mkdir()
            old = term / "자료구조_ 실습__c101.json"
            original = '{"schema_version":3,"course":{"last_success_at":"old"}}\n'
            old.write_text(original, encoding="utf-8")
            messages = []
            code = hs.SnapshotRunner(
                FixtureCanvasClient(), output_root=Path(directory), now=NOW,
                clock=lambda: NOW, out=messages.append,
            ).run("999")
            status = json.loads((term / "status.json").read_text(encoding="utf-8"))
            preserved = old.read_text(encoding="utf-8")
        self.assertEqual(1, code)
        self.assertEqual(original, preserved)
        self.assertEqual("kept", status["courses"][0]["status"])
        self.assertEqual("snapshot_integrity_failed", status["courses"][0]["error_code"])

    def test_fixture_snapshot_contains_no_secret_or_profile_fields(self):
        data = self.valid_data()
        serialized = json.dumps(data, ensure_ascii=False)
        for value in (
            "xn-secret-must-not-leak", "private-user-id", "private-login",
            "private@example.com", "must-not-leak", "verifier=secret",
        ):
            self.assertNotIn(value, serialized)
        self.assertIsNone(re.search(r"[?&](?:token|signature|sig|auth|key|expires)=", serialized, re.I))

        forbidden_keys = {"xn_api_token", "oauth_signature", "phpsessid", "user_id", "user_login", "email"}

        def walk(value):
            if isinstance(value, dict):
                self.assertTrue(forbidden_keys.isdisjoint(key.lower() for key in value))
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(data)


class ApplicationTest(unittest.TestCase):
    def record(self, token="1~old", expires="2026-12-01T23:59:59+09:00"):
        return hs.CredentialRecord(token, hs.extract_token_id(token), expires)

    def test_auth_check_validates_without_changing_credential_or_snapshots(self):
        store = FakeStore(self.record())
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshots"
            app = hs.Application(
                credential_store=store,
                client_factory=lambda token: AuthOnlyClient(token),
                output_root=output,
                now=lambda: NOW,
                out=messages.append,
            )
            self.assertEqual(0, app.run(["auth", "check"]))
            self.assertFalse(output.exists())
        self.assertEqual(1, store.self_tests)
        self.assertEqual([], store.writes)
        self.assertFalse(store.deleted)
        self.assertEqual("1~old", store.record.token)
        self.assertEqual(
            ["인증 확인: Credential Manager 정상, Canvas PAT 유효, 만료일 2026-12-01 (D-90)"],
            messages,
        )
        output_text = "\n".join(messages)
        self.assertNotIn("1~old", output_text)
        self.assertNotIn("must-not-be-saved", output_text)
        self.assertNotIn("secret@example.com", output_text)

    def test_auth_check_missing_rejected_and_self_test_failure_are_safe(self):
        cases = (
            ("missing", FakeStore(), lambda token: AuthOnlyClient(token), "auth_missing", 0),
            ("rejected", FakeStore(self.record()), lambda token: AuthOnlyClient(token, invalid=True), "auth_rejected", 1),
            ("self_test", FakeStore(self.record()), lambda token: self.fail("client must not be created"), "credential_self_test_failed", 1),
        )
        cases[2][1].fail_self_test = True
        for name, store, factory, error_code, self_tests in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "snapshots"
                messages = []
                app = hs.Application(
                    credential_store=store,
                    client_factory=factory,
                    output_root=output,
                    now=lambda: NOW,
                    out=messages.append,
                )
                self.assertEqual(1, app.run(["auth", "check"]))
                self.assertTrue(any(error_code in message for message in messages))
                self.assertEqual(self_tests, store.self_tests)
                self.assertEqual([], store.writes)
                self.assertFalse(store.deleted)
                self.assertFalse(output.exists())

    def test_auth_check_reports_d7_and_elapsed_expiry_without_failing_valid_pat(self):
        for expiry, expected in (
            ("2026-09-05T23:59:59+09:00", "D-3"),
            ("2026-09-01T23:59:59+09:00", "D+1"),
        ):
            with self.subTest(expiry=expiry):
                messages = []
                app = hs.Application(
                    credential_store=FakeStore(self.record(expires=expiry)),
                    client_factory=lambda token: AuthOnlyClient(token),
                    now=lambda: NOW,
                    out=messages.append,
                )
                self.assertEqual(0, app.run(["auth", "check"]))
                self.assertTrue(any(expected in message for message in messages))
                self.assertTrue(any("7일 이내" in message for message in messages))

    def test_usage_lists_both_auth_commands(self):
        messages = []
        app = hs.Application(credential_store=FakeStore(), now=lambda: NOW, out=messages.append)
        self.assertEqual(1, app.run(["unknown"]))
        self.assertEqual(["사용법: py hylms_snapshot.py [auth check|auth rotate]"], messages)

    def test_rotate_validates_stores_readback_and_revokes_old(self):
        old = self.record()
        store = FakeStore(old)
        revoked = []

        def factory(token):
            return AuthOnlyClient(token, revoke_log=revoked)

        messages = []
        app = hs.Application(
            credential_store=store,
            client_factory=factory,
            now=lambda: NOW,
            input_secret=lambda _: "2~new",
            input_text=lambda _: "2026-12-01",
            out=messages.append,
        )
        self.assertEqual(0, app.run(["auth", "rotate"]))
        self.assertEqual("2~new", store.record.token)
        self.assertEqual(["1~old"], revoked)
        self.assertEqual(1, store.self_tests)
        self.assertFalse(any("2~new" in message for message in messages))

    def test_invalid_new_token_does_not_write(self):
        old = self.record()
        store = FakeStore(old)

        def factory(token):
            return AuthOnlyClient(token, invalid=token == "2~new")

        messages = []
        app = hs.Application(
            credential_store=store,
            client_factory=factory,
            now=lambda: NOW,
            input_secret=lambda _: "2~new",
            input_text=lambda _: "2026-12-01",
            out=messages.append,
        )
        self.assertEqual(1, app.run(["auth", "rotate"]))
        self.assertEqual(old, store.record)
        self.assertEqual([], store.writes)
        self.assertTrue(any("auth_rejected" in message for message in messages))

    def test_credential_write_and_self_test_failures_keep_old(self):
        for failure in ("write", "self_test"):
            with self.subTest(failure=failure):
                old = self.record()
                store = FakeStore(old)
                store.fail_write = failure == "write"
                store.fail_self_test = failure == "self_test"
                app = hs.Application(
                    credential_store=store,
                    client_factory=lambda token: AuthOnlyClient(token),
                    now=lambda: NOW,
                    input_secret=lambda _: "2~new",
                    input_text=lambda _: "2026-12-01",
                    out=lambda _: None,
                )
                self.assertEqual(1, app.run(["auth", "rotate"]))
                self.assertEqual(old, store.record)

    def test_rotate_readback_mismatch_rolls_back(self):
        old = self.record()
        store = FakeStore(old)
        store.read_override = old  # First read for rotate sees the old record.

        # Make the second read return a different value after the new write.
        original_write = store.write

        def write_with_bad_readback(record):
            original_write(record)
            if record.token == "2~new":
                store.read_override = old

        store.write = write_with_bad_readback
        app = hs.Application(
            credential_store=store,
            client_factory=lambda token: AuthOnlyClient(token),
            now=lambda: NOW,
            input_secret=lambda _: "2~new",
            input_text=lambda _: "2026-12-01",
            out=lambda _: None,
        )
        self.assertEqual(1, app.run(["auth", "rotate"]))
        self.assertEqual(old, store.record)

    def test_revoke_failure_keeps_new_and_returns_two(self):
        old = self.record()
        store = FakeStore(old)

        def factory(token):
            return AuthOnlyClient(token, revoke_fails=token == old.token)

        messages = []
        app = hs.Application(
            credential_store=store,
            client_factory=factory,
            now=lambda: NOW,
            input_secret=lambda _: "2~new",
            input_text=lambda _: "2026-12-01",
            out=messages.append,
        )
        self.assertEqual(2, app.run(["auth", "rotate"]))
        self.assertEqual("2~new", store.record.token)
        self.assertTrue(any("직접 정리" in message for message in messages))

    def test_invalid_stored_token_does_not_touch_snapshots(self):
        store = FakeStore(self.record())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshots"
            messages = []
            app = hs.Application(
                credential_store=store,
                client_factory=lambda token: AuthOnlyClient(token, invalid=True),
                output_root=output,
                now=lambda: NOW,
                out=messages.append,
            )
            self.assertEqual(1, app.run([]))
            self.assertFalse(output.exists())
            self.assertEqual("1~old", store.record.token)
            self.assertTrue(any("auth rotate" in message for message in messages))

    def test_first_run_prompts_saves_and_writes_no_courses(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as directory:
            app = hs.Application(
                credential_store=store,
                client_factory=lambda token: AuthOnlyClient(token, no_courses=True),
                output_root=Path(directory),
                now=lambda: NOW,
                input_secret=lambda _: "3~bootstrap",
                input_text=lambda _: "",
                out=lambda _: None,
            )
            self.assertEqual(0, app.run([]))
            self.assertEqual("3~bootstrap", store.record.token)
            self.assertTrue((Path(directory) / "26-2" / "status.json").exists())

    def test_expiry_warning_does_not_change_success(self):
        store = FakeStore(self.record(expires="2026-09-05T23:59:59+09:00"))
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            app = hs.Application(
                credential_store=store,
                client_factory=lambda token: AuthOnlyClient(token, no_courses=True),
                output_root=Path(directory),
                now=lambda: NOW,
                out=messages.append,
            )
            self.assertEqual(0, app.run([]))
        self.assertTrue(any("7일 이내" in message for message in messages))

    def test_cancelled_first_input_leaves_store_untouched(self):
        store = FakeStore()

        def cancelled(_):
            raise KeyboardInterrupt

        app = hs.Application(
            credential_store=store,
            client_factory=lambda token: AuthOnlyClient(token),
            now=lambda: NOW,
            input_secret=cancelled,
            out=lambda _: None,
        )
        self.assertEqual(1, app.run([]))
        self.assertIsNone(store.record)
        self.assertEqual(0, store.self_tests)


if __name__ == "__main__":
    unittest.main()
