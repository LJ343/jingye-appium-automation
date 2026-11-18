"""Opt-in pairing with one pinned computer over the existing authenticated rendezvous.

Requests require explicit opt-in plus concrete authentication failure, or a
completed unsuccessful discovery for a fresh authenticated phone address.
The original pinned computer and phone network/unlock checks remain required.
Pairing codes are
AES-GCM encrypted, never logged, persisted or placed in command-line arguments.
Pairing success is not device identity verification; the normal caller must do it.
"""
from __future__ import annotations
import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

MAGIC='JYPAIR1'
HEX64=re.compile(r'[0-9a-f]{64}')


def pair_result_code(returncode, stdout, stderr, endpoint):
    """Classify the pinned ADB reply without retaining or exposing its text.

    commandline.cpp prints the stdin prompt without a newline. With piped
    input, the success reply therefore follows that prompt on the same line.
    A zero exit status alone is insufficient; require the exact requested peer.
    """
    if returncode != 0:return 'NONZERO_EXIT'
    if not isinstance(stdout,str) or not isinstance(stderr,str):return 'INVALID_OUTPUT'
    if stderr.strip():return 'UNEXPECTED_STDERR'
    if len(stdout)>1024:return 'UNRECOGNIZED_REPLY'
    reply=stdout.strip()
    success=r'Successfully paired to '+re.escape(endpoint)+r'(?: \[guid=[A-Za-z0-9_.:-]{1,200}\])?'
    if re.fullmatch(success,reply):return 'SUCCESS'
    if re.fullmatch(r'Enter pairing code: ?'+success,reply):return 'SUCCESS_INLINE_PROMPT'
    if re.fullmatch(r'Enter pairing code: ?\r?\n'+success,reply):return 'SUCCESS_PROMPT_NEWLINE'
    return 'UNRECOGNIZED_REPLY'


def host_key_fingerprint(path=None):
    raw=(Path(path) if path is not None else Path.home()/'.android/adbkey.pub').read_bytes()
    if len(raw)>16384:raise ValueError('pairing_host_key_invalid')
    try:key=base64.b64decode(raw.split()[0],validate=True)
    except (ValueError,IndexError):raise ValueError('pairing_host_key_invalid') from None
    if len(key)!=524:raise ValueError('pairing_host_key_invalid')
    return hashlib.sha256(key).hexdigest()


def validate(config):
    if (not isinstance(config,dict) or not {'enabled','host_key_sha256','ledger_path'}<=set(config)
            or set(config)-{'enabled','host_key_sha256','ledger_path','allow_verified_discovery_repair'}
            or ('allow_verified_discovery_repair' in config and type(config['allow_verified_discovery_repair']) is not bool)
            or config['enabled'] is not True or not isinstance(config['host_key_sha256'],str)
            or not HEX64.fullmatch(config['host_key_sha256'])
            or not Path(config['ledger_path']).is_absolute()):
        raise ValueError('pairing_config_invalid')
    if host_key_fingerprint()!=config['host_key_sha256']:raise ValueError('pairing_host_key_changed')


def sign(body,key):
    return body+'|'+hmac.new(key,body.encode('ascii'),hashlib.sha256).hexdigest()


def decode(packet,key):
    if not isinstance(packet,bytes) or not 1<=len(packet)<=1024:return None
    try:
        text=packet.decode('ascii');body,mac=text.rsplit('|',1)
        if not HEX64.fullmatch(mac) or not hmac.compare_digest(sign(body,key),text):return None
        fields=body.split('|')
        return fields if fields[0]==MAGIC else None
    except (ValueError,UnicodeError):return None


def cipher_key(key):
    return hmac.new(key,b'jingye-pairing-aes-gcm-v1',hashlib.sha256).digest()


class PairExchange:
    """No I/O or ADB. One request per receiver, fresh source/nonce/expiry bound."""
    def __init__(self,report_config,pair_config,*,clock=time.monotonic,wall=time.time,nonce=lambda:secrets.token_hex(32)):
        self.key=bytes.fromhex(report_config['shared_key_hex']);self.reporter=report_config['reporter_id']
        self.fingerprint=pair_config['host_key_sha256'];self.clock=clock;self.wall=wall;self.nonce=nonce
        self.lock=threading.Lock();self.enabled=False;self.pending=None;self.accepted=None;self.status=None
        self.consumed=False;self.authorized_at=None

    def authorize(self):
        with self.lock:
            if not self.enabled:self.authorized_at=self.clock()
            self.enabled=True

    def request(self,source):
        with self.lock:
            if not self.enabled or self.pending is not None or self.consumed:return None
            if self.authorized_at is None or self.clock()-self.authorized_at>25:
                self.status='PAIR_LOCAL_REQUEST_TIMEOUT';self.consumed=True;return None
            token=self.nonce();expires=int(self.wall())+40
            if not HEX64.fullmatch(token):raise ValueError('pairing_nonce_invalid')
            self.pending=(source,token,self.clock()+40)
            body='|'.join((MAGIC,'Q',self.reporter,token,self.fingerprint,str(expires)))
            return sign(body,self.key).encode('ascii')

    def handle(self,packet,source):
        fields=decode(packet,self.key)
        with self.lock:
            pending=self.pending
            if (fields is None or len(fields)!=5 or pending is None or self.accepted is not None
                    or source!=pending[0] or self.clock()>pending[2]
                    or fields[2]!=self.reporter or fields[3]!=pending[1]):return
            if fields[1]=='S':
                if re.fullmatch(r'PAIR_[A-Z0-9_]{1,70}',fields[4]):self.status=fields[4]
                return
            if fields[1]!='P':return
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                raw=base64.b64decode(fields[4],validate=True)
                if not 28<len(raw)<256:return
                aad='|'.join((MAGIC,'P',self.reporter,pending[1],self.fingerprint)).encode('ascii')
                text=AESGCM(cipher_key(self.key)).decrypt(raw[:12],raw[12:],aad).decode('ascii')
                ip,port,code,connection_port=text.split('|')
                if (ip!=source[0] or not port.isdigit() or not connection_port.isdigit()
                        or not 1024<=int(port)<=65535 or not 1024<=int(connection_port)<=65535
                        or port==connection_port or not re.fullmatch(r'[0-9]{6}',code)):return
                self.accepted=(ip,int(port),code,int(connection_port),pending[1],self.clock()+20)
            except Exception:
                # Nothing derived from untrusted plaintext is logged or exposed.
                return

    def take(self):
        with self.lock:
            if self.consumed or self.accepted is None:return None
            self.consumed=True;value=self.accepted;self.accepted=None
            if self.clock()<=value[-1]:return value
            self.status='PAIR_LOCAL_CODE_EXPIRED';return None

    def active(self):
        """Pause slow scans while a bounded request/code is in flight, including races."""
        with self.lock:
            if self.accepted is not None:return True
            if not self.enabled or self.consumed or self.status is not None:return False
            if self.pending is None:
                if self.authorized_at is not None and self.clock()-self.authorized_at<=25:return True
                self.status='PAIR_LOCAL_REQUEST_TIMEOUT';return False
            if self.clock()<=self.pending[2]:return True
            self.status='PAIR_LOCAL_RESPONSE_TIMEOUT';return False

    def settled(self):
        with self.lock:
            return (self.pending is None or self.accepted is not None or self.status is not None
                    or self.clock()>self.pending[2])


@contextmanager
def _locked(path):
    lock=Path(str(path)+'.lock');lock.parent.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(lock,os.O_RDWR|os.O_CREAT,0o600)
    try:
        if os.fstat(descriptor).st_size==0:os.write(descriptor,b'0')
        os.lseek(descriptor,0,os.SEEK_SET)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(descriptor,msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:
            os.lseek(descriptor,0,os.SEEK_SET)
            if os.name=='nt':msvcrt.locking(descriptor,msvcrt.LK_UNLCK,1)
            else:fcntl.flock(descriptor,fcntl.LOCK_UN)
    finally:os.close(descriptor)


def reserve(path,nonce,*,now=None):
    """Durable before pair command. Shared limit two / ten minutes across runs."""
    if not isinstance(nonce,str) or not HEX64.fullmatch(nonce):return False
    now=int(time.time()) if now is None else now
    if type(now) is not int or now<=0:return False
    path=Path(path)
    with _locked(path):
        if path.exists():
            if path.stat().st_size>4096:raise ValueError('pairing_ledger_invalid')
            value=json.loads(path.read_bytes())
            if set(value)!={'schema','attempts'} or value['schema']!=1 or not isinstance(value['attempts'],list):raise ValueError('pairing_ledger_invalid')
            rows=value['attempts']
        else:rows=[]
        if len(rows)>2:raise ValueError('pairing_ledger_invalid')
        fresh=[]
        for row in rows:
            if (not isinstance(row,dict) or set(row)!={'at','nonce'} or type(row['at']) is not int
                    or not 0<row['at']<=now or not isinstance(row['nonce'],str)
                    or not HEX64.fullmatch(row['nonce'])):raise ValueError('pairing_ledger_invalid')
            if row['nonce']==nonce:return False
            if now-row['at']<600:fresh.append(row)
        if len(fresh)>=2:return False
        fresh.append(dict(at=now,nonce=nonce))
        temporary=path.with_name(path.name+'.'+secrets.token_hex(8)+'.part')
        try:
            with temporary.open('x',encoding='ascii') as stream:
                json.dump(dict(schema=1,attempts=fresh),stream);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)
        return True
