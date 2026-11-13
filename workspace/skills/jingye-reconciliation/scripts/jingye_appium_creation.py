"""Bounded, read-only evidence for a failed creation on the same local host.

This is not an idle detector. A checkpoint is taken after the phone's verified
idle baseline and before Remote. Only one exact token-bearing request, a final
HTTP 500, and no competing mutation/master addition can discharge that request.
Phone identity, process/socket ownership and physical cleanup are separate.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re


class CreationEvidenceError(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise CreationEvidenceError(code)


def empty_sessions(value):
    return (isinstance(value, dict) and "error" not in value
            and type(value.get("value")) is list and value["value"] == [])


def audit_suffix(raw, token):
    require(isinstance(raw, bytes) and len(raw) <= 8 * 1024 * 1024
            and raw.endswith(b"\n"), "creation_log_suffix_incomplete")
    text = raw.decode("utf-8", errors="strict")
    lines = text.splitlines()
    starts = []; ends = []
    for i, line in enumerate(lines):
        match = re.search(r"\[HTTP\]\s+--> (\S+) (\S+)(?: (.*))?$", line)
        if match:
            method, route, body = match.groups()
            if method == "POST" and route == "/session":
                require(body is not None, "creation_request_body_missing")
                request = json.loads(body)
                caps = request.get("capabilities", {})
                require(caps.get("alwaysMatch", {}).get("jingye:ownershipToken") == token
                        and caps.get("firstMatch") == [{}], "creation_request_token_mismatch")
                starts.append(i)
            else:
                require(method == "GET", "creation_competing_mutation")
        end = re.search(r"\[HTTP\]\s+<-- POST /session (\d{3})(?:\s|$)", line)
        if end:
            ends.append((i, int(end[1])))
        require(not re.search(r"session .* added to master session list", line, re.I),
                "creation_master_session_added")
    require(len(starts) == len(ends) == 1 and starts[0] < ends[0][0]
            and ends[0][1] == 500, "creation_failed_terminal_not_proven")
    # Do not treat an arbitrary HTTP 500 or a truncated request as instrumentation failure.
    segment = "\n".join(lines[starts[0]:ends[0][0]+1])
    launched = "Starting UIAutomator2 server" in segment and "am instrument" in segment
    require("instrumentation process cannot be initialized" in segment,
            "creation_failure_not_instrumentation_start")
    return {"terminalFailed": True, "exclusiveRequest": True, "masterNotAdded": True,
            "instrumentationLaunchObserved": launched,
            "suffixSHA256": hashlib.sha256(raw).hexdigest()}


class FailureWitness:
    def __init__(self, host, host_probe, port, token, root=None):
        self.host, self.host_probe, self.port, self.token = dict(host), host_probe, port, token
        self.root = Path(root or os.environ.get("JINGYE_APPIUM_LOG_ROOT", r"D:\openclaw_data\logs\appium"))
        require(self.root.is_absolute(), "creation_log_root_not_absolute")
        self._same_host()
        self.prefix_verified = False
        born = dt.datetime.fromisoformat(host["createdAtUtc"].replace("Z", "+00:00"))
        candidates = []
        for path in self.root.glob("*.stdout.log"):
            name = re.fullmatch(r"(\d{8}T\d{9}Z)-[0-9a-f]{32}\.stdout\.log", path.name)
            if not name:
                continue
            at = dt.datetime.strptime(name[1], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=dt.timezone.utc)
            if -10 <= (at-born).total_seconds() <= 2:
                candidates.append(path)
        require(len(candidates) == 1, "creation_host_log_not_unique")
        self.path = candidates[0]
        with self.path.open("rb") as stream:
            self.file_id = (os.fstat(stream.fileno()).st_dev, os.fstat(stream.fileno()).st_ino)
            self.header = stream.read(16384)
            require(b"Welcome to Appium" in self.header and b"listener started" in self.header,
                    "creation_host_log_header_missing")
            stream.seek(0, 2); self.offset = stream.tell()
            require(self.offset > 0, "creation_host_log_empty")
            stream.seek(self.offset-1)
            require(stream.read(1) == b"\n", "creation_host_log_partial_checkpoint")
        self._same_host()

    def _same_host(self):
        require(self.host_probe(self.port) == self.host, "creation_appium_host_changed")

    def verify(self):
        self._same_host()
        with self.path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            require((stat.st_dev, stat.st_ino) == self.file_id,
                    "creation_log_identity_changed")
            require(stream.read(len(self.header)) == self.header, "creation_log_header_changed")
            stream.seek(0, 2); size = stream.tell()
            require(self.offset <= size <= self.offset+8*1024*1024, "creation_log_size_invalid")
            if not self.prefix_verified:
                # Appium 3 session discovery is disabled on this host. Audit
                # its real master-list lifecycle instead of mistaking a 404
                # from the removed /sessions route for an empty session list.
                # The old prefix is read only on failure, once per witness.
                stream.seek(0); active=set(); pending=0; deleting=set()
                while stream.tell() < self.offset:
                    line=stream.readline(self.offset-stream.tell()).decode("utf-8", errors="strict")
                    if re.search(r"\[HTTP\]\s+--> POST /session(?:\s|$)",line):
                        pending+=1
                        require(pending==1,"creation_prefix_overlapping_request")
                    if re.search(r"\[HTTP\]\s+<-- POST /session \d{3}(?:\s|$)",line):
                        require(pending==1,"creation_prefix_unpaired_terminal");pending-=1
                    add=re.search(r"session ([0-9a-fA-F-]{36}) added to master session list",line,re.I)
                    remove=re.search(r"Removing session ([0-9a-fA-F-]{36}) from our master session list",line,re.I)
                    if add:
                        require(add[1] not in active,"creation_prefix_duplicate_master");active.add(add[1])
                    if remove:
                        require(remove[1] in active,"creation_prefix_unpaired_master");active.remove(remove[1])
                    start=re.search(r"\[HTTP\]\s+--> DELETE /session/([0-9a-fA-F-]{36})(?:\s|$)",line)
                    end=re.search(r"\[HTTP\]\s+<-- DELETE /session/([0-9a-fA-F-]{36}) \d{3}(?:\s|$)",line)
                    if start:
                        require(start[1] not in deleting,"creation_prefix_duplicate_delete");deleting.add(start[1])
                    if end:
                        require(end[1] in deleting,"creation_prefix_unpaired_delete");deleting.remove(end[1])
                require(not active and pending==0 and not deleting,"creation_prefix_master_not_idle")
                self.prefix_verified=True
            stream.seek(self.offset); raw = stream.read(size-self.offset)
        result = audit_suffix(raw, self.token)
        result["prefixMasterIdle"] = True
        self._same_host()
        return result
