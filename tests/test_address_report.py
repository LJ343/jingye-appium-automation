"""Synthetic protocol, cache and recovery integration tests; no actual network."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import queue
import socket
import threading
import subprocess
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / 'workspace/skills/jingye-reconciliation/scripts'))
import jingye_wireless_address as report

ID = '01' * 16
KEY = '02' * 32
NONCE = '03' * 16
CHALLENGE = '04' * 32
IP = '192.168.50.100'
DIGEST = hashlib.sha256(b'synthetic-phone').hexdigest()


def config(path):
    return dict(enabled=True, listen_ipv4='10.10.10.2', listen_port=37729,
                reporter_id=ID, shared_key_hex=KEY, allowed_ipv4_cidrs=['192.168.50.0/24'],
                verified_cache_path=str(path))


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg = config(Path(self.tmp.name) / 'verified.json')
        self.now = 0.0
        self.p = report.Handshake(self.cfg, clock=lambda: self.now, nonce=lambda: CHALLENGE)
        self.peer = (IP, 12345)
    def packet(self, kind='A', ip=IP, nonce=NONCE, challenge=CHALLENGE):
        fields = ['JYADDR1', kind, ID, nonce]
        fields += [challenge, ip] if kind == 'R' else [ip]
        return report.sign('|'.join(fields), bytes.fromhex(KEY)).encode('ascii')
    def complete(self, ip=IP):
        peer = (ip, 12345)
        self.assertIsNotNone(self.p.handle(self.packet(ip=ip), peer))
        self.p.handle(self.packet('R', ip), peer)
    def test_hello_alone_never_accepts(self):
        reply = self.p.handle(self.packet(), self.peer)
        self.assertIsNone(self.p.latest_ip())
        self.assertEqual(report.decode(reply, bytes.fromhex(KEY)), ['JYADDR1','C',ID,NONCE,CHALLENGE])
    def test_fresh_challenge_response_accepts(self):
        self.complete(); self.assertEqual(self.p.latest_ip(), IP)
    def test_ip_changes_are_authenticated_individually(self):
        self.complete(); self.complete('192.168.50.53'); self.assertEqual(self.p.latest_ip(), '192.168.50.53')
    def test_replay_without_live_challenge_rejected(self):
        self.p.handle(self.packet('R'), self.peer); self.assertIsNone(self.p.latest_ip())
        self.complete(); self.now = 46; self.p.handle(self.packet('R'), self.peer); self.assertIsNone(self.p.latest_ip())
    def test_challenge_expires(self):
        self.p.handle(self.packet(), self.peer); self.now = 6
        self.p.handle(self.packet('R'), self.peer); self.assertIsNone(self.p.latest_ip())
    def test_wrong_source_ip_or_port_rejected(self):
        self.assertIsNone(self.p.handle(self.packet(), ('192.168.50.101', 12345)))
        self.p.handle(self.packet(), self.peer)
        self.p.handle(self.packet('R'), (IP, 12346)); self.assertIsNone(self.p.latest_ip())
    def test_bad_mac_id_nonce_and_challenge_rejected(self):
        for packet in [self.packet()[:-1]+b'0', b'x'*385, b'\xff',
                       report.sign('JYADDR1|A|'+'ff'*16+'|'+NONCE+'|'+IP,bytes.fromhex(KEY)).encode(),
                       self.packet(nonce='invalid')]:
            self.assertIsNone(self.p.handle(packet,self.peer))
        self.p.handle(self.packet(),self.peer)
        self.p.handle(self.packet('R',challenge='09'*32),self.peer)
        self.assertIsNone(self.p.latest_ip())
    def test_out_of_scope_network_and_broadcast_rejected(self):
        for ip in ['192.168.21.1','192.168.50.0','192.168.50.255','127.0.0.1','8.8.8.8']:
            self.assertIsNone(self.p.handle(self.packet(ip=ip),(ip,12345)))
    def test_pending_set_bounded(self):
        for i in range(20):self.p.handle(self.packet(nonce=f'{i:032x}'),self.peer)
        self.assertLessEqual(len(self.p.pending),8)
    def test_stale_accepted_address_expires(self):
        self.complete();self.now=45.01;self.assertIsNone(self.p.latest_ip())
    def test_bad_config_scope_and_secrets_rejected(self):
        for field,value in [('allowed_ipv4_cidrs',['192.168.0.0/16']),('shared_key_hex','bad'),
                            ('listen_ipv4','0.0.0.0'),('listen_port',True),('reporter_id','bad')]:
            c=dict(self.cfg);c[field]=value
            with self.assertRaises((ValueError,TypeError)):report.validate_config(c)
    def test_verified_cache_roundtrip_and_status_reader(self):
        ep=IP+':42383';report.save_verified_endpoint(self.cfg,ep,DIGEST)
        self.assertEqual(report.read_verified_endpoint(self.cfg,DIGEST),ep)
        spec=importlib.util.spec_from_file_location('candidate_status',HERE/'operations/jingye_wireless_status.py')
        status=importlib.util.module_from_spec(spec);spec.loader.exec_module(status)
        path=Path(self.tmp.name)/'config.json'
        path.write_text(json.dumps(dict(expected_identity_sha256=DIGEST,discovery=dict(allowed_ipv4=['192.168.50.53']),address_report=self.cfg)))
        self.assertEqual(status.read_config(path),(IP,DIGEST))
    def test_tampered_cache_and_wrong_phone_rejected(self):
        ep=IP+':42383';report.save_verified_endpoint(self.cfg,ep,DIGEST)
        self.assertIsNone(report.read_verified_endpoint(self.cfg,'f'*64))
        p=Path(self.cfg['verified_cache_path']);value=json.loads(p.read_text());value['endpoint']='192.168.50.200:42383';p.write_text(json.dumps(value))
        self.assertIsNone(report.read_verified_endpoint(self.cfg,DIGEST))
    def test_expired_cache_and_future_timestamp_rejected(self):
        with patch.object(report.time,'time',return_value=100000):report.save_verified_endpoint(self.cfg,IP+':42383',DIGEST)
        for now in [99999,186401]:
            with patch.object(report.time,'time',return_value=now):self.assertIsNone(report.read_verified_endpoint(self.cfg,DIGEST))
    def test_receipt_does_not_create_verified_cache(self):
        self.complete();self.assertFalse(Path(self.cfg['verified_cache_path']).exists())


class IntegrationTests(unittest.TestCase):
    def client(self, directory):
        import jingye_wireless as wireless
        cfg=dict(adb_path=wireless.PINNED_ADB_PATH,expected_identity_sha256=DIGEST,
                 log_path=str(Path(directory)/'events.jsonl'),max_wait_seconds=3,
                 address_report=config(Path(directory)/'verified.json'))
        return wireless.WirelessADB(cfg,binary_digest_reader=lambda p:wireless.PINNED_ADB_SHA256,server_version_probe=lambda t:0x29)
    def test_existing_connection_never_opens_receiver(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.client(td)
            with patch.object(c,'_candidates',return_value=iter([(IP+':42383',False)])),patch.object(c,'_verify',return_value=True),patch.object(report,'AddressReceiver') as receiver:
                self.assertEqual(c.ensure_connected(),IP+':42383');receiver.assert_not_called()
    def test_wrong_phone_never_saves_verified_cache(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.client(td);c._reported_ip=IP
            def execute(args,*a,**kw):return subprocess.CompletedProcess(args,0,'device' if args[-1]=='get-state' else 'wrong-phone','')
            with patch.object(c,'_exec',side_effect=execute):self.assertFalse(c._verify(IP+':42383'))
            self.assertFalse((Path(td)/'verified.json').exists())
    def test_failed_operation_closes_receiver_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.client(td)
            class Receiver:
                closed=False
                def close(self):self.closed=True
            receiver=Receiver()
            with self.assertRaises(ValueError):
                with c._operation():
                    c._address_receiver=receiver
                    raise ValueError('synthetic')
            self.assertTrue(receiver.closed);self.assertIsNone(c._address_receiver)
            self.assertTrue(c.lock.acquire(blocking=False));c.lock.release()
    def test_udp_worker_handles_exchange_and_closes_without_real_network(self):
        with tempfile.TemporaryDirectory() as td:
            receiver=report.AddressReceiver(config(Path(td)/'verified.json'))
            hello=report.sign('JYADDR1|A|'+ID+'|'+NONCE+'|'+IP,bytes.fromhex(KEY)).encode()
            done=threading.Event()
            class FakeSocket:
                closed=False
                bound=None
                def __init__(self):self.inbox=queue.Queue();self.inbox.put((hello,(IP,12345)))
                def setsockopt(self,*a):pass
                def bind(self,target):self.bound=target
                def settimeout(self,value):pass
                def recvfrom(self,*a):
                    try:return self.inbox.get(timeout=.02)
                    except queue.Empty:
                        if self.closed:raise OSError()
                        if receiver.latest_ip()==IP:done.set()
                        raise socket.timeout()
                def sendto(self,packet,peer):
                    parts=report.decode(packet,bytes.fromhex(KEY))
                    response=report.sign('|'.join(['JYADDR1','R',ID,NONCE,parts[4],IP]),bytes.fromhex(KEY)).encode()
                    self.inbox.put((response,peer))
                def close(self):self.closed=True
            fake=FakeSocket()
            with patch.object(report.socket,'socket',return_value=fake):
                receiver.start()
                self.assertTrue(done.wait(timeout=1));receiver.close()
            self.assertEqual(fake.bound,('10.10.10.2',37729));self.assertTrue(fake.closed);self.assertFalse(receiver.thread.is_alive())
    def test_new_ip_is_single_discovery_target_and_receiver_closes(self):
        import jingye_wireless as wireless
        import jingye_wireless_discovery as discovery
        with tempfile.TemporaryDirectory() as td:
            cfg=dict(adb_path=wireless.PINNED_ADB_PATH,expected_identity_sha256=DIGEST,
                     log_path=str(Path(td)/'events.jsonl'),max_wait_seconds=3,
                     address_report=config(Path(td)/'verified.json'),
                     discovery=dict(enabled=True,allowed_ipv4=['192.168.50.53'],port_range=[42383,42383]))
            c=wireless.WirelessADB(cfg,binary_digest_reader=lambda p:wireless.PINNED_ADB_SHA256,server_version_probe=lambda t:0x29)
            events=[]
            class Receiver:
                def __init__(self,cfg):pass
                def start(self):events.append('start');return self
                def latest_ip(self):return IP
                def close(self):events.append('close')
            class Discovery:
                last_report={'reason':'verified_scan'}
                def __init__(self,cfg):self.cfg=cfg;events.append(tuple(cfg['allowed_ipv4']))
                def discover(self,**kw):
                    endpoint=self.cfg['allowed_ipv4'][0]+':42383'
                    kw['verify_candidate'](endpoint,1)
                    return endpoint
            def verify(ep,**kw):c.serial=ep;return True
            with patch.object(c,'_candidates',return_value=iter(())),patch.object(c,'_verify',side_effect=verify),patch.object(report,'AddressReceiver',Receiver),patch.object(discovery,'EndpointDiscovery',Discovery):
                self.assertEqual(c.ensure_connected(),IP+':42383')
            self.assertEqual(events,['start',(IP,),'close'])
    def test_unauthenticated_report_cannot_replace_scope(self):
        cfg=config(Path(tempfile.gettempdir())/'unused.json');p=report.Handshake(cfg)
        body='JYADDR1|A|'+ID+'|'+NONCE+'|'+IP
        p.handle(report.sign(body,b'wrong').encode(),(IP,12345))
        self.assertIsNone(p.latest_ip())

if __name__=='__main__':unittest.main()
