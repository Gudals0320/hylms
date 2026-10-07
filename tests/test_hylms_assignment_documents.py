import copy
import json
import tempfile
import unittest
from pathlib import Path

from hylms.collector import CanvasCollector
from hylms.core import CANVAS_ORIGIN, CanvasHTTPError, HylmsError, select_current_term
from hylms.documents import DocumentDownloader
from hylms.storage import finalize_document_paths, validate_snapshot
from tests.test_hylms_snapshot import FixtureCanvasClient, FakeDownloadOpener, FakeDownloadResponse, NOW


PDF = b"%PDF-1.4\nfixture document\n%%EOF"


class AssignmentFileClient(FixtureCanvasClient):
    token = "fixture-token"
    origin = CANVAS_ORIGIN

    def __init__(self, *, links=None, informational=True, restricted=False, filename="자료.pdf"):
        super().__init__()
        self.links = links or ["/courses/101/files/701/download?wrap=1", "/courses/101/files/701/download"]
        self.informational, self.restricted, self.filename = informational, restricted, filename

    def get_paginated(self, path, params=None):
        result = copy.deepcopy(super().get_paginated(path, params))
        if path == "/api/v1/courses/101/assignment_groups":
            item = result[0]["assignments"][0]
            item.update(name="학습 노트", points_possible=0 if self.informational else 10,
                        omit_from_final_grade=self.informational,
                        submission_types=["none"] if self.informational else ["online_upload"],
                        description="<p>자료 안내</p>" + "".join(f'<a href="{url}">{self.filename}</a>' for url in self.links))
        return result

    def get_json(self, path, params=None):
        if path in {"/api/v1/files/700", "/api/v1/files/701"}:
            self.calls.append(("json", path, params))
            if path.endswith("701") and self.restricted:
                raise CanvasHTTPError(403, path)
            identity = path.rsplit("/", 1)[-1]
            return {"id": int(identity), "display_name": self.filename, "content-type": "application/pdf",
                    "size": len(PDF), "url": f"{CANVAS_ORIGIN}/files/{identity}/download"}
        result = copy.deepcopy(super().get_json(path, params))
        if path == "/api/v1/courses/101/assignments/501/submissions/self":
            result["body"] = '<a href="/courses/101/files/999/download">private-submission.pdf</a>'
        return result


def collect(client):
    term = select_current_term(client.get_paginated("/api/v1/courses"), NOW)
    data, warnings = CanvasCollector(client, now=NOW, user_id="999").collect_course(term.courses[0], term)
    finalize_document_paths(data)
    return data, warnings


class AssignmentDocumentTests(unittest.TestCase):
    def test_informational_assignment_file_is_registered_independently(self):
        client = AssignmentFileClient()
        data, _ = collect(client)
        assignment = next(item for item in data["assignments"] if item["id"] == "501")
        self.assertTrue(assignment["informational"])
        self.assertFalse(assignment["submission_required"])
        self.assertEqual(assignment["document_refs"], ["canvas-file:701"])
        self.assertEqual(data["documents"]["canvas-file:701"]["references"],
                         [{"source": "assignment", "source_id": "501", "relation": "body_link"}])
        self.assertEqual(data["sources"]["documents"]["discovered_count"], 2)
        self.assertEqual(sum(call[1] == "/api/v1/files/701" for call in client.calls), 1)
        self.assertFalse(any(call[1] == "/api/v1/files/999" for call in client.calls))
        validate_snapshot(data)

    def test_graded_assignment_body_files_are_also_registered(self):
        data, _ = collect(AssignmentFileClient(informational=False))
        assignment = next(item for item in data["assignments"] if item["id"] == "501")
        self.assertTrue(assignment["submission_required"])
        self.assertEqual(assignment["document_refs"], ["canvas-file:701"])

    def test_shared_announcement_and_assignment_file_resolves_once(self):
        client = AssignmentFileClient(links=["/courses/101/files/700/download"])
        data, _ = collect(client)
        self.assertEqual(len(data["documents"]), 1)
        refs = data["documents"]["canvas-file:700"]["references"]
        self.assertEqual({x["source"] for x in refs}, {"announcement", "assignment"})
        self.assertEqual(sum(call[1] == "/api/v1/files/700" for call in client.calls), 1)
        validate_snapshot(data)

    def test_foreign_and_cross_course_links_are_not_followed(self):
        client = AssignmentFileClient(links=["https://example.test/courses/101/files/701/download",
            "/courses/102/files/701/download", "https://learning.hanyang.ac.kr.example.test/courses/101/files/701/download"])
        data, _ = collect(client)
        self.assertNotIn("canvas-file:701", data["documents"])
        self.assertEqual(data["assignments"][0]["document_refs"], [])

    def test_direct_canvas_file_link_is_supported(self):
        data, _ = collect(AssignmentFileClient(links=["/files/701/download"]))
        self.assertIn("canvas-file:701", data["documents"])

    def test_restricted_metadata_keeps_reference_and_warning(self):
        data, warnings = collect(AssignmentFileClient(restricted=True))
        self.assertIn("document_metadata_restricted", warnings)
        self.assertEqual(data["documents"]["canvas-file:701"]["access_state"], "restricted")
        self.assertEqual(data["assignments"][0]["document_refs"], ["canvas-file:701"])
        validate_snapshot(data)

    def test_unsupported_file_extension_is_not_registered(self):
        data, _ = collect(AssignmentFileClient(filename="program.exe"))
        self.assertEqual(data["documents"], {})
        self.assertEqual(data["assignments"][0]["document_refs"], [])
        validate_snapshot(data)

    def test_download_and_repeat_use_one_canonical_file(self):
        client = AssignmentFileClient()
        data, _ = collect(client)
        opener = FakeDownloadOpener(lambda request: FakeDownloadResponse(PDF, url=request.full_url,
                                      headers={"Content-Type": "application/pdf"}))
        downloader = DocumentDownloader(client, None, canvas_opener=opener, sleep=lambda _: None)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(downloader.download(data, root), [])
            target = root / data["documents"]["canvas-file:701"]["saved_path"]
            self.assertEqual(target.read_bytes(), PDF)
            calls, mtime = len(opener.requests), target.stat().st_mtime_ns
            self.assertEqual(downloader.download(data, root), [])
            self.assertEqual(len(opener.requests), calls)
            self.assertEqual(target.stat().st_mtime_ns, mtime)
            self.assertEqual(data["documents"]["canvas-file:701"]["download_state"], "existing")
            validate_snapshot(data)

    def test_assignment_document_reference_integrity_and_legacy_compatibility(self):
        data, _ = collect(AssignmentFileClient())
        bad = copy.deepcopy(data)
        bad["assignments"][0]["document_refs"] = []
        with self.assertRaises(HylmsError):
            validate_snapshot(bad)
        legacy, _ = collect(FixtureCanvasClient())
        for assignment in legacy["assignments"]:
            assignment.pop("document_refs", None)
        validate_snapshot(legacy)
