"""Local skill worker and bounded file exchange. No LLM service or daemon."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .core import HylmsError
from .diff import _scrub
from .execution_context import execution_context, require_execution_owner
from .google_calendar import sync_lock
from .storage import atomic_write_json as _atomic_write_json
from .workflow_adapters import EngineAdapters, error, fingerprint, pending_review, read_state
from .pending_agenda import create_agenda, extend_agenda, resolve_labels, validate_agenda

ROOT = Path(__file__).resolve().parent.parent
_SESSION = re.compile(r"[a-zA-Z0-9_-]{1,100}\Z")
_TOKEN = re.compile(r"(?<![\w$-])\$hylms(?![\w-])")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
ACTIVE = {"starting", "running", "waiting"}


@contextmanager
def mailbox_lock(path):
    deadline = time.monotonic() + 5
    while True:
        manager = sync_lock(path)
        try:
            manager.__enter__()
            break
        except HylmsError as exc:
            if exc.code != "busy" or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    try:
        yield
    finally:
        manager.__exit__(None, None, None)


def session_id(environ=None):
    env = os.environ if environ is None else environ
    value = env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID")
    if not isinstance(value, str) or not _SESSION.fullmatch(value):
        error("runtime_session_missing")
    return value


def atomic_write_json(path, value):
    # Windows readers briefly deny replacement. Retry only the local metadata
    # write, never a completed engine/API operation.
    for attempt in range(20):
        try:
            return _atomic_write_json(path, value)
        except PermissionError:
            if attempt == 19:
                error("runtime_metadata_write_failed")
            time.sleep(.02)


def read_json(path, default=None):
    for attempt in range(20):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except PermissionError:
            if attempt == 19:
                error("runtime_metadata_invalid")
            time.sleep(.02)
        except (ValueError, UnicodeError, OSError):
            error("runtime_metadata_invalid")


def alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denial is not proof the process has exited.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def clean_exchange(folder):
    for name in ("request.json", "response.json", "manual.json", "cancel.json"):
        remove_metadata(folder / name)


def remove_metadata(path):
    for attempt in range(20):
        try:
            Path(path).unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == 19:
                error("runtime_cleanup_failed")
            time.sleep(.02)


class FileExchange:
    def __init__(self, folder, session, operation, *, timeout=900, monotonic=time.monotonic,
                 sleep=time.sleep, progress=lambda **kwargs: None):
        self.folder, self.session, self.operation = Path(folder), session, operation
        self.timeout, self.monotonic, self.sleep, self.progress = timeout, monotonic, sleep, progress

    def check_cancel(self):
        if (self.folder / "cancel.json").exists():
            raise KeyboardInterrupt()

    def __call__(self, kind, payload, attempt):
        self.check_cancel()
        request = {"schema_version": 1, "session_id": self.session,
                   "operation_id": self.operation, "request_id": uuid.uuid4().hex,
                   "kind": kind, "attempt": attempt, "payload": copy.deepcopy(payload)}
        request["binding"] = fingerprint(request)
        with mailbox_lock(self.folder / "exchange"):
            remove_metadata(self.folder / "response.json")
            atomic_write_json(self.folder / "request.json", request)
        self.progress(status="waiting", stage=kind)
        deadline = self.monotonic() + self.timeout
        try:
            while self.monotonic() < deadline:
                self.check_cancel()
                with mailbox_lock(self.folder / "exchange"):
                    response = read_json(self.folder / "response.json")
                    if response is not None:
                        self.validate_response(request, response)
                        if response.get("error"):
                            error(response["error"])
                        return response["result"]
                self.sleep(0.2)
            error("runtime_model_timeout")
        finally:
            with mailbox_lock(self.folder / "exchange"):
                remove_metadata(self.folder / "request.json")
                remove_metadata(self.folder / "response.json")
            self.progress(status="running", stage=kind)

    @staticmethod
    def validate_response(request, response):
        keys = {"schema_version", "session_id", "operation_id", "request_id", "binding", "result", "error"}
        if not isinstance(response, dict) or set(response) != keys:
            error("runtime_response_invalid")
        if any(response[key] != request[key] for key in keys - {"result", "error"}):
            error("runtime_response_stale")
        code = response["error"]
        if code is not None and (not isinstance(code, str) or not _CODE.fullmatch(code)):
            error("runtime_response_invalid")
        if (code is None) == (response["result"] is None):
            error("runtime_response_invalid")


class RuntimeService:
    def __init__(self, root=ROOT, *, launcher=None, is_alive=alive, execution_check=None):
        self.root = Path(root).resolve()
        self.directory = self.root / ".hylms-runtime"
        self.launcher = launcher or self.launch
        self.is_alive = is_alive
        self.execution_check = execution_check or require_execution_owner

    def folder(self, session):
        if not isinstance(session, str) or not _SESSION.fullmatch(session):
            error("runtime_session_missing")
        return self.directory / session

    def launch(self, session, operation):
        # Popen returns promptly; stdout/stderr never contain LMS data or secrets.
        process = subprocess.Popen([sys.executable, "-m", "hylms.runtime", "_worker", session, operation],
            cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt")
        return process.pid

    def status(self, session):
        folder = self.folder(session)
        record = read_json(folder / "session.json")
        if record is None:
            return {"status": "not_started"}
        result = dict(record)
        if record["status"] in ACTIVE and not self.is_alive(record.get("pid")):
            result.update(status="interrupted", code="runtime_worker_interrupted")
            # Read-only detection; the next controlled start handles cleanup.
        if result["status"] == "waiting":
            result["request"] = read_json(folder / "request.json")
        if result["status"] == "completed":
            result["result"] = read_json(folder / "result.json")
        return result

    def start(self, session, prompt=None, *, manual=None, run_key=None):
        # Reject activation before creating even runtime files.
        if manual is None and (not isinstance(prompt, str) or not _TOKEN.search(prompt)):
            return {"status": "not_invoked"}
        if run_key is not None:
            if manual is not None or not isinstance(run_key, str) or not re.fullmatch(r"[0-9]{8}T[0-9]{4}", run_key):
                error("runtime_run_key_invalid")
            try:
                dt.datetime.strptime(run_key, "%Y%m%dT%H%M")
            except ValueError:
                error("runtime_run_key_invalid")
        # Before locks, session admission, credential lookup or any engine work.
        # A denied preflight does not consume this session's one pipeline run.
        self.execution_check()
        folder = self.folder(session)
        with sync_lock(self.directory / "control"):
            existing = self.status(session)
            reservation = self.directory / "scheduled-runs" / f"{run_key}.json" if run_key else None
            prior = read_json(reservation) if reservation else None
            if prior:
                previous = self.status(prior["session_id"])
                if previous.get("run_key") == run_key:
                    return previous
                history = read_json(self.folder(prior["session_id"]) / "history" / f"{run_key}.json")
                return history["status"] if history else {
                    "status": "interrupted", "code": "runtime_run_already_admitted",
                    "session_id": prior["session_id"], "run_key": run_key}
            if manual is None and existing["status"] != "not_started":
                if run_key is None or existing["status"] in ACTIVE:
                    if existing["status"] == "interrupted":
                        clean_exchange(folder)
                    return existing
            if manual is not None:
                if existing["status"] != "completed" or not existing.get("pipeline_completed"):
                    error("runtime_manual_not_ready")
                if not isinstance(manual, dict) or set(manual) not in (
                    {"instruction", "target_ids"}, {"instruction", "target_labels"}
                ):
                    error("runtime_manual_invalid")
                # Validate before spawning, using the existing transaction contract.
                from .diff import prepare_manual_packet
                from .core import now_kst
                state = read_state(self.root / "phase2_state.json")
                agenda = self.session_agenda(session, state)
                if "target_labels" in manual:
                    targets = resolve_labels(agenda, [item["id"] for item in state["pending"]], manual["target_labels"])
                    manual = {"instruction": manual["instruction"], "target_ids": targets}
                prepare_manual_packet(state,
                                      manual["instruction"], manual["target_ids"], now_kst().isoformat(timespec="seconds"))
            active = read_json(self.directory / "active.json")
            if active and self.is_alive(active.get("pid")):
                error("busy")
            if active:
                clean_exchange(self.folder(active["session_id"]))
            # Also detects workers not represented by a stale admission record.
            with sync_lock(self.directory / "pipeline"):
                pass
            folder.mkdir(parents=True, exist_ok=True)
            if run_key is not None and existing["status"] != "not_started":
                history_key = existing.get("run_key") or existing["operation_id"]
                atomic_write_json(folder / "history" / f"{history_key}.json", {
                    "status": existing, "agenda": read_json(folder / "pending-agenda.json")})
            clean_exchange(folder)
            operation = uuid.uuid4().hex
            record = {"schema_version": 1, "session_id": session, "operation_id": operation,
                      "mode": "manual" if manual is not None else "pipeline", "status": "starting",
                      "pipeline_completed": existing.get("pipeline_completed", False) if manual else False, "pid": None}
            if run_key is not None or existing.get("run_key"):
                record["run_key"] = run_key or existing["run_key"]
            # Reserve before launch: an interrupted admission cannot duplicate external writes,
            # including when an archived execution thread is replaced by a new thread.
            if reservation:
                atomic_write_json(reservation, {"session_id": session, "operation_id": operation, "run_key": run_key})
            atomic_write_json(folder / "session.json", record)
            if manual is None:
                remove_metadata(folder / "result.json")
                remove_metadata(folder / "pending-agenda.json")
            if manual is not None:
                atomic_write_json(folder / "manual.json", _scrub(manual))
            try:
                record["pid"] = self.launcher(session, operation)
                atomic_write_json(folder / "session.json", record)
                atomic_write_json(self.directory / "active.json", record)
            except Exception:
                record.update(status="interrupted", code="runtime_launch_failed")
                atomic_write_json(folder / "session.json", record)
                clean_exchange(folder)
                raise HylmsError("runtime_launch_failed", "runtime_launch_failed") from None
            return record

    def submit(self, session, response):
        folder = self.folder(session)
        with mailbox_lock(folder / "exchange"):
            request = read_json(folder / "request.json")
            if request is None or request["session_id"] != session:
                error("runtime_response_stale")
            FileExchange.validate_response(request, response)
            if (folder / "response.json").exists():
                error("runtime_response_duplicate")
            if request.get("kind") == "qa":
                from .qa_capabilities import QA_AVAILABILITY_ERRORS, availability_error
                if response["error"] in QA_AVAILABILITY_ERRORS:
                    observed = availability_error(read_json(folder / "qa-capabilities.json"), session)
                    if observed is None:
                        return {"status": "rejected", "code": "runtime_qa_discovery_required",
                                "reason": "inspect_tool_registry_and_record_actual_result",
                                "worker_response_written": False}
                    response = {**response, "error": observed}
            if request.get("kind") == "qa" and response["error"] is None:
                from .qa import validate_qa_verdict
                try:
                    validate_qa_verdict(response["result"], request["payload"]["packet"])
                except HylmsError as exc:
                    reasons = {
                        "QA issue가 알 수 없는 entity를 참조합니다.": "unknown_entity_id",
                        "QA issue가 알 수 없는 change를 참조합니다.": "unknown_change_id",
                        "QA verdict.qa_packet_id가 packet과 일치하지 않습니다.": "packet_id_mismatch",
                        "QA verdict.review_id가 packet과 일치하지 않습니다.": "review_id_mismatch",
                        "QA verdict attempt가 packet과 일치하지 않습니다.": "attempt_mismatch",
                    }
                    diagnostic = reasons.get(exc.message, "schema_or_privacy_violation")
                    path = folder / "qa-submit-validation.json"
                    previous = read_json(path, {})
                    count = previous.get("count", 0) if previous.get("binding") == request["binding"] and previous.get("request_id") == request["request_id"] else 0
                    atomic_write_json(path, {"binding": request["binding"], "request_id": request["request_id"],
                                             "count": count + 1, "reason": diagnostic})
                    if count == 0:
                        return {"status": "rejected", "code": "phase2_qa_invalid", "reason": diagnostic,
                                "repair_remaining": 1, "worker_response_written": False}
                    failure = {**response, "result": None, "error": "runtime_qa_format_exhausted"}
                    atomic_write_json(folder / "response.json", failure)
                    return {"status": "failed", "code": "runtime_qa_format_exhausted", "reason": diagnostic,
                            "repair_remaining": 0, "worker_response_written": True}
            if request.get("kind") == "interpret" and response["error"] is None:
                from .diff import validate_decision_submission, decision_format_diagnostic
                try:
                    validate_decision_submission(response["result"], request["payload"]["packet"])
                except HylmsError as exc:
                    diagnostic = decision_format_diagnostic(response["result"])
                    path = folder / "interpret-submit-validation.json"
                    previous = read_json(path, {})
                    count = previous.get("count", 0) if previous.get("binding") == request["binding"] and previous.get("request_id") == request["request_id"] else 0
                    atomic_write_json(path, {"binding": request["binding"], "request_id": request["request_id"],
                                             "count": count + 1, **diagnostic})
                    if count == 0:
                        return {"status": "rejected", "code": exc.code, **diagnostic,
                                "repair_remaining": 1, "worker_response_written": False}
                    failure = {**response, "result": None, "error": "runtime_interpret_format_exhausted"}
                    atomic_write_json(folder / "response.json", failure)
                    return {"status": "failed", "code": "runtime_interpret_format_exhausted", **diagnostic,
                            "repair_remaining": 0, "worker_response_written": True}
            atomic_write_json(folder / "response.json", response)
        return {"status": "submitted"}

    def cancel(self, session):
        status = self.status(session)
        if status["status"] in ACTIVE:
            atomic_write_json(self.folder(session) / "cancel.json", {"cancelled": True})
            return {"status": "cancelling"}
        return {"status": status["status"]}

    def targets(self, session):
        status = self.status(session)
        if status["status"] != "completed" or not status.get("pipeline_completed"):
            error("runtime_manual_not_ready")
        state = read_state(self.root / "phase2_state.json")
        return {key: [{field: item[field] for field in ("id", "title") if field in item}
                      for item in state[key]] for key in ("natural_events", "pending", "announcement_applications")}

    def pending(self, session):
        status = self.status(session)
        if status["status"] != "completed" or not status.get("pipeline_completed"):
            error("runtime_manual_not_ready")
        state = read_state(self.root / "phase2_state.json")
        return pending_review(state, self.session_agenda(session, state))

    def read_agenda(self, session):
        record = read_json(self.folder(session) / "pending-agenda.json")
        if record is None:
            return None
        if not isinstance(record, dict) or set(record) != {"session_id", "agenda"} or record["session_id"] != session:
            error("pending_agenda_invalid")
        validate_agenda(record["agenda"])
        return record["agenda"]

    def save_agenda(self, session, agenda):
        validate_agenda(agenda)
        atomic_write_json(self.folder(session) / "pending-agenda.json", {"session_id": session, "agenda": agenda})

    def session_agenda(self, session, state):
        # Derived display metadata only; the semantic state remains read-only.
        with mailbox_lock(self.folder(session) / "pending-agenda"):
            old = self.read_agenda(session)
            ids = {item["id"] for item in state["pending"]}
            agenda = create_agenda(ids, ids) if old is None else extend_agenda(old, ids)
            if old != agenda:
                self.save_agenda(session, agenda)
            return agenda


def worker(root, session, operation, *, engine_factory=EngineAdapters, execution_check=None):
    (execution_check or require_execution_owner)()
    service = RuntimeService(root)
    folder = service.folder(session)
    with sync_lock(service.directory / "pipeline"):
        with mailbox_lock(service.directory / "control"):
            record = read_json(folder / "session.json")
            if record is None or record["operation_id"] != operation or record["status"] != "starting":
                error("runtime_worker_stale")
            record.update(status="running", pid=os.getpid())
            atomic_write_json(folder / "session.json", record)

        def progress(**fields):
            record.update(fields)
            atomic_write_json(folder / "session.json", record)

        exchange = FileExchange(folder, session, operation, progress=progress)
        try:
            engine = engine_factory(root, exchange, check_cancel=exchange.check_cancel)
            engine.progress = progress
            engine.agenda_writer = lambda agenda: service.save_agenda(session, agenda)
            if record["mode"] == "manual":
                engine.agenda = service.read_agenda(session)
                instruction = read_json(folder / "manual.json")
                result = engine.manual(instruction["instruction"], instruction["target_ids"], session)
            else:
                result = engine.run(session)
            atomic_write_json(folder / "result.json", result)
            progress(status="completed", pipeline_completed=True)
        except (KeyboardInterrupt, SystemExit):
            progress(status="interrupted", code="runtime_cancelled")
        except Exception as exc:
            code = exc.code if isinstance(exc, HylmsError) and _CODE.fullmatch(exc.code) else "runtime_worker_failed"
            atomic_write_json(folder / "result.json", {"schema_version": 1, "status": "failed", "code": code})
            progress(status="completed", pipeline_completed=record["mode"] == "pipeline" or record["pipeline_completed"])
        finally:
            clean_exchange(folder)
            with mailbox_lock(service.directory / "control"):
                active = read_json(service.directory / "active.json")
                if active and active["operation_id"] == operation:
                    (service.directory / "active.json").unlink(missing_ok=True)


def self_check(root=ROOT):
    root = Path(root).resolve()
    state = read_state(root / "phase2_state.json")
    from .diff import validate_state_references
    validate_state_references(state, root / "snapshots" / state["term"])
    context = execution_context()
    return {"status": context["status"], "execution_context": context,
            "root": str(root), "state_schema": state["schema_version"],
            "term": state["term"], "session_available": bool(os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")),
            "external_calls": 0}


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "cancel", "targets", "pending", "self-check"):
        sub.add_parser(name)
    start = sub.add_parser("start")
    start.add_argument("--prompt-file", required=True)
    start.add_argument("--run-key", help="Scheduled occurrence in KST: YYYYMMDDTHHMM; never invent a retry key")
    sub.add_parser("submit").add_argument("--response-file", required=True)
    sub.add_parser("manual").add_argument("--instruction-file", required=True)
    private = sub.add_parser("_worker")
    private.add_argument("session")
    private.add_argument("operation")
    args = parser.parse_args(argv)
    try:
        if args.command == "self-check":
            result = self_check()
        elif args.command == "_worker":
            if args.session != session_id():
                error("runtime_session_mismatch")
            worker(ROOT, args.session, args.operation)
            return 0
        else:
            session = session_id()
            service = RuntimeService()
            if args.command == "start":
                source = staging_file(service, session, args.prompt_file, "invocation.txt")
                options = {"run_key": args.run_key} if args.run_key else {}
                result = service.start(session, source.read_text(encoding="utf-8"), **options)
                source.unlink(missing_ok=True)
            elif args.command == "submit":
                source = staging_file(service, session, args.response_file, "submission.json")
                result = service.submit(session, read_json(source))
                if result["status"] == "submitted":
                    source.unlink(missing_ok=True)
            elif args.command == "manual":
                source = staging_file(service, session, args.instruction_file, "instruction.json")
                result = service.start(session, manual=read_json(source))
                source.unlink(missing_ok=True)
            else:
                result = getattr(service, args.command)(session)
        print(json.dumps(result, ensure_ascii=False))
        if result.get("status") in {"rejected", "failed"}:
            return 1
        if args.command == "self-check" and result["status"] != "ready":
            return 1
        return 0
    except HylmsError as exc:
        print(json.dumps({"status": "failed", "code": exc.code}, ensure_ascii=False))
        return 1
    except Exception:
        print('{"status":"failed","code":"runtime_command_failed"}')
        return 1


def staging_file(service, session, value, name):
    path = Path(value).resolve()
    if path != (service.folder(session) / name).resolve():
        error("runtime_staging_path_invalid")
    return path


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
