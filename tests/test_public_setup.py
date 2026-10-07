import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hylms.config import load_config
from hylms.setup import initialize_state, bind_calendar
from hylms.diff import discover_runs, validate_state_references, prepare_decision_packet
from hylms.google_calendar import calendar_marker
from tests.test_hylms_diff import write_run, course_payload


class PublicSetupTests(unittest.TestCase):
    def test_fresh_baseline_does_not_skip_existing_first_snapshot_text(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            initialize_state(root, '26-2')
            state = json.loads((root / 'phase2_state.json').read_text(encoding='utf-8'))
            term = root / 'snapshots/26-2'
            baseline = discover_runs(term)[0]
            write_run(term, 'first-real', baseline['started_at'] + dt.timedelta(seconds=1), course_payload())
            validate_state_references(state, term)
            packet = prepare_decision_packet(term, state)
            self.assertTrue(packet['changes'])
            self.assertTrue(all(c['type'] == 'added' for c in packet['changes']))
            original = (root / 'phase2_state.json').read_bytes()
            with self.assertRaises(ValueError):
                initialize_state(root, '26-2')
            self.assertEqual(original, (root / 'phase2_state.json').read_bytes())

    def test_invalid_term_cannot_escape_repository(self):
        with tempfile.TemporaryDirectory() as name:
            for term in ('../outside', '26-2/../../escape', 'unknown'):
                with self.assertRaises(ValueError):
                    initialize_state(name, term)
            self.assertEqual(list(Path(name).iterdir()), [])

    def test_configuration_is_private_explicit_and_rejects_credentials(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'settings.json'
            with mock.patch.dict(os.environ, {'HYLMS_CONFIG': str(path)}):
                self.assertEqual(load_config(), {})
                for bad in ({'token': 'do-not-print'}, {'ntfy_topic': '../escape'}, {'schedule_hours': [24]}):
                    path.write_text(json.dumps(bad), encoding='utf-8')
                    with self.assertRaises(ValueError) as caught:
                        load_config()
                    self.assertNotIn('do-not-print', str(caught.exception))

    def test_calendar_binding_reads_only_and_refuses_existing_managed_data(self):
        client = mock.Mock()
        client.auth.auth_type = 'service_account'
        client.auth.load.return_value = {'client_email': 'example-sync@example-project-123.iam.gserviceaccount.com'}
        identity = 'a' * 32
        client.get_calendar.return_value = {'id': 'example-calendar', 'description': calendar_marker(identity)}
        client.list_events.return_value = []
        with tempfile.TemporaryDirectory() as name:
            self.assertEqual(bind_calendar(name, 'example-calendar', identity, client=client)['external_writes'], 0)
            with self.assertRaises(ValueError):
                bind_calendar(name, 'example-calendar', identity, client=client)
            client.insert.assert_not_called()
            client.patch.assert_not_called()
            client.delete.assert_not_called()
        client.list_events.return_value = [{'extendedProperties': {'private': {'hylms_owner': identity}}}]
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(ValueError):
                bind_calendar(name, 'example-calendar', identity, client=client)
            self.assertFalse((Path(name) / 'google_calendar_state.json').exists())
