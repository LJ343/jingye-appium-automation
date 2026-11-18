"""Encrypted protocol and real adapter tests. No sockets, ADB or phone I/O."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCRIPTS=Path(__file__).resolve().parents[1]/'workspace/skills/jingye-reconciliation/scripts'
sys.path.insert(0,str(SCRIPTS))
import jingye_wireless_pairing as pair
import jingye_wireless as wire
KEY=bytes.fromhex('02'*32);ID='03'*16;HOST='05'*32;NONCE='04'*32
REPORT=dict(shared_key_hex=KEY.hex(),reporter_id=ID)
SOURCE=('192.168.50.100',32123)


class Protocol(unittest.TestCase):
    def setUp(self):
        self.now=100.;self.wall=1794963600
        self.exchange=pair.PairExchange(REPORT,dict(host_key_sha256=HOST),clock=lambda:self.now,wall=lambda:self.wall,nonce=lambda:NONCE)
    def ready(self):
        self.exchange.authorize();return self.exchange.request(SOURCE)
    def packet(self,text='192.168.50.100|37111|042819|37222',*,nonce=NONCE,host=HOST,key=KEY):
        iv=b'0'*12;aad=f'JYPAIR1|P|{ID}|{nonce}|{host}'.encode()
        raw=iv+AESGCM(pair.cipher_key(key)).encrypt(iv,text.encode(),aad)
        return pair.sign(f'JYPAIR1|P|{ID}|{nonce}|'+base64.b64encode(raw).decode(),key).encode()
    def test_no_request_without_authentication_failure(self):self.assertIsNone(self.exchange.request(SOURCE))
    def test_request_is_signed_pinned_and_expires(self):
        p=self.ready();fields=pair.decode(p,KEY);self.assertEqual(fields,['JYPAIR1','Q',ID,NONCE,HOST,str(self.wall+40)])
    def test_request_only_once(self):self.ready();self.assertIsNone(self.exchange.request(SOURCE))
    def test_roundtrip_and_one_time_consumption(self):
        self.ready();p=self.packet();self.assertNotIn(b'042819',p);self.exchange.handle(p,SOURCE)
        self.assertEqual(self.exchange.take()[:5],(SOURCE[0],37111,'042819',37222,NONCE));self.assertIsNone(self.exchange.take())
    def test_forged_mac_rejected(self):
        self.ready();p=self.packet();self.exchange.handle(p[:-1]+b'f' if p[-1:]!=b'f' else p[:-1]+b'e',SOURCE);self.assertIsNone(self.exchange.take())
    def test_wrong_source_port_rejected(self):self.ready();self.exchange.handle(self.packet(),(SOURCE[0],32124));self.assertIsNone(self.exchange.take())
    def test_wrong_source_ip_rejected(self):self.ready();self.exchange.handle(self.packet(),('192.168.50.101',32123));self.assertIsNone(self.exchange.take())
    def test_old_nonce_rejected(self):self.ready();self.exchange.handle(self.packet(nonce='aa'*32),SOURCE);self.assertIsNone(self.exchange.take())
    def test_wrong_pinned_host_aad_rejected(self):self.ready();self.exchange.handle(self.packet(host='aa'*32),SOURCE);self.assertIsNone(self.exchange.take())
    def test_wrong_aes_key_even_with_correct_outer_mac_rejected(self):
        self.ready();packet=self.packet(key=bytes.fromhex('aa'*32));body=packet.decode().rsplit('|',1)[0];self.exchange.handle(pair.sign(body,KEY).encode(),SOURCE);self.assertIsNone(self.exchange.take())
    def test_expired_request_rejected(self):self.ready();self.now+=41;self.exchange.handle(self.packet(),SOURCE);self.assertIsNone(self.exchange.take())
    def test_accepted_code_expires_in_memory(self):self.ready();self.exchange.handle(self.packet(),SOURCE);self.now+=21;self.assertIsNone(self.exchange.take())
    def test_payload_ip_must_match_authenticated_source(self):self.ready();self.exchange.handle(self.packet('192.168.50.101|37111|042819|37222'),SOURCE);self.assertIsNone(self.exchange.take())
    def test_pair_and_connection_ports_are_distinct(self):self.ready();self.exchange.handle(self.packet('192.168.50.100|37111|042819|37111'),SOURCE);self.assertIsNone(self.exchange.take())
    def test_invalid_code_and_ports_rejected(self):
        for text in ['192.168.50.100|80|042819|37222','192.168.50.100|37111|04281a|37222','192.168.50.100|37111|042819|65536']:
            self.exchange=pair.PairExchange(REPORT,dict(host_key_sha256=HOST),clock=lambda:self.now,wall=lambda:self.wall,nonce=lambda:NONCE);self.ready();self.exchange.handle(self.packet(text),SOURCE);self.assertIsNone(self.exchange.take())
    def test_encrypted_tag_tampering_is_rejected(self):
        self.ready();body=self.packet().decode().rsplit('|',1)[0];fields=body.split('|');raw=bytearray(base64.b64decode(fields[-1]));raw[-1]^=1;fields[-1]=base64.b64encode(raw).decode();self.exchange.handle(pair.sign('|'.join(fields),KEY).encode(),SOURCE);self.assertIsNone(self.exchange.take())
    def test_status_is_authenticated_not_an_arbitrary_log_string(self):
        self.ready();self.exchange.handle(pair.sign(f'JYPAIR1|S|{ID}|{NONCE}|PAIR_SCREEN_LOCKED_OR_OFF',KEY).encode(),SOURCE);self.assertEqual(self.exchange.status,'PAIR_SCREEN_LOCKED_OR_OFF');self.assertTrue(self.exchange.settled())
    def test_malformed_or_oversize_packets_rejected(self):
        self.ready()
        for packet in [None,b'',b'x'*1025,b'\xff',b'JYPAIR1|P']:
            self.exchange.handle(packet,SOURCE);self.assertIsNone(self.exchange.take())
    def test_requests_block_business_resumption_until_phone_ui_settles(self):
        self.assertTrue(self.exchange.settled());self.ready();self.assertFalse(self.exchange.settled());self.now+=41;self.assertTrue(self.exchange.settled())


class Ledger(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'ledger.json'
    def tearDown(self):self.temp.cleanup()
    def test_two_attempts_across_new_callers(self):self.assertTrue(pair.reserve(self.path,'01'*32,now=1000));self.assertTrue(pair.reserve(self.path,'02'*32,now=1001));self.assertFalse(pair.reserve(self.path,'03'*32,now=1002))
    def test_duplicate_nonce_never_replays(self):self.assertTrue(pair.reserve(self.path,NONCE,now=1000));self.assertFalse(pair.reserve(self.path,NONCE,now=1001))
    def test_expired_budget_allows_new_nonce(self):pair.reserve(self.path,'01'*32,now=1000);pair.reserve(self.path,'02'*32,now=1001);self.assertTrue(pair.reserve(self.path,'03'*32,now=1601))
    def test_clock_reversal_denied(self):pair.reserve(self.path,NONCE,now=1000);self.assertRaises(ValueError,pair.reserve,self.path,'01'*32,now=999)
    def test_corrupt_storage_denied(self):self.path.write_text('*');self.assertRaises(ValueError,pair.reserve,self.path,NONCE,now=1000)
    def test_bad_nonce_does_not_create_ledger(self):self.assertFalse(pair.reserve(self.path,'*',now=1000));self.assertFalse(self.path.exists())
    def test_ledger_has_no_pairing_secret(self):pair.reserve(self.path,NONCE,now=1000);self.assertNotIn('042819',self.path.read_text());self.assertEqual(set(json.loads(self.path.read_bytes())),{'schema','attempts'})
    def test_failed_replace_does_not_consume_success(self):
        with mock.patch.object(pair.os,'replace',side_effect=OSError):self.assertRaises(OSError,pair.reserve,self.path,NONCE,now=1000)
        self.assertFalse(self.path.exists());self.assertFalse(list(self.path.parent.glob('*.part')))


class Adapter(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.events=[];self.calls=[]
        config=dict(adb_path=wire.PINNED_ADB_PATH,expected_identity_sha256=hashlib.sha256(b'owned-phone').hexdigest(),log_path=str(Path(self.temp.name)/'wire.jsonl'))
        self.client=wire.WirelessADB(config,runner=self.runner,server_version_probe=lambda _:0x29,binary_digest_reader=lambda _:wire.PINNED_ADB_SHA256)
        self.client._log=lambda event,**kw:self.events.append((event,kw))
        self.client.config['pairing_recovery']=dict(enabled=True,host_key_sha256=HOST,ledger_path=str(Path(self.temp.name)/'ledger.json'))
        self.client._pair_auth_failure=True;self.client._reported_ip=SOURCE[0];self.client._pair_auth_ips={SOURCE[0]}
        self.client._pair_exchange=SimpleNamespace(take=lambda:(SOURCE[0],37111,'042819',37222,NONCE,1000))
    def tearDown(self):self.temp.cleanup()
    def runner(self,args,**kwargs):
        self.calls.append((args,kwargs));return subprocess.CompletedProcess(args,0,'Successfully paired to test\n','')
    def run_pair(self):
        with mock.patch.object(pair,'validate'):
            return self.client._try_pairing(None)
    def test_code_is_stdin_not_command_argument_and_identity_required(self):
        self.client._verify=mock.Mock(return_value=True);self.assertEqual(self.run_pair(),SOURCE[0]+':37222')
        args,kw=self.calls[0];self.assertNotIn('042819',' '.join(args));self.assertEqual(kw['input'],'042819\n');self.assertNotIn('stdin',kw)
        self.assertEqual(args[1:5],['-H','127.0.0.1','-P','5037']);self.client._verify.assert_called_once_with(SOURCE[0]+':37222',connect=True,operation_deadline=None)
    def test_success_message_does_not_replace_phone_identity(self):self.client._verify=mock.Mock(return_value=False);self.assertIsNone(self.run_pair());self.assertNotIn('pairing_recovery_identity_verified',[e for e,_ in self.events])
    def test_no_authentication_rejection_means_no_pair(self):self.client._pair_auth_failure=False;self.assertIsNone(self.run_pair());self.assertFalse(self.calls)
    def test_foreign_address_auth_failure_cannot_trigger_pair(self):self.client._pair_auth_ips={'192.168.50.101'};self.assertIsNone(self.run_pair());self.assertFalse(self.calls)
    def test_stale_reported_address_cannot_trigger_pair(self):self.client._reported_ip=None;self.assertIsNone(self.run_pair());self.assertFalse(self.calls)
    def test_pair_timeout_never_replays_within_operation(self):
        self.client.runner=mock.Mock(side_effect=subprocess.TimeoutExpired('pinned',1));self.assertIsNone(self.run_pair());self.assertIsNone(self.run_pair());self.assertEqual(self.client.runner.call_count,1)
    def test_failed_pair_never_runs_identity_command_as_success(self):
        self.client.runner=mock.Mock(return_value=subprocess.CompletedProcess([],1,'','failed'));self.client._verify=mock.Mock();self.assertIsNone(self.run_pair());self.client._verify.assert_not_called()
    def test_daemon_mismatch_never_invokes_cli(self):
        self.client.server_version_probe=lambda _:0x28
        with self.assertRaises(wire.ConnectionUnavailable):self.run_pair()
        self.assertFalse(self.calls)
    def test_ordinary_connection_failure_does_not_authorize_pair(self):
        self.client._pair_auth_failure=False;self.client._pair_auth_ips=set();self.client.runner=mock.Mock(return_value=subprocess.CompletedProcess([],1,'','connection refused'))
        self.assertFalse(self.client._verify(SOURCE[0]+':37222',connect=True));self.assertFalse(self.client._pair_auth_failure)
    def test_auth_rejection_is_classified_without_raw_output(self):
        self.client._pair_auth_failure=False;self.client._pair_auth_ips=set();self.client.runner=mock.Mock(return_value=subprocess.CompletedProcess([],1,'failed to authenticate to private endpoint',''))
        self.assertFalse(self.client._verify(SOURCE[0]+':37222',connect=True));self.assertEqual(self.client._pair_auth_ips,{SOURCE[0]});self.assertNotIn('private endpoint',repr(self.events))
    def test_only_pair_operation_accepts_sensitive_input(self):
        with self.assertRaises(wire.ConnectionUnavailable):self.client._exec(['shell','echo'],1,input_text='042819\n')
        self.assertFalse(self.calls)


if __name__=='__main__':unittest.main()
