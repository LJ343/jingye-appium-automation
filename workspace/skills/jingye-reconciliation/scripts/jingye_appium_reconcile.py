"""Read-only proof for a known session's unresolved cleanup, across clients.

No session creation/deletion, signals, settings, forwards or service control.
The journal owner holds its lock throughout this proof and the final archival.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import time

import jingye_appium_ownership as phone

UUID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'

class ReconciliationError(RuntimeError):
    pass

def require(value, code):
    if not value:raise ReconciliationError(code)

def utc(value):
    parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    require(parsed.tzinfo is not None,'cleanup_timestamp_timezone_missing')
    return parsed.astimezone(dt.timezone.utc)

def query_client(pid):
    """Fresh CIM identity only. No command line, service or signal operations."""
    require(type(pid) is int and pid>0,'cleanup_client_pid_invalid')
    require(os.name=='nt','cleanup_client_windows_required')
    executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    script='''$ErrorActionPreference='Stop'; try {
 $items=@(Get-CimInstance Win32_Process -Filter 'ProcessId=PID_VALUE' -ErrorAction Stop)
 if($items.Count -gt 1){throw 'ambiguous'}
 if($items.Count -eq 0){'null'}else{
  [ordered]@{pid=[int]$items[0].ProcessId;createdAtUtc=$items[0].CreationDate.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')} | ConvertTo-Json -Compress
 }
}catch{exit 1}'''.replace('PID_VALUE',str(pid))
    try:
        result=subprocess.run([str(executable),'-NoProfile','-NonInteractive','-Command',script],
            capture_output=True,text=True,encoding='utf-8',errors='strict',timeout=15,stdin=subprocess.DEVNULL)
        require(result.returncode==0,'cleanup_client_query_failed')
        value=json.loads(result.stdout.lstrip('\ufeff'))
        require(value is None or isinstance(value,dict) and value.get('pid')==pid
                and isinstance(value.get('createdAtUtc'),str),'cleanup_client_query_invalid')
        return value
    except (OSError,ValueError,subprocess.SubprocessError):
        raise ReconciliationError('cleanup_client_query_failed') from None

def original_client_gone(record, probe=query_client):
    pid=record.get('clientPid');require(type(pid) is int and pid>0,'cleanup_client_pid_invalid')
    observed=probe(pid)
    if observed is None:return True
    require(isinstance(observed,dict) and observed.get('pid')==pid,'cleanup_client_query_invalid')
    # A reused Windows PID is not the original client. Missing/ambiguous birth
    # evidence never permits treating a potentially live original as dead.
    require(utc(observed['createdAtUtc'])>utc(record['atUtc'])+dt.timedelta(seconds=1),
            'cleanup_original_client_still_present')
    return True

class MasterIdleAudit:
    """Stream a single birth-matched startup log under a total deadline."""
    def __init__(self,host,sid,deadline,clock=time.monotonic,root=None):
        self.clock,self.deadline=clock,deadline
        self.root=Path(root or os.environ.get('JINGYE_APPIUM_LOG_ROOT',r'D:\openclaw_data\logs\appium'))
        require(self.root.is_absolute(),'cleanup_log_root_invalid')
        candidates=[]
        for path in self.root.glob('*.stdout.log'):
            match=re.fullmatch(r'(\d{8}T\d{9}Z)-[0-9a-f]{32}\.stdout\.log',path.name)
            if not match:continue
            at=dt.datetime.strptime(match[1],'%Y%m%dT%H%M%S%fZ').replace(tzinfo=dt.timezone.utc)
            if -10<=(at-utc(host['createdAtUtc'])).total_seconds()<=2:candidates.append(path)
        require(len(candidates)==1,'cleanup_host_log_not_unique');self.path=candidates[0]
        active=set();added=set();removed=set();pending=None;deleting=set();target_deletes=0;digest=hashlib.sha256()
        start=re.compile(r'\[HTTP\]\s+--> POST /session(?:\s|$)')
        end=re.compile(r'\[HTTP\]\s+<-- POST /session (\d{3})(?:\s|$)')
        add=re.compile(r'session ('+UUID+r') added to master session list',re.I)
        remove=re.compile(r'Removing session ('+UUID+r') from our master session list',re.I)
        ds=re.compile(r'\[HTTP\]\s+--> DELETE /session/('+UUID+r')(?:\s|$)')
        de=re.compile(r'\[HTTP\]\s+<-- DELETE /session/('+UUID+r') (\d{3})(?:\s|$)')
        with self.path.open('rb') as stream:
            stat=os.fstat(stream.fileno());self.identity=(stat.st_dev,stat.st_ino)
            self.header=stream.read(16384)
            require(b'Welcome to Appium' in self.header and b'listener started' in self.header,'cleanup_log_header_missing')
            stream.seek(0,2);self.offset=stream.tell();require(self.offset>0,'cleanup_log_empty')
            stream.seek(0)
            while stream.tell()<self.offset:
                require(clock()<deadline,'cleanup_reconciliation_budget_exhausted')
                raw=stream.readline(min(1024*1024,self.offset-stream.tell()))
                require(raw and raw.endswith(b'\n'),'cleanup_log_partial_or_oversize_line')
                digest.update(raw)
                if b'[HTTP]' not in raw and b'master session list' not in raw:continue
                line=raw.decode('utf-8',errors='strict')
                if start.search(line):
                    require(pending is None,'cleanup_log_overlapping_creation');pending=[]
                match=add.search(line)
                if match:
                    key=match[1].lower();require(pending is not None and key not in added,'cleanup_log_master_add_unpaired')
                    added.add(key);active.add(key);pending.append(key)
                match=remove.search(line)
                if match:
                    key=match[1].lower();require(key in active and key not in removed,'cleanup_log_master_remove_unpaired')
                    active.remove(key);removed.add(key)
                match=end.search(line)
                if match:
                    require(pending is not None,'cleanup_log_creation_terminal_unpaired')
                    require((200<=int(match[1])<300 and len(pending)==1)
                            or (not 200<=int(match[1])<300 and not pending),'cleanup_log_creation_outcome_ambiguous')
                    pending=None
                match=ds.search(line)
                if match:
                    key=match[1].lower();require(key not in deleting,'cleanup_log_overlapping_delete');deleting.add(key)
                match=de.search(line)
                if match:
                    key=match[1].lower();require(key in deleting,'cleanup_log_delete_terminal_unpaired');deleting.remove(key)
                    if key==sid.lower() and int(match[2])==200:target_deletes+=1
        require(not active and pending is None and not deleting,'cleanup_master_not_idle')
        require(sid.lower() in added and sid.lower() in removed and target_deletes==1,'cleanup_exact_delete_not_proven')
        self.prefix_sha256=digest.hexdigest()

    def unchanged_since_idle(self):
        require(self.clock()<self.deadline,'cleanup_reconciliation_budget_exhausted')
        with self.path.open('rb') as stream:
            stat=os.fstat(stream.fileno());require((stat.st_dev,stat.st_ino)==self.identity,'cleanup_log_identity_changed')
            require(stream.read(len(self.header))==self.header,'cleanup_log_header_changed')
            stream.seek(0,2);size=stream.tell()
            require(self.offset<=size<=self.offset+2*1024*1024,'cleanup_log_tail_size_invalid')
            stream.seek(self.offset);tail=stream.read(size-self.offset)
        require(not tail or tail.endswith(b'\n'),'cleanup_log_tail_partial')
        for line in tail.decode('utf-8',errors='strict').splitlines():
            require(not re.search(r'\[HTTP\]\s+--> (?:POST|DELETE|PUT|PATCH) ',line)
                    and 'master session list' not in line,'cleanup_master_changed_during_proof')
        return True

def old_session_ended(port,sid,deadline,clock=time.monotonic):
    require(re.fullmatch(UUID,sid) and 0<port<=65535,'cleanup_session_identity_invalid')
    left=deadline-clock();require(left>0,'cleanup_reconciliation_budget_exhausted')
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=min(8,left))
    try:
        connection.request('GET','/session/'+sid+'/timeouts',headers={'Accept':'application/json','Connection':'close'})
        response=connection.getresponse();raw=response.read(65537)
        require(len(raw)<=65536,'cleanup_session_response_oversize')
        body=json.loads(raw)
        require(response.status==404 and isinstance(body,dict) and isinstance(body.get('value'),dict)
                and body['value'].get('error')=='invalid session id','cleanup_old_session_not_terminal')
        return True
    except (OSError,http.client.HTTPException,ValueError):
        raise ReconciliationError('cleanup_old_session_query_failed') from None
    finally:connection.close()

def prove_cleanup_idle(guard,link,record,host,*,client_probe=query_client,clock=time.monotonic,
                       log_root=None,audit_factory=MasterIdleAudit,session_probe=old_session_ended):
    require(record.get('state')=='cleanup_uncertain' and record.get('scope')=='host'
            and record.get('host')==host and isinstance(record.get('sessionId'),str)
            and re.fullmatch(UUID,record['sessionId']),'cleanup_record_not_eligible')
    deadline=clock()+90
    original_client_gone(record,client_probe)
    require(guard._current_host()==host,'cleanup_appium_host_changed')
    audit=audit_factory(host,record['sessionId'],deadline,clock,log_root)
    session_probe(guard.port,record['sessionId'],deadline,clock)
    owner=phone.PhoneOwnership(link,'http://127.0.0.1:'+str(guard.port),host,guard.host_probe)
    require(clock()<deadline,'cleanup_reconciliation_budget_exhausted')
    with owner._operation(min(60,deadline-clock())) as phone_deadline:
        first=owner._snapshot(phone_deadline)
        require(not first['processes'] and not first['instruments'] and not first['forwards'],'cleanup_phone_not_idle')
        second=owner._snapshot(phone_deadline)
        require(second==first,'cleanup_phone_snapshot_changed')
    original_client_gone(record,client_probe)
    session_probe(guard.port,record['sessionId'],deadline,clock)
    audit.unchanged_since_idle()
    require(guard._current_host()==host and clock()<deadline,'cleanup_host_or_budget_changed')
    return dict(originalClientGone=True,masterLogIdle=True,exactSessionTerminal=True,samePhoneVerified=True,
                physicalIdleObservations=2,processCount=0,instrumentationCount=0,forwardCount=0,
                masterLogPrefixSHA256=audit.prefix_sha256)
