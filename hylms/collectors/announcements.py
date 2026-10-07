"""Announcement collection and canonical document metadata discovery."""

from __future__ import annotations

import urllib.parse
import re
from pathlib import Path
from typing import Any, Mapping

from ..content import clean_html, sanitize_filename_component, sanitize_url
from ..core import (
    CANVAS_ORIGIN,
    DOCUMENT_EXTENSIONS,
    TERM_RE,
    CanvasHTTPError,
    HylmsError,
    TermSelection,
    TermSelectionError,
    kst_iso,
)


class AnnouncementCollectorMixin:
    def _collect_announcements(
        self, course_id: str, term: TermSelection, base_url: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], list[str]]:
        announcements, candidates = self._announcement_candidates(course_id, term, base_url)
        documents, source, warnings = self._resolve_documents(course_id, candidates)
        self._filter_document_refs(announcements, documents)
        return announcements, documents, source, warnings

    def _announcement_candidates(
        self, course_id: str, term: TermSelection, base_url: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        match = TERM_RE.fullmatch(term.name)
        if not match:
            raise TermSelectionError("term_unknown", "공지 조회 기간을 계산할 수 없습니다.")
        academic_year = int(match.group(1))
        raw_announcements = self.client.get_paginated(
            "/api/v1/announcements",
            {
                "context_codes[]": [f"course_{course_id}"],
                "start_date": f"{academic_year:04d}-01-01",
                "end_date": f"{academic_year + 1:04d}-03-01",
                "per_page": 100,
            },
        )
        announcements: list[dict[str, Any]] = []
        candidates: dict[str, dict[str, Any]] = {}
        for raw in raw_announcements:
            if not isinstance(raw, dict) or raw.get("id") is None:
                raise HylmsError("announcements_invalid", "Canvas 공지 형식이 올바르지 않습니다.")
            announcement_id = str(raw["id"])
            source_url = sanitize_url(raw.get("html_url") or f"{base_url}/discussion_topics/{announcement_id}", base_url)
            message, raw_links = clean_html(raw.get("message"), source_url or base_url)
            document_refs: list[str] = []
            for attachment in raw.get("attachments") or []:
                if not isinstance(attachment, dict) or attachment.get("id") is None:
                    continue
                file_id = str(attachment["id"])
                self._add_document_candidate(
                    candidates,
                    file_id,
                    attachment.get("display_name") or attachment.get("filename"),
                    announcement_id,
                    "attachment",
                )
                document_refs.append(f"canvas-file:{file_id}")
            for raw_url in raw_links:
                absolute = urllib.parse.urljoin(base_url + "/", raw_url)
                path = urllib.parse.urlsplit(absolute).path
                file_match = re.search(r"/courses/(\d+)/files/(\d+)(?:/download)?/?$", path)
                if not file_match or file_match.group(1) != course_id:
                    continue
                file_id = file_match.group(2)
                filename_hint = Path(urllib.parse.unquote(path)).name
                if filename_hint in {file_id, "download"}:
                    filename_hint = None
                self._add_document_candidate(
                    candidates, file_id, filename_hint, announcement_id, "body_link"
                )
                document_refs.append(f"canvas-file:{file_id}")
            announcements.append(
                {
                    "id": announcement_id,
                    "title": raw.get("title"),
                    "posted_at": kst_iso(raw.get("posted_at")),
                    "delayed_post_at": kst_iso(raw.get("delayed_post_at")),
                    "lock_at": kst_iso(raw.get("lock_at")),
                    "author_display_name": (raw.get("author") or {}).get("display_name")
                    if isinstance(raw.get("author"), dict)
                    else None,
                    "message": message,
                    "source_url": source_url,
                    "document_refs": list(dict.fromkeys(document_refs)),
                }
            )

        return announcements, candidates

    @staticmethod
    def _filter_document_refs(records, documents):
        for record in records:
            record["document_refs"] = [reference for reference in record["document_refs"] if reference in documents]

    @staticmethod
    def _add_document_candidate(
        candidates: dict[str, dict[str, Any]],
        file_id: str,
        filename_hint: str | None,
        announcement_id: str,
        relation: str,
        *, source: str = "announcement",
    ) -> None:
        candidate = candidates.setdefault(
            file_id, {"filename_hint": filename_hint, "references": []}
        )
        if not candidate.get("filename_hint") and filename_hint:
            candidate["filename_hint"] = filename_hint
        reference = {"source": source, "source_id": announcement_id, "relation": relation}
        if reference not in candidate["references"]:
            candidate["references"].append(reference)

    def _resolve_documents(
        self, course_id: str, candidates: Mapping[str, Mapping[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        documents: dict[str, Any] = {}
        warnings: list[str] = []
        eligible_count = 0
        restricted_count = 0
        skipped_extension_count = 0
        for file_id, candidate in candidates.items():
            metadata: Mapping[str, Any] | None = None
            access_state = "available"
            try:
                payload = self.client.get_json(f"/api/v1/files/{file_id}")
                if not isinstance(payload, dict):
                    raise HylmsError("document_metadata_invalid", "Canvas 파일 metadata 형식이 올바르지 않습니다.")
                metadata = payload
            except CanvasHTTPError as exc:
                if exc.status in {401, 403, 404}:
                    access_state = "restricted"
                    restricted_count += 1
                    warnings.append("document_metadata_restricted")
                else:
                    access_state = "unavailable"
                    warnings.append("document_metadata_failed")
            except HylmsError:
                access_state = "unavailable"
                warnings.append("document_metadata_failed")

            provider_filename = None
            if metadata:
                provider_filename = metadata.get("display_name") or metadata.get("filename")
            provider_filename = provider_filename or candidate.get("filename_hint")
            extension = Path(str(provider_filename or "")).suffix.lower()
            if extension not in DOCUMENT_EXTENSIONS:
                skipped_extension_count += 1
                continue
            eligible_count += 1
            canonical_id = f"canvas-file:{file_id}"
            safe_name = sanitize_filename_component(str(provider_filename), max_length=100, fallback=f"file-{file_id}{extension}")
            # The caller fills the exact course directory using its canonical name.
            saved_filename = f"canvas-file-{file_id}__{safe_name}"
            documents[canonical_id] = {
                "id": canonical_id,
                "provider": "canvas",
                "provider_id": file_id,
                "provider_filename": provider_filename,
                "extension": extension,
                "content_type": metadata.get("content-type") or metadata.get("content_type") if metadata else None,
                "size": metadata.get("size") if metadata else None,
                "saved_filename": saved_filename,
                "saved_path": None,
                "stable_url": f"{CANVAS_ORIGIN}/courses/{course_id}/files/{file_id}/download",
                "access_state": access_state,
                "download_state": "not_collected",
                "error_code": None,
                "references": list(candidate.get("references") or []),
            }
        registered_count = len(documents)
        completeness = "complete"
        if eligible_count != registered_count:
            completeness = "incomplete"
            warnings.append("document_registry_incomplete")
        elif warnings:
            completeness = "with_warnings"
        return (
            documents,
            {
                "status": "collected" if candidates else "empty",
                "discovered_count": len(candidates),
                "eligible_count": eligible_count,
                "registered_count": registered_count,
                "restricted_count": restricted_count,
                "skipped_extension_count": skipped_extension_count,
                "downloaded_count": 0,
                "existing_count": 0,
                "failed_count": 0,
                "completeness": completeness,
            },
            list(dict.fromkeys(warnings)),
        )
