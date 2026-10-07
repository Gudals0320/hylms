"""Local recovery contracts, not a simulation of Codex approval decisions."""
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hylms.core import HylmsError
from hylms.runtime import RuntimeService, main


class ApprovalContractTests(unittest.TestCase):
    def test_status_before_start_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = mock.Mock()
            service = RuntimeService(root, launcher=launcher)
            self.assertEqual(service.status('session'), {'status': 'not_started'})
            launcher.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_admitted_start_is_not_relaunched_after_status_check(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = mock.Mock(return_value=os.getpid())
            service = RuntimeService(Path(directory), launcher=launcher, execution_check=lambda: None)
            first = service.start('session', '$hylms')
            self.assertEqual(service.status('session')['status'], 'starting')
            second = service.start('session', '$hylms')
            self.assertEqual(first['operation_id'], second['operation_id'])
            launcher.assert_called_once()

    def test_cli_preserves_original_request_after_preflight_failure(self):
        # Host review denial happens before Python. This tests the separate local
        # preflight boundary and the invocation file needed for an approved retry.
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'invocation.txt'
            original = '$hylms\n이번 예약 범위만 실행해 주세요.\n'
            source.write_text(original, encoding='utf-8')
            service = mock.Mock()
            service.start.side_effect = HylmsError('runtime_execution_context_required', 'fixture')
            with mock.patch('hylms.runtime.RuntimeService', return_value=service), \
                 mock.patch('hylms.runtime.session_id', return_value='session'), \
                 mock.patch('hylms.runtime.staging_file', return_value=source), \
                 mock.patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(main(['start', '--prompt-file', str(source)]), 1)
                self.assertEqual(source.read_text(encoding='utf-8'), original)
                service.start.assert_called_once_with('session', original)
                service.start.reset_mock()
                service.start.side_effect = None
                service.start.return_value = {'status': 'starting'}
                self.assertEqual(main(['start', '--prompt-file', str(source)]), 0)
                service.start.assert_called_once_with('session', original)
                self.assertFalse(source.exists())

    def test_skill_scope_and_denial_contract_structure(self):
        # Structural lint only: cannot establish what a model/reviewer will do.
        skill = (Path(__file__).resolve().parents[1] / 'skills/hylms/SKILL.md').read_text(encoding='utf-8')
        pipeline = skill.split('**Pipeline scope template:**', 1)[1].split('**Manual scope template:**', 1)[0]
        manual = skill.split('**Manual scope template:**', 1)[1].split('### Before-start denial', 1)[0]
        for item in ('<configured-ntfy-topic>', 'https://ntfy.sh/', 'HY-LMS Calendar', 'configured credential-owner'):
            self.assertIn(item, pipeline)
        self.assertIn('phase2_state.json', manual)
        self.assertIn('Google 동기화·credential 변경은 실행하지 않습니다', manual)
        for item in ('worker 미시작', '현재 목록 미확인', 'Only `not_started`',
                     'If status cannot be read, stop', 'Do not call `pending` or `targets`',
                     'they are not evidence of authorization'):
            self.assertIn(item, skill)
