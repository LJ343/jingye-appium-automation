"""Cross-client cleanup proof: actual parsers/factory; all external effects fake."""
from contextlib import redirect_stdout
import copy,io,json,os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import test_phone_process_ownership as base
import jingye_appium_reconcile as recovery

HOST={'pid':42,'createdAtUtc':'2026-11-04T00:00:00.000Z'}
SID=base.SID
OTHER='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
RECORD=dict(state='cleanup_uncertain',scope='host',host=HOST,sessionId=SID,clientPid=901,
            atUtc='2026-11-04T01:00:00.000Z')

def logs(sid=SID):
    return ('Welcome to Appium\nlistener started\n'
      '[HTTP] --> POST /session {}\n'
      f'session {sid} added to master session list\n'
      '[HTTP] <-- POST /session 200 12 ms\n'
      f'[HTTP] --> DELETE /session/{sid}\n'
      f'Removing session {sid} from our master session list\n'
      f'[HTTP] <-- DELETE /session/{sid} 200 12 ms\n').encode()

class AuditTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/('20261104T000000000Z-'+'a'*32+'.stdout.log')
        self.path.write_bytes(logs())
    def audit(self,**kwargs):return recovery.MasterIdleAudit(HOST,SID,90,lambda:0,self.root,**kwargs)
    def test_complete_explicit_delete_and_read_only_tail(self):
        a=self.audit()
        with self.path.open('ab') as f:f.write(b'[HTTP] --> GET /status\n[HTTP] <-- GET /status 200\n')
        self.assertTrue(a.unchanged_since_idle());self.assertEqual(len(a.prefix_sha256),64)
    def test_active_foreign_session_and_pending_creation_rejected(self):
        for extra in [f'[HTTP] --> POST /session {{}}\nsession {OTHER} added to master session list\n[HTTP] <-- POST /session 200\n',
                      '[HTTP] --> POST /session {}\n',f'[HTTP] --> DELETE /session/{OTHER}\n']:
            self.path.write_bytes(logs()+extra.encode())
            with self.subTest(extra=extra),self.assertRaises(recovery.ReconciliationError):self.audit()
    def test_exact_session_and_successful_delete_required(self):
        for raw in [logs(OTHER),logs().replace(f'<-- DELETE /session/{SID} 200'.encode(),f'<-- DELETE /session/{SID} 500'.encode())]:
            self.path.write_bytes(raw)
            with self.assertRaisesRegex(recovery.ReconciliationError,'exact_delete'):self.audit()
    def test_partial_line_missing_header_and_unpaired_master_rejected(self):
        for raw in [logs()[:-1],logs().replace(b'Welcome to Appium',b'wrong'),logs()+f'Removing session {OTHER} from our master session list\n'.encode()]:
            self.path.write_bytes(raw)
            with self.assertRaises(recovery.ReconciliationError):self.audit()
    def test_overlapping_requests_and_failed_creation_with_master_add_rejected(self):
        for raw in [logs().replace(b'[HTTP] <-- POST /session 200',b'[HTTP] --> POST /session {}\n[HTTP] <-- POST /session 200'),
                    logs().replace(b'<-- POST /session 200',b'<-- POST /session 500')]:
            self.path.write_bytes(raw)
            with self.assertRaises(recovery.ReconciliationError):self.audit()
    def test_new_mutation_or_master_change_during_phone_proof_rejected(self):
        for extra in [b'[HTTP] --> POST /session {}\n',f'[HTTP] --> DELETE /session/{SID}\n'.encode(),b'Removing session from master session list\n']:
            self.path.write_bytes(logs());a=self.audit()
            with self.path.open('ab') as f:f.write(extra)
            with self.assertRaises(recovery.ReconciliationError):a.unchanged_since_idle()
    def test_log_replacement_truncation_or_partial_tail_rejected(self):
        for action in ['replace','truncate','partial']:
            self.path.write_bytes(logs());a=self.audit()
            if action=='replace':
                p=self.root/'replacement.tmp';p.write_bytes(logs());os.replace(p,self.path)
            elif action=='truncate':self.path.write_bytes(b'Welcome to Appium\n')
            else:
                with self.path.open('ab') as f:f.write(b'[HTTP] --> GET /status')
            with self.assertRaises(recovery.ReconciliationError):a.unchanged_since_idle()
    def test_wrong_birth_or_ambiguous_log_rejected(self):
        with self.assertRaises(recovery.ReconciliationError):recovery.MasterIdleAudit({**HOST,'createdAtUtc':'2026-11-05T00:00:00.000Z'},SID,90,lambda:0,self.root)
        (self.root/('20261104T000000000Z-'+'b'*32+'.stdout.log')).write_bytes(logs())
        with self.assertRaises(recovery.ReconciliationError):self.audit()
    def test_budget_and_line_size_are_bounded(self):
        with self.assertRaisesRegex(recovery.ReconciliationError,'budget'):recovery.MasterIdleAudit(HOST,SID,0,lambda:1,self.root)
        self.path.write_bytes(logs()+b'x'*(1024*1024)+b'\n')
        with self.assertRaisesRegex(recovery.ReconciliationError,'oversize'):self.audit()

class ClientTest(unittest.TestCase):
    def test_absent_and_proven_reused_pid_are_gone(self):
        self.assertTrue(recovery.original_client_gone(RECORD,lambda _:None))
        self.assertTrue(recovery.original_client_gone(RECORD,lambda _:{'pid':901,'createdAtUtc':'2026-11-04T02:00:00.000Z'}))
    def test_live_missing_or_wrong_pid_birth_not_accepted(self):
        for value in [{'pid':901,'createdAtUtc':'2026-11-04T00:59:00.000Z'},
                      {'pid':901},{'pid':902,'createdAtUtc':'2026-11-04T02:00:00.000Z'}]:
            with self.subTest(value=value),self.assertRaises(Exception):recovery.original_client_gone(RECORD,lambda _:value)
    def test_client_query_failure_is_not_absence(self):
        with self.assertRaises(OSError):recovery.original_client_gone(RECORD,Mock(side_effect=OSError('query failed')))

class ProofTest(unittest.TestCase):
    def setUp(self):
        self.client=base.FakeClient();self.guard=SimpleNamespace(port=4723,host_probe=lambda _:HOST,_current_host=Mock(return_value=HOST))
        self.audit=SimpleNamespace(unchanged_since_idle=Mock(return_value=True),prefix_sha256='a'*64)
        self.session=Mock(return_value=True)
        self.kwargs=dict(client_probe=lambda _:None,clock=self.client.clock,audit_factory=Mock(return_value=self.audit),session_probe=self.session)
        self.link=SimpleNamespace(_default_client=lambda:self.client)
    def prove(self,record=None):return recovery.prove_cleanup_idle(self.guard,self.link,record or copy.deepcopy(RECORD),HOST,**self.kwargs)
    def test_actual_phone_snapshot_idle_twice_and_no_mutations(self):
        result=self.prove();self.assertEqual(result['physicalIdleObservations'],2);self.assertTrue(result['samePhoneVerified'])
        self.assertEqual(self.session.call_count,2);self.assertEqual(self.client.stops,[])
        commands=[r[0][2:] for r in self.client.calls if isinstance(r[0],list)]
        self.assertTrue(commands);self.assertTrue(all(c[:2] in [['shell','ps'],['shell','dumpsys'],['forward','--list']] for c in commands))
    def test_foreign_process_or_forward_refuses_without_signal(self):
        for field in ['active','keep_forward']:
            setattr(self.client,field,True)
            with self.assertRaises(Exception):self.prove()
            self.assertEqual(self.client.stops,[]);setattr(self.client,field,False)
    def test_unknown_instrumentation_report_is_not_zero(self):
        self.client.unknown_report=True
        with self.assertRaises(Exception):self.prove()
    def test_endpoint_change_between_snapshots_refuses(self):
        calls=[]
        def ensure(deadline):
            calls.append(1);return 'old-endpoint' if len(calls)==1 else 'new-endpoint'
        self.client._ensure=ensure
        with self.assertRaisesRegex(recovery.ReconciliationError,'snapshot_changed'):self.prove()
    def test_active_unknown_or_missing_session_refuses_before_phone(self):
        for change in [{'state':'active'},{'state':'creation_unknown'},{'scope':'unbound'},{'sessionId':None}]:
            with self.assertRaises(Exception):self.prove({**RECORD,**change})
        self.assertEqual(self.client.calls,[])
    def test_session_or_audit_failure_never_promotes(self):
        self.session.side_effect=RuntimeError('not ended')
        with self.assertRaises(RuntimeError):self.prove()
        self.assertEqual(self.client.calls,[])
        self.session.side_effect=None;self.audit.unchanged_since_idle.side_effect=RuntimeError('new creation')
        with self.assertRaises(RuntimeError):self.prove()
    def test_original_client_reappears_and_host_change_refuse(self):
        self.kwargs['client_probe']=Mock(side_effect=[None,{'pid':901,'createdAtUtc':'2026-11-04T00:59:00.000Z'}])
        with self.assertRaises(Exception):self.prove()
        self.kwargs['client_probe']=lambda _:None;self.guard._current_host.side_effect=[HOST,{**HOST,'pid':43}]
        with self.assertRaises(Exception):self.prove()
    def test_exact_session_response_requires_404_invalid_id(self):
        for status,body in [(200,{'value':{}}),(404,{'value':{'error':'unknown command'}}),(302,{'value':{'error':'invalid session id'}}),
                            (404,{'value':[]})]:
            conn=Mock();conn.getresponse.return_value=SimpleNamespace(status=status,read=lambda _:json.dumps(body).encode())
            with patch.object(recovery.http.client,'HTTPConnection',return_value=conn),self.assertRaises(Exception):
                recovery.old_session_ended(4723,SID,90,lambda:0)
            self.assertEqual(conn.request.call_args.args[0],'GET');conn.close.assert_called_once()
    def test_exact_invalid_session_response_passes(self):
        conn=Mock();conn.getresponse.return_value=SimpleNamespace(status=404,read=lambda _:b'{"value":{"error":"invalid session id"}}')
        with patch.object(recovery.http.client,'HTTPConnection',return_value=conn):self.assertTrue(recovery.old_session_ended(4723,SID,90,lambda:0))
        self.assertEqual(conn.request.call_args.args[1],'/session/'+SID+'/timeouts')

if __name__=='__main__':unittest.main()
