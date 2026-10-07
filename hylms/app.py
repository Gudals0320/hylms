"""CLI application flow and safe PAT rotation."""

from __future__ import annotations

import datetime as dt
import getpass
import sys
from pathlib import Path
from typing import Callable, Sequence

from .core import (
    KST,
    CanvasHTTPError,
    CanvasTransportError,
    CredentialStoreError,
    HylmsError,
    TermSelectionError,
    extract_token_id,
    now_kst,
    parse_iso_datetime,
)
from .credentials import CredentialRecord, WindowsCredentialStore
from .http import CanvasClient
from .learningx import LearningXBootstrap
from .storage import SnapshotRunner

class Application:
    def __init__(
        self,
        *,
        credential_store: Any | None = None,
        client_factory: Callable[[str], CanvasClient] | None = None,
        output_root: Path | None = None,
        now: Callable[[], dt.datetime] = now_kst,
        input_text: Callable[[str], str] = input,
        input_secret: Callable[[str], str] = getpass.getpass,
        out: Callable[[str], None] = print,
    ) -> None:
        self.credential_store = credential_store
        self.client_factory = client_factory or (lambda token: CanvasClient(token))
        self.output_root = output_root or Path.cwd() / "snapshots"
        self.now = now
        self.input_text = input_text
        self.input_secret = input_secret
        self.out = out

    def _store(self) -> Any:
        if self.credential_store is None:
            self.credential_store = WindowsCredentialStore()
        return self.credential_store

    def run(self, argv: Sequence[str]) -> int:
        try:
            if list(argv) == ["auth", "check"]:
                return self.check()
            if list(argv) == ["auth", "rotate"]:
                return self.rotate()
            if argv:
                self.out("사용법: py hylms_snapshot.py [auth check|auth rotate]")
                return 1
            return self.snapshot()
        except (CredentialStoreError, TermSelectionError, CanvasTransportError, HylmsError) as exc:
            self.out(f"오류[{exc.code}]: {exc.message}")
            return 1
        except (EOFError, KeyboardInterrupt):
            self.out("입력이 취소되었습니다. 기존 credential과 스냅샷은 변경하지 않았습니다.")
            return 1
        except OSError:
            self.out("오류[filesystem_error]: 스냅샷 파일을 안전하게 기록하지 못했습니다.")
            return 1
        except Exception:
            self.out("오류[unexpected_error]: 예상하지 못한 오류로 작업을 중단했습니다.")
            return 1

    def _prompt_record(self) -> CredentialRecord:
        current = self.now().astimezone(KST)
        recommended = current.date() + dt.timedelta(days=90)
        self.out(f"권장 PAT 만료일: {recommended.isoformat()} (오늘부터 90일)")
        token = self.input_secret("Canvas PAT (화면에 표시되지 않음): ").strip()
        if not token:
            raise HylmsError("auth_input_empty", "Canvas PAT가 입력되지 않았습니다.")
        expiry_text = self.input_text(f"Canvas에서 지정한 만료일 [{recommended.isoformat()}]: ").strip()
        expiry_text = expiry_text or recommended.isoformat()
        try:
            expiry_date = dt.date.fromisoformat(expiry_text)
        except ValueError as exc:
            raise HylmsError("auth_expiry_invalid", "만료일은 YYYY-MM-DD 형식이어야 합니다.") from exc
        if expiry_date < current.date():
            raise HylmsError("auth_expiry_invalid", "이미 지난 만료일은 저장할 수 없습니다.")
        expires_at = dt.datetime.combine(expiry_date, dt.time(23, 59, 59), tzinfo=KST).isoformat(timespec="seconds")
        return CredentialRecord(token=token, token_id=extract_token_id(token), expires_at=expires_at)

    def _install_record(self, new_record: CredentialRecord, old_record: CredentialRecord | None) -> int:
        new_client = self.client_factory(new_record.token)
        try:
            new_client.validate_user()
        except CanvasHTTPError as exc:
            if exc.status in {401, 403}:
                raise HylmsError("auth_rejected", "새 Canvas PAT가 만료되었거나 거부되었습니다.") from exc
            raise
        store = self._store()
        store.self_test()
        wrote_new = False
        try:
            store.write(new_record)
            wrote_new = True
            readback = store.read()
            if readback != new_record:
                raise CredentialStoreError(
                    "credential_readback_mismatch", "저장한 credential의 readback이 일치하지 않습니다."
                )
            try:
                self.client_factory(readback.token).validate_user()
            except CanvasHTTPError as exc:
                if exc.status in {401, 403}:
                    raise HylmsError("auth_readback_rejected", "저장 후 다시 읽은 Canvas PAT 검증에 실패했습니다.") from exc
                raise
        except BaseException:
            if wrote_new:
                try:
                    if old_record is None:
                        store.delete()
                    else:
                        store.write(old_record)
                except BaseException as rollback_error:
                    raise CredentialStoreError(
                        "credential_rollback_failed", "새 credential 저장 실패 후 기존 credential 복원에도 실패했습니다."
                    ) from rollback_error
            raise

        if old_record and old_record.token != new_record.token:
            try:
                self.client_factory(old_record.token).revoke_self()
            except HylmsError:
                self.out("경고: 기존 PAT 자동 폐기에 실패했습니다. Canvas 설정에서 직접 정리하세요.")
                return 2
        return 0

    def rotate(self) -> int:
        store = self._store()
        old_record = store.read()
        new_record = self._prompt_record()
        result = self._install_record(new_record, old_record)
        self.out("새 Canvas PAT를 검증하고 Windows Credential Manager에 저장했습니다.")
        return result

    def check(self) -> int:
        store = self._store()
        record = store.read()
        if record is None:
            raise HylmsError(
                "auth_missing",
                "저장된 Canvas PAT가 없습니다. `py hylms_snapshot.py auth rotate`를 실행하세요.",
            )
        store.self_test()
        try:
            self.client_factory(record.token).validate_user()
        except CanvasHTTPError as exc:
            if exc.status in {401, 403}:
                raise HylmsError(
                    "auth_rejected",
                    "저장된 PAT가 만료되었거나 거부되었습니다. `py hylms_snapshot.py auth rotate`를 실행하세요.",
                ) from exc
            raise
        expiry = parse_iso_datetime(record.expires_at)
        if expiry is None:
            raise HylmsError("auth_expiry_invalid", "저장된 PAT 만료일을 확인할 수 없습니다.")
        current = self.now().astimezone(KST)
        days = (expiry.astimezone(KST).date() - current.date()).days
        remaining = "D-Day" if days == 0 else f"D-{days}" if days > 0 else f"D+{-days}"
        self.out(
            "인증 확인: Credential Manager 정상, Canvas PAT 유효, "
            f"만료일 {expiry.astimezone(KST).date().isoformat()} ({remaining})"
        )
        if expiry - current <= dt.timedelta(days=7):
            self.out("경고: Canvas PAT 만료가 7일 이내입니다. `py hylms_snapshot.py auth rotate`를 실행하세요.")
        return 0

    def snapshot(self) -> int:
        store = self._store()
        record = store.read()
        if record is None:
            self.out("저장된 Canvas PAT가 없습니다. 최초 등록을 시작합니다.")
            record = self._prompt_record()
            install_result = self._install_record(record, None)
            if install_result != 0:
                return install_result
        client = self.client_factory(record.token)
        try:
            user = client.validate_user()
        except CanvasHTTPError as exc:
            if exc.status in {401, 403}:
                self.out("오류[auth_rejected]: 저장된 PAT가 만료되었거나 거부되었습니다. `py hylms_snapshot.py auth rotate`를 실행하세요.")
                return 1
            raise
        expiry = parse_iso_datetime(record.expires_at)
        if expiry and expiry - self.now().astimezone(KST) <= dt.timedelta(days=7):
            self.out("경고: Canvas PAT 만료가 7일 이내입니다. `py hylms_snapshot.py auth rotate`를 실행하세요.")
        runner = SnapshotRunner(
            client,
            output_root=self.output_root,
            now=self.now(),
            clock=self.now,
            out=self.out,
            learningx_bootstrap=LearningXBootstrap(client),
        )
        return runner.run(str(user["id"]))


def main(argv: Sequence[str] | None = None) -> int:
    if not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not sys.stderr.isatty() and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    return Application().run(list(sys.argv[1:] if argv is None else argv))
