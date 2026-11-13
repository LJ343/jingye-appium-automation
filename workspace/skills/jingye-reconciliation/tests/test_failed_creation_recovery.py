"""Failed Remote + roaming reproduction; no live phone/network/service actions."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_creation_capture_recovery as capture
import test_wireless_session_ownership as boundary
import jingye_appium_creation as evidence

owner = capture.owner
TOKEN = 'jingye-'+'a'*32


def log(token=TOKEN):
    body = json.dumps({'capabilities':{'firstMatch':[{}], 'alwaysMatch':{'jingye:ownershipToken':token,'appium:udid':'test-phone'}}})
    command=json.dumps(['-P','5037','-s','test-phone','shell','am','instrument','-w','io.appium.uiautomator2.server.test/androidx.test.runner.AndroidJUnitRunner'])
    return ('[HTTP] --> POST /session '+body+'\n'
            '[driver] Starting UIAutomator2 server 10.2.1\n'
            '[ADB] Creating ADB subprocess with args: '+command+'\n'
            '[driver] instrumentation process cannot be initialized\n'
            '[HTTP] <-- POST /session 500 123 ms - 123\n').encode()


def failed(active=True):
    value, client = capture.make()
    value.failure_witness = SimpleNamespace(verify=Mock(return_value=evidence.audit_suffix(log(),TOKEN)))
    client.active=active;client.serial='new-endpoint'
    return value, client


class FailedCreationTests(unittest.TestCase):
    def test_exclusive_failed_request_and_status_reads(self):
        result=evidence.audit_suffix(log()+b'[HTTP] --> GET /sessions\n[HTTP] <-- GET /sessions 200\n',TOKEN)
        self.assertTrue(result['terminalFailed']);self.assertTrue(result['instrumentationLaunchObserved'])
        for raw in [log().replace(b'"-s", "test-phone"',b'"-s", "other-phone"'),
                    log().replace(b'androidx.test.runner.AndroidJUnitRunner',b'other.Runner')]:
            self.assertFalse(evidence.audit_suffix(raw,TOKEN)['instrumentationLaunchObserved'])

    def test_incomplete_success_foreign_overlap_mutation_and_master_add_rejected(self):
        cases=[log()[:-1],log().replace(b'500 123',b'200 123'),log('other'),log()+log(),
               log()+b'[HTTP] --> DELETE /session/foreign\n',
               log()+b'session abc added to master session list\n',
               log().replace(b'[HTTP] <-- POST /session 500 123 ms - 123\n',b'')]
        for raw in cases:
            with self.subTest(raw=raw[:30]),self.assertRaises(Exception):evidence.audit_suffix(raw,TOKEN)

    def test_actual_log_checkpoint_bound_to_host_and_file_prefix(self):
        host={'pid':42,'createdAtUtc':'2026-11-04T00:00:00.000Z'}
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/('20261104T000000000Z-'+'b'*32+'.stdout.log')
            p.write_bytes(b'Welcome to Appium\nlistener started\n')
            witness=evidence.FailureWitness(host,lambda _:host,4723,TOKEN,d)
            with p.open('ab') as stream:stream.write(log())
            self.assertTrue(witness.verify()['terminalFailed'])
            p.write_bytes(b'changed\n'+log())
            with self.assertRaises(evidence.CreationEvidenceError):witness.verify()

    def test_host_change_never_accepts_same_filename(self):
        host={'pid':42,'createdAtUtc':'2026-11-04T00:00:00.000Z'}
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/('20261104T000000000Z-'+'b'*32+'.stdout.log');p.write_bytes(b'Welcome to Appium\nlistener started\n')
            current=dict(host);w=evidence.FailureWitness(host,lambda _:current,4723,TOKEN,d)
            with p.open('ab') as stream:stream.write(log())
            current['pid']=43
            with self.assertRaises(evidence.CreationEvidenceError):w.verify()

    def test_roaming_failed_remote_empty_server_cleans_exact_process_only(self):
        value,client=failed()
        with patch.object(owner,'read_sessions_json',return_value={'value':[]}):
            receipt=value.finish_failed_creation()
        self.assertTrue(receipt['physicalTerminalVerified']);self.assertEqual(len(client.stops),1)
        self.assertIn('kill -TERM 123',client.stops[0]);self.assertEqual(client.entries,[])
        self.assertTrue(all(r['removed'] for r in value.proof_forwards))

    def test_empty_phone_after_terminal_needs_no_signal(self):
        value,client=failed(False)
        with patch.object(owner,'read_sessions_json',return_value={'value':[]}):
            self.assertTrue(value.finish_failed_creation()['physicalTerminalVerified'])
        self.assertEqual(client.stops,[]);self.assertEqual(value.proof_forwards,[])

    def test_no_checkpoint_or_bad_terminal_never_signals(self):
        for missing in [True,False]:
            value,client=failed()
            if missing:value.failure_witness=None
            else:value.failure_witness.verify.side_effect=RuntimeError('unknown request')
            with patch.object(owner,'read_sessions_json',return_value={'value':[]}):
                with self.assertRaises(Exception):value.finish_failed_creation()
            self.assertEqual(client.stops,[])

    def test_active_master_or_downstream_session_never_signals(self):
        for response in [capture.sessions('foreign'),{'value':[{'id':'foreign'}]}]:
            value,client=failed()
            with patch.object(owner,'read_sessions_json',return_value=response):
                with self.assertRaises(Exception):value.finish_failed_creation()
            self.assertEqual(client.stops,[]);self.assertEqual(client.entries,[])

    def test_active_or_pending_master_in_prefix_never_releases(self):
        host={'pid':42,'createdAtUtc':'2026-11-04T00:00:00.000Z'}
        sid='11111111-2222-4333-8444-555555555555'
        for prefix in [f'session {sid} added to master session list\n',
                       '[HTTP] --> POST /session {}\n',f'[HTTP] --> DELETE /session/{sid}\n']:
            with tempfile.TemporaryDirectory() as d:
                p=Path(d)/('20261104T000000000Z-'+'b'*32+'.stdout.log')
                p.write_bytes(b'Welcome to Appium\nlistener started\n'+prefix.encode())
                w=evidence.FailureWitness(host,lambda _:host,4723,TOKEN,d)
                with p.open('ab') as stream:stream.write(log())
                with self.assertRaises(Exception):w.verify()

    def test_socket_or_final_snapshot_change_never_signals(self):
        for change in ['uid','snapshot']:
            value,client=failed()
            if change=='uid':client.socket_uid+=1
            else:
                original=value._snapshot;calls=[]
                def changed(deadline):
                    result=original(deadline);calls.append(1)
                    if len(calls)>1:client.ticks+=1;result=original(deadline)
                    return result
                value._snapshot=changed
            with patch.object(owner,'read_sessions_json',return_value={'value':[]}):
                with self.assertRaises(Exception):value.finish_failed_creation()
            self.assertEqual(client.stops,[]);self.assertEqual(client.entries,[])

    def test_lost_signal_response_is_not_replayed(self):
        value,client=failed();client.stop_lost=True
        with patch.object(owner,'read_sessions_json',return_value={'value':[]}):
            with self.assertRaises(Exception):value.finish_failed_creation()
            with self.assertRaises(Exception):value.finish_failed_creation()
        self.assertEqual(len(client.stops),1)

    def test_factory_reconnects_after_terminal_cleanup_before_second_remote(self):
        scope=boundary.load();events=[]
        phone=scope['appium_phone'].prepare.return_value
        phone.finish_failed_creation=Mock(return_value={'physicalTerminalVerified':True})
        reservation=scope['appium_guard'].begin.return_value
        reservation.failed_creation_closed=Mock(side_effect=lambda receipt:events.append('released'))
        scope['wireless_link'].ensure_connected.side_effect=lambda:events.append('connect') or 'new-endpoint'
        candidate=boundary.driver();scope['webdriver'].Remote.side_effect=[RuntimeError('create500'),candidate]
        with patch('sys.stdout',new=__import__('io').StringIO()):self.assertIs(scope['create_appium_session']('test'),candidate)
        self.assertEqual(events,['connect','released','connect'])
        self.assertFalse(scope['_session_creation_outcome_uncertain'])
        scope['options'].set_capability.assert_any_call('appium:disableSuppressAccessibilityService',True)

    def test_guard_release_failure_blocks_second_remote(self):
        scope=boundary.load();phone=scope['appium_phone'].prepare.return_value
        phone.finish_failed_creation=Mock(return_value={'physicalTerminalVerified':True})
        scope['appium_guard'].begin.return_value.failed_creation_closed=Mock(side_effect=OSError('disk'))
        scope['webdriver'].Remote.side_effect=RuntimeError('create500')
        with patch('sys.stdout',new=__import__('io').StringIO()),self.assertRaises(Exception):scope['create_appium_session']('test')
        self.assertEqual(scope['webdriver'].Remote.call_count,1)
        self.assertTrue(scope['_session_creation_outcome_uncertain'])

if __name__=='__main__':unittest.main()
