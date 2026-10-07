import copy
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from hylms.course_context import save_courses, read_context, class_slot, classify_record, unresolved_reviews
from hylms.context_recovery import prepare_recovery
from hylms.workflow_adapters import EngineAdapters, read_state, pending_review
from hylms import diff, qa
from hylms.core import HylmsError
from hylms.storage import atomic_write_json
from tests.test_hylms_diff import course_payload, write_run, phase2_state, pending, qa_verdict

NOW = dt.datetime(2026, 9, 20, 15, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def course(mode="in_person"):
    return {"course_id":"101", "name":"test", "mode":mode, "authority":"user_confirmed",
            "slots":[{"weekday":1,"start":"16:00","end":"17:30","room":"Room 205"}]}


class CourseContextTests(unittest.TestCase):
    def test_durable_timetable_and_delivery_exceptions(self):
        with tempfile.TemporaryDirectory() as root:
            for mode in ("in_person", "reference_only", "online_only", "online_with_special_lectures"):
                save_courses(root, "26-2", [course(mode)])
                context = read_context(root, "26-2")
                normal = class_slot(context, "101", "2026-09-22")
                self.assertEqual(normal is not None, mode == "in_person")
                special = class_slot(context, "101", "2026-09-22", special_lecture=True)
                self.assertEqual(special is not None, mode in {"in_person", "online_with_special_lectures"})
                self.assertIsNone(class_slot(context, "101", "2026-09-21", special_lecture=True))
                self.assertIsNone(class_slot(context, "101", "next Tuesday", special_lecture=True))
            self.assertEqual(read_context(root,"27-1")["courses"], [])

    def test_obligation_classification_does_not_invent_tasks(self):
        record = {"section":"weekly_learning", "structured":{"compare":{
            "kind":"pdf", "progress":{"completed":False}, "attendance":{"targeted":False},
            "schedule":{"effective":{"due_at":{"state":"unbounded","value":None},
                                       "closes_at":{"state":"unbounded","value":None}}}}}}
        self.assertEqual(classify_record(record), "resource_only")
        record["structured"]["compare"]["kind"] = "new-unrecognized-kind"
        self.assertEqual(classify_record(record), "internal_review")
        record["structured"]["compare"]["kind"] = "video"
        record["structured"]["compare"]["schedule"]["effective"]["due_at"] = {"state":"unknown","value":None}
        self.assertEqual(classify_record(record), "internal_review")
        record["structured"]["compare"]["attendance"]["targeted"] = True
        self.assertEqual(classify_record(record), "manage_obligation")

    def test_technical_pending_is_not_a_student_question(self):
        state=phase2_state("r0")
        state["pending"]=[{**pending("technical"),"context":{"kind":"qa_pending"}},pending("academic")]
        review=pending_review(state)
        self.assertEqual([p["id"] for p in review["items"]],["academic"])
        self.assertEqual(review["technical_items"][0]["id"],"technical")

    def test_source_waiting_is_preserved_without_requesting_user_answers(self):
        state=phase2_state("r0")
        state['pending']=[{**pending('notice'),'context':{'resolution_owner':'source'}}]
        review=pending_review(state)
        self.assertEqual(review['count'],0)
        self.assertEqual(review['source_waiting'][0]['id'],'notice')
        self.assertEqual(review['technical_items'],[])
        self.assertEqual(len(state['pending']),1)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.term=self.root/'snapshots/26-2'
        data=course_payload()
        record=data['weekly_learning'][0]
        record['kind']='pdf'
        record['attendance']['targeted']=False
        record['schedule']['effective']={k:{'state':'unbounded','value':None} for k in ['due_at','closes_at','late_until_at','opens_at']}
        write_run(self.term,'r0',NOW,data)
        self.state=phase2_state('r0')
        self.state['pending']=[{**pending('old-qa',source='weekly_learning:10'),'context':{'kind':'qa_pending','issue_codes':['MISSING_EVIDENCE']}},
                              {**pending('class-deadline'),'context':{'date':'2026-09-22','deadline_condition':'수업 전까지'}}]
        self.path=self.root/'phase2_state.json'
        atomic_write_json(self.path,self.state)
        save_courses(self.root,'26-2',[course()])

    def test_recovery_proposal_has_context_and_resolves_time_without_cursor_reset(self):
        proposed=prepare_recovery(self.root,self.state,self.term,NOW.isoformat())
        candidate=proposed['preview']['candidate_state']
        self.assertEqual(candidate['last_processed_run_id'],'r0')
        self.assertEqual(candidate['pending'],[])
        event=candidate['natural_events'][0]
        self.assertEqual(event['timing']['end'],'2026-09-22T16:00:00+09:00')
        self.assertEqual(event['location'],'Room 205')
        self.assertFalse(event['timing']['end_inclusive'])
        self.assertEqual(proposed['preview']['qa_context']['course_context']['courses'][0]['course_id'],'101')
        self.assertEqual(read_state(self.path),self.state)

    def test_failed_independent_qa_preserves_state_and_records_internal_review(self):
        def exchange(kind,payload,attempt):
            packet=payload['packet']
            return qa_verdict(packet,'failed',[{'code':'MORE_EVIDENCE','message':'Need evidence','change_ids':[],
                'entity_ids':[packet['current_entities'][0]['id']],'field_paths':[]}])
        engine=EngineAdapters(self.root,exchange,clock=lambda:NOW)
        engine.load()
        result=engine.recover_known_context('test')
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(read_state(self.path),self.state)
        self.assertTrue(unresolved_reviews(self.root))

    def test_passed_independent_qa_applies_existing_validated_manual_transaction(self):
        engine=EngineAdapters(self.root,lambda kind,payload,attempt:qa_verdict(payload['packet']),clock=lambda:NOW)
        engine.load()
        result=engine.recover_known_context('test')
        self.assertEqual(result['status'],'success')
        after=read_state(self.path)
        self.assertEqual(after['pending'],[])
        self.assertEqual(after['last_processed_run_id'],'r0')
        self.assertEqual(after['last_failure'],self.state['last_failure'])
        self.assertEqual(unresolved_reviews(self.root),[])
        self.assertIsNone(engine.recover_known_context('again'))

    def test_context_changed_after_review_rejects_commit(self):
        proposed=prepare_recovery(self.root,self.state,self.term,NOW.isoformat())
        packet=qa.prepare_qa_packet(proposed['preview']['qa_context'])
        changed=course();changed['slots'][0]['start']='15:30'
        save_courses(self.root,'26-2',[changed])
        with self.assertRaises(HylmsError):
            diff.commit_manual_state(self.path,self.state,proposed['packet'],proposed['decision'],
                term_directory=self.term,qa_packet=packet,qa_verdict=qa_verdict(packet))
        self.assertEqual(read_state(self.path),self.state)

    def test_pending_reclassification_requires_existing_selected_target_and_sources(self):
        packet=diff.prepare_manual_packet(self.state,'Classify source-owned unknowns',['class-deadline'],NOW.isoformat())
        item=copy.deepcopy(self.state['pending'][1]);item['context']['resolution_owner']='source'
        decision={'schema_version':diff.MANUAL_SCHEMA_VERSION,'transaction_id':packet['transaction']['id'],
                  'reason':'Await source notice','operations':[{'op':'upsert_pending','value':item}]}
        preview=diff.preview_manual_transaction(self.term,self.state,packet,decision)
        self.assertEqual(preview['candidate_state']['last_processed_run_id'],'r0')
        item['source_record_ids']=[]
        with self.assertRaises(HylmsError):
            diff.preview_manual_transaction(self.term,self.state,packet,decision)

    def test_recovery_revision_is_bounded_and_keeps_same_transaction(self):
        for final in ['pass','revise']:
            atomic_write_json(self.path,self.state)
            calls=[]
            def exchange(kind,payload,attempt):
                calls.append((kind,attempt))
                if kind=='manual':
                    self.assertEqual(payload['feedback']['verdict'],'revise')
                    return payload['previous_decision']
                packet=payload['packet']
                value='revise' if attempt==1 else final
                issues=[] if value=='pass' else [{'code':'CHECK_CONTEXT','message':'Review context','change_ids':[],
                    'entity_ids':[packet['current_entities'][0]['id']],'field_paths':[]}]
                return qa_verdict(packet,value,issues)
            engine=EngineAdapters(self.root,exchange,clock=lambda:NOW);engine.load()
            result=engine.recover_known_context('retry-test')
            self.assertEqual(calls,[('qa',1),('manual',2),('qa',2)])
            self.assertEqual(result['status'],'success' if final=='pass' else 'blocked')
            if final=='revise':self.assertEqual(read_state(self.path),self.state)
