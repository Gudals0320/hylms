import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hylms.qa_capabilities import availability_error
from hylms.runtime import RuntimeService, main
from hylms.storage import atomic_write_json
from tests.test_hylms_runtime import response


def evidence(status="tools_missing"):
    return {"schema_version":1,"session_id":"session","checked_at":dt.datetime.now(dt.timezone.utc).isoformat(),
            "discovery_method":"tool_registry","discovered_tools":[],"status":status}


class CapabilityTests(unittest.TestCase):
    def test_missing_and_unverified_claims_are_rejected(self):
        for value in (None, {}, {**evidence(),"session_id":"other"},
                      {**evidence(),"checked_at":"2020-01-01T00:00:00+00:00"},
                      {**evidence(),"discovery_method":"model_assumption"}):
            self.assertIsNone(availability_error(value,"session"))
        self.assertEqual(availability_error(evidence(),"session"),"runtime_qa_tools_missing")

    def test_present_tools_cannot_be_reported_missing(self):
        record=evidence()
        record['discovered_tools']=['multi_agent_v1__spawn_agent','multi_agent_v1__send_input','multi_agent_v1__wait_agent']
        self.assertIsNone(availability_error(record,'session'))

    def test_spawn_and_execution_failure_have_distinct_codes(self):
        record=evidence('spawn_failed')
        record.update(spawn_tool='multi_agent_v1__spawn_agent',send_tool='multi_agent_v1__send_input',wait_tool='multi_agent_v1__wait_agent')
        record['discovered_tools']=[record[k] for k in ['spawn_tool','send_tool','wait_tool']]
        record.update(attempted_tool=record['spawn_tool'],failure_code='tool_call_failed')
        self.assertEqual(availability_error(record,'session'),'runtime_qa_spawn_failed')
        record.update(status='execution_failed',attempted_tool=record['wait_tool'],reviewer_agent_id='actual-agent')
        self.assertEqual(availability_error(record,'session'),'runtime_qa_execution_failed')
        record['status']='ready'
        self.assertIsNone(availability_error(record,'session'))

    def test_unproven_error_keeps_worker_waiting_and_submission_file(self):
        with tempfile.TemporaryDirectory() as directory:
            service=RuntimeService(Path(directory));folder=service.folder('session');folder.mkdir(parents=True)
            request={'schema_version':1,'session_id':'session','operation_id':'op','request_id':'req','binding':'bound','kind':'qa'}
            atomic_write_json(folder/'request.json',request)
            source=folder/'submission.json'
            atomic_write_json(source,response(request,code='runtime_qa_unavailable'))
            original=source.read_bytes()
            with mock.patch('hylms.runtime.RuntimeService',return_value=service),mock.patch('hylms.runtime.session_id',return_value='session'),mock.patch('sys.stdout',new_callable=io.StringIO) as out:
                self.assertEqual(main(['submit','--response-file',str(source)]),1)
                self.assertEqual(json.loads(out.getvalue())['code'],'runtime_qa_discovery_required')
            self.assertEqual(source.read_bytes(),original)
            self.assertFalse((folder/'response.json').exists())
            atomic_write_json(folder/'qa-capabilities.json',evidence())
            self.assertEqual(service.submit('session',response(request,code='runtime_qa_unavailable'))['status'],'submitted')
            self.assertEqual(json.loads((folder/'response.json').read_text())['error'],'runtime_qa_tools_missing')
