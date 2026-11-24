"""Nov23 post-init disconnect and durable cross-client recovery, entirely offline."""
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_creation_capture_recovery as capture
import test_failed_creation_recovery as failed
import test_wireless_session_ownership as boundary
import jingye_appium_creation as creation
import jingye_appium_guard as guard_module
import jingye_appium_reconcile as recovery

HOST = capture.base.HOST
TOKEN = failed.TOKEN
HEADER = b'Welcome to Appium\nlistener started\n'
LOG_NAME = '20261104T000000000Z-'+'b'*32+'.stdout.log'


def post_start_log():
    return failed.log().replace(b'instrumentation process cannot be initialized',
        b'The initialization of the instrumentation process took 3399ms\n'
        b'[ADB] The instrumentation process exited with code 255 and signal null\n'
        b'[ADB] Cannot remove forward: device offline')


class PostStartEvidenceTest(unittest.TestCase):
    def test_exact_post_init_offline_window_is_accepted(self):
        receipt = creation.audit_suffix(post_start_log(), TOKEN)
        self.assertEqual(receipt['failureStage'], 'initialized_then_device_offline')
        self.assertTrue(receipt['masterNotAdded'])

    def test_exit_255_or_arbitrary_500_alone_does_not_prove_window(self):
        for raw in (post_start_log().replace(b'device offline', b'other failure'),
                    post_start_log().replace(b'The initialization of the instrumentation process took 3399ms', b'no initialization evidence'),
                    post_start_log().replace(b'Starting UIAutomator2 server', b'other server'),
                    post_start_log().replace(b'"-s", "test-phone"', b'"-s", "foreign-phone"')):
            with self.subTest(raw=raw[:20]), self.assertRaises(creation.CreationEvidenceError):
                creation.audit_suffix(raw, TOKEN)

    def test_success_competition_foreign_token_and_master_add_still_protected(self):
        for raw in (post_start_log().replace(b'500 123', b'200 123'),
                    post_start_log()+post_start_log(),
                    post_start_log()+b'[HTTP] --> POST /session/foreign/element {}\n',
                    post_start_log()+b'session foreign added to master session list\n',
                    post_start_log().replace(TOKEN.encode(), b'foreign')):
            with self.assertRaises(Exception): creation.audit_suffix(raw, TOKEN)


class CrossClientTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.log = self.root / LOG_NAME
        self.log.write_bytes(HEADER)
        self.guard = guard_module.SessionGuard(self.root/'guard.json', 'http://127.0.0.1:4723',
                                               host_probe=lambda _: dict(HOST))
        self.ticket = self.guard.begin()
        self.original, self.client = capture.make()
        self.original.creation_token = TOKEN
        self.original.boot_marker = {'bootId': self.client.boot_id, 'uptime': 0.0}
        self.original.failure_witness = creation.FailureWitness(HOST, lambda _: HOST, 4723, TOKEN, self.root)
        self.ticket.bind_creation(self.original.creation_evidence())
        self.ticket.uncertain('creation_unknown')
        with self.log.open('ab') as f: f.write(post_start_log())
        self.link = SimpleNamespace(_default_client=lambda: self.client)
        self.client.serial = 'new-endpoint'

    def recover(self, *, client_probe=lambda _: None):
        # Construct a different guard instance, using only bytes from the
        # canonical journal. No original PhoneOwnership/FailureWitness reused.
        other = guard_module.SessionGuard(self.guard.path, 'http://127.0.0.1:4723',
                                          host_probe=self.guard.host_probe)
        return other.reconcile_creation(lambda record, host, action:
            recovery.prove_failed_creation(other, self.link, record, host, action,
                                            client_probe=client_probe, log_root=self.root))

    def edit(self, change):
        value = self.guard._read(); change(value['record']); self.guard._write(value)

    def assert_protected(self):
        self.assertIsNotNone(self.guard._read()['record'])
        self.assertFalse(self.client.stops)

    def test_residual_with_exact_token_is_cleaned_then_new_task_can_reserve(self):
        self.client.active = True
        with patch.object(recovery.phone, 'read_sessions_json', return_value=capture.sessions(TOKEN)):
            proof = self.recover()
        self.assertEqual(len(self.client.stops), 1)
        self.assertEqual(self.client.entries, [])
        self.assertEqual(proof['physicalIdleObservations'], 2)
        value = self.guard._read()
        self.assertIsNone(value['record'])
        self.assertEqual(value['lastClosed']['state'], 'failed_creation_reconciled')
        actions = value['lastClosed']['phoneActions']
        self.assertEqual([a['kind'] for a in actions], ['forward_allocate', 'forward_remove', 'process_signal'])
        self.assertTrue(all(a['phase'] == 'confirmed' for a in actions))
        self.assertEqual(self.guard.begin().host, HOST)

    def test_empty_downstream_owned_socket_is_also_reclaimable(self):
        self.client.active = True
        with patch.object(recovery.phone, 'read_sessions_json', return_value={'value': []}):
            self.assertTrue(self.recover()['physicalTerminalVerified'])
        self.assertEqual(len(self.client.stops), 1)

    def test_idle_phone_needs_no_signal_or_temporary_forward(self):
        proof = self.recover()
        self.assertTrue(proof['samePhoneVerified'])
        self.assertEqual(self.client.stops, [])
        self.assertEqual(self.client.next_port, 49100)

    def test_json_roundtrip_retains_production_tuple_snapshot_types(self):
        value=recovery.phone.PhoneOwnership(self.link,'http://127.0.0.1:4723',HOST,lambda _:HOST)
        value.restore_creation(self.guard._read()['record']['creationEvidence'],log_root=self.root)
        self.assertEqual(value.initial,self.original.initial)

    def test_browser_forward_in_original_baseline_is_preserved(self):
        browser = ('old-endpoint', 'tcp:49998', 'localabstract:webview_devtools_remote_100')
        self.edit(lambda r: r['creationEvidence']['initial']['forwards'].append(browser))
        self.client.base_entries = [browser]; self.client.keep_forward = True
        proof = self.recover()
        self.assertEqual(proof['preservedBaselineForwardCount'], 1)
        self.assertEqual(self.client.removes, [])

    def test_new_unknown_browser_forward_is_protected(self):
        self.client.base_entries = [('other', 'tcp:49998', 'localabstract:foreign')]
        self.client.keep_forward = True
        with self.assertRaises(Exception): self.recover()
        self.assert_protected(); self.assertFalse(self.client.removes)

    def test_foreign_downstream_token_never_signals(self):
        self.client.active = True
        with patch.object(recovery.phone, 'read_sessions_json', return_value=capture.sessions('foreign')):
            with self.assertRaises(Exception): self.recover()
        self.assert_protected()
        self.assertEqual(self.client.entries, [])

    def test_old_client_alive_never_touches_phone(self):
        record = self.guard._read()['record']
        with self.assertRaises(Exception):
            self.recover(client_probe=lambda _: {'pid':record['clientPid'], 'createdAtUtc':HOST['createdAtUtc']})
        self.assert_protected(); self.assertEqual(self.client.entries, [])

    def test_host_change_cannot_discharge_creation(self):
        self.guard.host_probe = lambda _: {**HOST, 'pid': 999}
        with self.assertRaises(Exception): self.recover()
        self.assert_protected()

    def test_legacy_missing_checkpoint_never_invents_baseline(self):
        self.edit(lambda r: r.pop('creationEvidence'))
        with self.assertRaises(Exception): self.recover()
        self.assert_protected()

    def test_corrupt_checkpoint_path_hash_or_offset_never_cleans(self):
        original = self.guard._read()
        for field, replacement in [('logName','../foreign.stdout.log'), ('headerSHA256','0'*64),
                                    ('offset',1), ('fileId',[0,0]), ('token','foreign')]:
            self.guard._write(copy.deepcopy(original))
            self.edit(lambda r: r['creationEvidence']['witness'].update({field:replacement}))
            with self.subTest(field=field), self.assertRaises(Exception): self.recover()
            self.assert_protected()

    def test_live_or_failed_baseline_cannot_be_restored_as_idle(self):
        self.edit(lambda r: r['creationEvidence']['initial']['processes'].append({'pid':100}))
        with self.assertRaises(Exception): self.recover()
        self.assert_protected()

    def test_lost_or_successful_remote_response_stays_protected(self):
        for raw in (post_start_log().split(b'[HTTP] <-- POST /session')[0],
                    post_start_log().replace(b'500 123', b'200 123')):
            self.log.write_bytes(HEADER+raw)
            with self.assertRaises(Exception): self.recover()
            self.assert_protected()

    def test_same_host_new_mutation_in_log_stays_protected(self):
        with self.log.open('ab') as f: f.write(b'[HTTP] --> DELETE /session/foreign\n')
        with self.assertRaises(Exception): self.recover()
        self.assert_protected()

    def test_reboot_with_new_process_cannot_be_claimed_as_original(self):
        self.client.active = True; self.client.boot_id = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        with self.assertRaisesRegex(Exception, 'boot_changed'): self.recover()
        self.assert_protected()

    def test_new_process_outside_creation_window_never_signals(self):
        self.client.active = True; self.client.ticks = 50000; self.client.uptime = 600.0
        with self.assertRaisesRegex(Exception, 'birth_outside_window'): self.recover()
        self.assert_protected()

    def test_reboot_with_empty_phone_can_converge_without_mutation(self):
        self.client.boot_id = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        self.assertTrue(self.recover()['physicalTerminalVerified'])
        self.assertEqual(self.client.stops, [])

    def test_lost_signal_response_never_replayed_by_next_process(self):
        self.client.active = True; self.client.stop_lost = True
        with patch.object(recovery.phone, 'read_sessions_json', return_value=capture.sessions(TOKEN)):
            with self.assertRaises(TimeoutError): self.recover()
            ports = self.client.next_port
            with self.assertRaisesRegex(Exception, 'physical_not_idle'): self.recover()
        self.assertEqual(len(self.client.stops), 1)
        self.assertEqual(self.client.next_port, ports)
        self.assertEqual(self.guard._read()['record']['phoneActions'][-1]['phase'], 'intent')

    def test_lost_signal_then_observed_idle_archives_read_only(self):
        self.client.active = True; self.client.stop_lost = True
        with patch.object(recovery.phone, 'read_sessions_json', return_value=capture.sessions(TOKEN)):
            with self.assertRaises(TimeoutError): self.recover()
        self.client.active = False; ports = self.client.next_port
        proof = self.recover()
        self.assertTrue(proof['priorMutationReadOnlyConvergence'])
        self.assertEqual(len(self.client.stops), 1); self.assertEqual(self.client.next_port, ports)

    def test_lost_forward_allocation_stays_protected_without_guessing_removal(self):
        self.client.active = True; self.client.unknown_allocation = True
        with self.assertRaises(TimeoutError): self.recover()
        ports = self.client.next_port
        with self.assertRaises(Exception): self.recover()
        self.assertEqual(self.client.next_port, ports); self.assertEqual(self.client.removes, [])
        self.assert_protected()

    def test_storage_failure_before_intent_prevents_mutation(self):
        self.client.active = True
        with patch.object(guard_module.SessionGuard, '_write', side_effect=guard_module.GuardError('disk')):
            with self.assertRaises(Exception): self.recover()
        self.assert_protected(); self.assertEqual(self.client.next_port, 49100)

    def test_final_archive_write_failure_is_durable_and_converges_without_second_signal(self):
        self.client.active=True; real=guard_module.SessionGuard._write
        def disk_failure(g,value):
            if value['record'] is None:raise guard_module.GuardError('disk')
            return real(g,value)
        with patch.object(recovery.phone,'read_sessions_json',return_value=capture.sessions(TOKEN)),\
             patch.object(guard_module.SessionGuard,'_write',new=disk_failure):
            with self.assertRaises(guard_module.GuardError):self.recover()
        self.assertIsNotNone(self.guard._read()['record']);self.assertEqual(len(self.client.stops),1)
        ports=self.client.next_port
        self.assertTrue(self.recover()['priorMutationReadOnlyConvergence'])
        self.assertEqual(self.client.next_port,ports);self.assertEqual(len(self.client.stops),1)

    def test_known_cleanup_preserves_only_durable_baseline_browser_mapping(self):
        browser=('old-endpoint','tcp:49998','localabstract:webview_devtools_remote_100')
        self.edit(lambda r:r['creationEvidence']['initial']['forwards'].append(browser))
        record=self.guard._read()['record'];record.update(state='cleanup_uncertain',sessionId=capture.base.SID)
        self.client.base_entries=[browser];self.client.keep_forward=True
        audit=SimpleNamespace(unchanged_since_idle=lambda:True,prefix_sha256='a'*64)
        proof=recovery.prove_cleanup_idle(self.guard,self.link,record,HOST,client_probe=lambda _:None,
            clock=self.client.clock,log_root=self.root,audit_factory=Mock(return_value=audit),session_probe=Mock())
        self.assertEqual(proof['preservedBaselineForwardCount'],1);self.assertEqual(self.client.removes,[])

    def test_cross_task_read_recovery_keeps_one_total_deadline(self):
        self.client.active=True;real=self.client._exec
        def unavailable(args,timeout,deadline):
            if 'ps' in args:
                self.client.now+=min(80,deadline-self.client.now)
                raise TimeoutError('synthetic offline')
            return real(args,timeout,deadline)
        self.client._exec=unavailable
        with self.assertRaisesRegex(Exception,'budget_exhausted'):self.recover()
        self.assertEqual(self.client.now,300);self.assert_protected()

    def test_instrumentation_or_socket_owner_change_keeps_protection(self):
        self.client.active = True; self.client.socket_uid += 1
        with self.assertRaises(Exception): self.recover()
        self.assert_protected(); self.assertEqual(self.client.entries, [])

    def test_final_idle_snapshot_change_cannot_archive(self):
        count=[]; original=self.client._exec
        def changed(args, timeout, deadline):
            if 'ps' in args:
                count.append(1)
                if len(count) >= 3: self.client.active = True
            return original(args,timeout,deadline)
        self.client._exec=changed
        with self.assertRaises(Exception): self.recover()
        self.assertIsNotNone(self.guard._read()['record'])

    def test_configured_factory_routes_unknown_creation_to_cross_recovery(self):
        config = self.root/'wireless.json'
        config.write_text(json.dumps({'appium_guard_path':str(self.guard.path)}))
        real = recovery.prove_failed_creation
        def prove(g, link, record, host, action):
            return real(g, self.link, record, host, action, client_probe=lambda _:None, log_root=self.root)
        with patch.dict(os.environ, {'JINGYE_WIRELESS_ENABLED':'1','JINGYE_WIRELESS_CONFIG':str(config)}),\
             patch.object(guard_module, 'query_local_host', return_value=HOST),\
             patch.object(recovery, 'prove_failed_creation', side_effect=prove), redirect_stdout(io.StringIO()):
            ticket = guard_module.begin('http://127.0.0.1:4723')
        self.assertNotEqual(ticket.owner, self.ticket.owner)


class FactoryEvidenceTest(unittest.TestCase):
    def test_checkpoint_commit_precedes_remote(self):
        scope=boundary.load(); events=[]
        owner=scope['appium_phone'].prepare.return_value
        owner.failure_witness=object();owner.creation_evidence.return_value={'test':'evidence'}
        reservation=scope['appium_guard'].begin.return_value
        reservation.bind_creation.side_effect=lambda _:events.append('checkpoint')
        candidate=boundary.driver()
        scope['webdriver'].Remote.side_effect=lambda *a,**k:events.append('Remote') or candidate
        with redirect_stdout(io.StringIO()): scope['create_appium_session']('test')
        self.assertEqual(events,['checkpoint','Remote'])

    def test_checkpoint_write_failure_prevents_remote(self):
        scope=boundary.load();owner=scope['appium_phone'].prepare.return_value
        owner.failure_witness=object()
        scope['appium_guard'].begin.return_value.bind_creation.side_effect=RuntimeError('disk')
        with redirect_stdout(io.StringIO()),self.assertRaises(Exception): scope['create_appium_session']('test')
        scope['webdriver'].Remote.assert_not_called()

    def test_guard_refusal_sets_unconfirmed_final_status(self):
        scope=boundary.load()
        scope['appium_guard'].begin.side_effect=scope['appium_guard'].GuardError('protected')
        out=io.StringIO()
        with redirect_stdout(out),self.assertRaises(Exception):scope['create_appium_session']('test')
        self.assertTrue(scope['_session_creation_outcome_uncertain'])
        self.assertIn('YYB_PREVIOUS_SESSION_GUARD_UNRESOLVED',out.getvalue())

    def test_cleanup_failure_uses_fixed_code_and_never_private_exception_text(self):
        scope=boundary.load(); scope['webdriver'].Remote.side_effect=RuntimeError('Remote failed')
        scope['appium_phone'].prepare.return_value.finish_failed_creation.side_effect=RuntimeError('PRIVATE_CLEANUP_DETAIL')
        out=io.StringIO()
        with redirect_stdout(out),self.assertRaises(Exception):scope['create_appium_session']('test')
        self.assertNotIn('PRIVATE_CLEANUP_DETAIL',out.getvalue())
        self.assertIn('YYB_CREATION_CLEANUP_UNCONFIRMED',out.getvalue())


if __name__=='__main__':unittest.main()
