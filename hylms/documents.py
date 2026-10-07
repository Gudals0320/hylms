"""Canonical Canvas and HYCMS document downloads."""

from __future__ import annotations

import http.client
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from .core import CanvasHTTPError, DOCUMENT_EXTENSIONS, HylmsError
from .http import CanvasClient, canonical_origin, retry_after_seconds
from .learningx import LearningXHTTPError, LearningXSession


class _CanvasDownloadRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> urllib.request.Request | None:
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.HTTPError(req.full_url, 470, "unsafe redirect blocked", headers, fp)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and canonical_origin(newurl) != canonical_origin(req.full_url):
            redirected.remove_header("Authorization")
        return redirected


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: str) -> None:
        self.origin = canonical_origin(origin)

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> urllib.request.Request | None:
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https" or canonical_origin(newurl) != self.origin:
            raise urllib.error.HTTPError(req.full_url, 470, "cross-origin redirect blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _DocumentError(HylmsError):
    def __init__(self, code: str, *, restricted: bool = False) -> None:
        super().__init__(code, "문서를 다운로드하지 못했습니다.")
        self.restricted = restricted


def _is_login_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return (
        host.endswith(".hanyang.ac.kr") and path.startswith("/login")
    ) or (
        host.endswith(".hanyang.ac.kr")
        and (host.startswith("sso.") or "/sso" in path or "/saml" in path)
    )


def _is_html(value: bytes) -> bool:
    start = value.lstrip().lower()
    return start.startswith(b"<!doctype html") or start.startswith(b"<html")


def _is_html_file(path: Path) -> bool:
    with path.open("rb") as handle:
        return _is_html(handle.read(512))


class DocumentDownloader:
    def __init__(
        self,
        canvas: CanvasClient,
        learningx: LearningXSession,
        *,
        canvas_opener: Any | None = None,
        hycms_opener: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.canvas = canvas
        self.learningx = learningx
        self.canvas_opener = canvas_opener or urllib.request.build_opener(_CanvasDownloadRedirectHandler())
        self.hycms_opener = hycms_opener
        self.hycms_origin: tuple[str, str, int | None] | None = None
        self.sleep = sleep

    def download(self, data: dict[str, Any], term_directory: Path) -> list[str]:
        documents = data.get("documents") or {}
        self._remove_orphaned_canvas_html(documents, data, term_directory)
        source = data["sources"]["documents"]
        previous_warning = source.get("completeness") == "with_warnings"
        counts = {state: 0 for state in ("downloaded", "existing", "failed", "restricted")}
        warnings: list[str] = []

        for document in documents.values():
            try:
                state = self._download_one(document, data, term_directory)
                error_code = None
            except _DocumentError as exc:
                state = "restricted" if exc.restricted else "failed"
                error_code = exc.code
            except LearningXHTTPError as exc:
                state = "restricted" if exc.status in {401, 403, 404} else "failed"
                error_code = f"document_http_{exc.status}"
            except CanvasHTTPError as exc:
                state = "restricted" if exc.status in {401, 403, 404} else "failed"
                error_code = f"document_http_{exc.status}"
            except HylmsError as exc:
                state, error_code = "failed", exc.code
            except (OSError, http.client.HTTPException):
                state, error_code = "failed", "filesystem_error"

            document["download_state"] = state
            document["error_code"] = error_code
            if state == "downloaded":
                document["access_state"] = "available"
            elif state == "restricted":
                document["access_state"] = "restricted"
            counts[state] += 1

        source.update({f"{state}_count": count for state, count in counts.items()})
        registered = len(documents)
        source["registered_count"] = registered
        result_count = sum(counts.values())
        incomplete = int(source.get("eligible_count") or 0) != registered or result_count != registered
        if counts["failed"]:
            warnings.append("document_download_failed")
        if incomplete:
            source["completeness"] = "incomplete"
            warnings.append("document_registry_incomplete")
        elif counts["failed"]:
            source["completeness"] = "with_warnings"
        elif previous_warning:
            source["completeness"] = "with_warnings"
        elif counts["restricted"]:
            source["completeness"] = "restricted"
        else:
            source["completeness"] = "complete"
        return warnings

    def _remove_orphaned_canvas_html(
        self, documents: Mapping[str, Any], data: Mapping[str, Any], term_directory: Path
    ) -> None:
        root = (term_directory / "files").resolve()
        if not root.is_dir():
            return
        registered: set[Path] = set()
        for document in documents.values():
            try:
                registered.add(self._target(document, data, term_directory))
            except HylmsError:
                continue
        course_id = str((data.get("course") or {}).get("id"))
        try:
            course_directories = [
                path for path in root.iterdir()
                if path.is_dir() and not path.is_symlink() and path.name.endswith(f"__c{course_id}")
            ]
            for directory in course_directories:
                for path in directory.iterdir():
                    if (
                        path not in registered
                        and path.is_file()
                        and not path.is_symlink()
                        and path.name.startswith("canvas-file-")
                        and _is_html_file(path)
                    ):
                        path.unlink()
        except OSError as exc:
            raise _DocumentError("document_cleanup_failed") from exc

    def _download_one(self, document: dict[str, Any], data: Mapping[str, Any],
                      term_directory: Path) -> str:
        if document.get("extension") not in DOCUMENT_EXTENSIONS:
            raise _DocumentError("document_extension_rejected")
        target = self._target(document, data, term_directory)
        invalid_existing = target.is_file() and _is_html_file(target)
        if (
            document.get("download_state") in {"downloaded", "existing"}
            and target.is_file()
            and not invalid_existing
        ):
            return "existing"

        try:
            provider = document.get("provider")
            if provider == "canvas":
                url = self._canvas_url(document)
                headers = {
                    "Authorization": f"Bearer {self.canvas.token}",
                    "User-Agent": "hylms-snapshot/4",
                }
                self._save_url(self.canvas_opener, url, target, headers)
            elif provider == "hycms":
                viewer_url, download_url = self._hycms_urls(document)
                opener = self._get_hycms_opener(viewer_url)
                viewer_final = urllib.parse.urlsplit(self._read_url(opener, viewer_url))
                referer = urllib.parse.urlunsplit(
                    (viewer_final.scheme, viewer_final.netloc, viewer_final.path, "", "")
                )
                self._save_url(
                    opener,
                    download_url,
                    target,
                    {"User-Agent": "Mozilla/5.0 hylms-snapshot/4", "Referer": referer},
                )
            else:
                raise _DocumentError("document_provider_invalid")
        except BaseException:
            if invalid_existing:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            raise
        return "downloaded"

    @staticmethod
    def _target(document: Mapping[str, Any], data: Mapping[str, Any], term_directory: Path) -> Path:
        saved_path = document.get("saved_path")
        saved_filename = document.get("saved_filename")
        if not isinstance(saved_path, str) or not isinstance(saved_filename, str):
            raise _DocumentError("document_path_invalid")
        relative = Path(saved_path)
        if relative.is_absolute() or Path(saved_filename).name != saved_filename:
            raise _DocumentError("document_path_invalid")
        root = (term_directory / "files").resolve()
        target = (term_directory / relative).resolve()
        try:
            parts = target.relative_to(root).parts
        except ValueError as exc:
            raise _DocumentError("document_path_invalid") from exc
        course_id = str((data.get("course") or {}).get("id"))
        if len(parts) != 2 or not parts[0].endswith(f"__c{course_id}") or parts[1] != saved_filename:
            raise _DocumentError("document_path_invalid")
        return target

    def _canvas_url(self, document: Mapping[str, Any]) -> str:
        provider_id = document.get("provider_id")
        if provider_id is None or not str(provider_id).strip():
            raise _DocumentError("canvas_metadata_invalid")
        provider_id = urllib.parse.quote(str(provider_id).strip(), safe="")
        metadata = self.canvas.get_json(f"/api/v1/files/{provider_id}")
        if not isinstance(metadata, dict):
            raise _DocumentError("canvas_metadata_invalid")
        url = metadata.get("url")
        if not isinstance(url, str) or urllib.parse.urlsplit(url).scheme.lower() != "https":
            raise _DocumentError("canvas_download_url_invalid")
        if canonical_origin(url) != canonical_origin(self.canvas.origin):
            raise _DocumentError("canvas_download_url_invalid")
        if urllib.parse.urlsplit(url).path.rstrip("/") != f"/files/{provider_id}/download":
            raise _DocumentError("canvas_download_url_invalid")
        return url

    def _hycms_urls(self, document: Mapping[str, Any]) -> tuple[str, str]:
        content_id = urllib.parse.quote(str(document.get("provider_id")), safe="")
        payload = self.learningx.get_json(
            "/learningx/api/v1/commons/contents", {"content_id": content_id}
        )
        containers = [payload]
        if isinstance(payload, dict):
            containers.extend(
                payload.get(key) for key in ("result", "data", "content", "commons_content")
            )
        for value in containers:
            if not isinstance(value, dict):
                continue
            viewer = value.get("viewer_url") or value.get("view_url")
            download = value.get("download_url") or value.get("file_url")
            if isinstance(viewer, str) and isinstance(download, str):
                if not self._safe_hycms_pair(viewer, download):
                    raise _DocumentError("hycms_download_url_invalid")
                return viewer, download
        raise _DocumentError("hycms_metadata_invalid")

    @staticmethod
    def _safe_hycms_pair(viewer: str, download: str) -> bool:
        return (
            urllib.parse.urlsplit(viewer).scheme.lower() == "https"
            and urllib.parse.urlsplit(download).scheme.lower() == "https"
            and canonical_origin(viewer) == canonical_origin(download)
        )

    def _get_hycms_opener(self, viewer_url: str) -> Any:
        origin = canonical_origin(viewer_url)
        if self.hycms_origin is not None and origin != self.hycms_origin:
            raise _DocumentError("hycms_download_url_invalid")
        self.hycms_origin = origin
        if self.hycms_opener is None:
            cookie_jar = next(
                (handler.cookiejar for handler in self.learningx.opener.handlers if hasattr(handler, "cookiejar")),
                None,
            )
            if cookie_jar is None:
                raise _DocumentError("hycms_session_missing")
            self.hycms_opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cookie_jar),
                _SameOriginRedirectHandler(viewer_url),
            )
        return self.hycms_opener

    def _open(self, opener: Any, url: str, headers: Mapping[str, str]) -> Any:
        request = urllib.request.Request(url, headers=dict(headers))
        for attempt in range(3):
            try:
                response = opener.open(request, timeout=30.0)
            except urllib.error.HTTPError as exc:
                if exc.code in {429} or 500 <= exc.code <= 599:
                    if attempt < 2:
                        self.sleep(retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None, 60.0))
                        continue
                if exc.code == 470:
                    raise _DocumentError("document_redirect_rejected") from exc
                raise _DocumentError(
                    f"document_http_{exc.code}", restricted=exc.code in {401, 403, 404}
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 2:
                    self.sleep(float(2**attempt))
                    continue
                raise _DocumentError("document_transport_error") from exc
            status = int(getattr(response, "status", 200))
            if status == 429 or 500 <= status <= 599:
                response.close()
                if attempt < 2:
                    headers_value = getattr(response, "headers", {})
                    self.sleep(retry_after_seconds(headers_value.get("Retry-After"), 60.0))
                    continue
            if not 200 <= status <= 299:
                response.close()
                raise _DocumentError(f"document_http_{status}", restricted=status in {401, 403, 404})
            final_url = response.geturl() if hasattr(response, "geturl") else url
            if _is_login_url(final_url):
                response.close()
                raise _DocumentError("document_login_redirect")
            return response
        raise _DocumentError("document_transport_error")

    def _read_url(self, opener: Any, url: str) -> str:
        with self._open(opener, url, {"User-Agent": "Mozilla/5.0 hylms-snapshot/4"}) as response:
            try:
                response.read()
            except (OSError, http.client.HTTPException) as exc:
                raise _DocumentError("document_transport_error") from exc
            return response.geturl() if hasattr(response, "geturl") else url

    def _save_url(self, opener: Any, url: str, target: Path, headers: Mapping[str, str]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        with self._open(opener, url, headers) as response:
            response_headers = getattr(response, "headers", {})
            content_type = str(
                response_headers.get("Content-Type")
                or response_headers.get("content-type")
                or ""
            ).lower()
            try:
                first_chunk = response.read(64 * 1024)
            except (OSError, http.client.HTTPException) as exc:
                raise _DocumentError("document_transport_error") from exc
            if "text/html" in content_type or _is_html(first_chunk[:512]):
                raise _DocumentError("document_login_response")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(first_chunk)
                    while True:
                        try:
                            chunk = response.read(64 * 1024)
                        except (OSError, http.client.HTTPException) as exc:
                            raise _DocumentError("document_transport_error") from exc
                        if not chunk:
                            break
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, target)
                temporary_name = None
            finally:
                if temporary_name is not None:
                    try:
                        os.unlink(temporary_name)
                    except FileNotFoundError:
                        pass
