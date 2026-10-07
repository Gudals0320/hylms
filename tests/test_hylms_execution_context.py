import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hylms.core import HylmsError
from hylms.execution_context import execution_context, require_execution_owner
from hylms.runtime import RuntimeService, main, worker


def deny():
    raise HylmsError("runtime_execution_context_required", "fixture wrong Windows account")


class ExecutionContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_sandbox_user_is_permission_error_not_missing_credentials(self):
        value = execution_context(user_reader=lambda: "codexsandboxoffline")
        self.assertEqual(value["status"], "permission_required")
        self.assertEqual(value["code"], "runtime_execution_context_required")
        self.assertNotIn("auth_missing", str(value))

    def test_actual_owner_is_accepted_case_insensitively(self):
        value = execution_context(user_reader=lambda: "EXAMPLE-USER")
        self.assertEqual(value["status"], "ready")
        self.assertIsNone(value["code"])

    def test_inherited_username_cannot_override_os_identity(self):
        with mock.patch.dict(os.environ, {"USERNAME": "example-user", "USERPROFILE": "C:/Users/example-user"}):
            self.assertEqual(execution_context(user_reader=lambda: "codexsandboxonline")["status"], "permission_required")

    def test_unknown_identity_fails_closed(self):
        with mock.patch("hylms.execution_context.execution_context", side_effect=HylmsError("runtime_execution_context_unknown", "fixture")):
            with self.assertRaises(HylmsError) as caught:
                require_execution_owner()
        self.assertEqual(caught.exception.code, "runtime_execution_context_unknown")

    def test_rejected_start_creates_no_files_and_launches_no_worker(self):
        launcher = mock.Mock()
        service = RuntimeService(self.root, launcher=launcher, execution_check=deny)
        with self.assertRaises(HylmsError) as caught:
            service.start("session", "$hylms")
        self.assertEqual(caught.exception.code, "runtime_execution_context_required")
        launcher.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_permission_retry_can_use_same_unstarted_session(self):
        launcher = mock.Mock(return_value=os.getpid())
        service = RuntimeService(self.root, launcher=launcher, execution_check=deny)
        with self.assertRaises(HylmsError):
            service.start("session", "$hylms")
        service.execution_check = lambda: None
        result = service.start("session", "$hylms")
        self.assertEqual(result["status"], "starting")
        launcher.assert_called_once()

    def test_missing_token_does_not_even_check_execution_context(self):
        check = mock.Mock(side_effect=deny)
        service = RuntimeService(self.root, execution_check=check)
        self.assertEqual(service.start("session", "LMS 오류 확인")["status"], "not_invoked")
        check.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_worker_has_its_own_guard_before_engine_or_files(self):
        engine = mock.Mock()
        with self.assertRaises(HylmsError):
            worker(self.root, "session", "operation", engine_factory=engine, execution_check=deny)
        engine.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_manual_start_also_requires_correct_user(self):
        launcher = mock.Mock()
        service = RuntimeService(self.root, launcher=launcher, execution_check=deny)
        with self.assertRaises(HylmsError) as caught:
            service.start("session", manual={"instruction": "완료", "target_ids": ["event"]})
        self.assertEqual(caught.exception.code, "runtime_execution_context_required")
        launcher.assert_not_called()

    def test_self_check_permission_failure_has_nonzero_exit(self):
        with mock.patch("hylms.runtime.self_check", return_value={"status": "permission_required"}), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["self-check"]), 1)
