"""Wireless-only ownership of this client's UiA2 processes. Import performs no I/O.

A successful WebDriver quit is necessary, but cannot prove phone cleanup when
Appium's ADB object still targets a vanished TLS port. Only a captured process
identity from a previously idle phone may be stopped, after a normal quit and
an independently verified endpoint change. No package-wide stop is used.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
import shlex
import socket
import subprocess
import uuid
from urllib.parse import urlsplit
import jingye_appium_creation as creation


PACKAGES = frozenset({"io.appium.uiautomator2.server", "io.appium.uiautomator2.server.test"})
OWNERSHIP_CAPABILITY = "jingye:ownershipToken"


class OwnershipError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class CreationTransportChanged(OwnershipError):
    """Owned creation succeeded, but its Appium transport must be closed first."""
    def __init__(self):
        super().__init__("phone_creation_transport_changed_owned")


class CreationReadTransportError(OwnershipError):
    """Only read-only transport loss may repeat proof under the same deadline."""
    def __init__(self):
        super().__init__("phone_creation_read_transport_unavailable")


def _require(value, code):
    if not value:
        raise OwnershipError(code)


def parse_forwards(text):
    _require(isinstance(text, str), "phone_forward_report_invalid")
    rows = []
    service = r"(?:tcp|localabstract|localreserved|localfilesystem|dev|dev-raw|jdwp|vsock|acceptfd):\S+"
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        _require(len(fields) == 3 and re.fullmatch(service, fields[1])
                 and re.fullmatch(service, fields[2]), "phone_forward_report_invalid")
        rows.append(tuple(fields))
    return rows


def parse_instrumentation(text):
    """Only a complete Android15 AMS processes report can establish zero."""
    _require(isinstance(text, str) and text.endswith("\n"), "phone_instrumentation_report_incomplete")
    lines = text.splitlines()
    header = "ACTIVITY MANAGER RUNNING PROCESSES (dumpsys activity processes)"
    _require(bool(lines) and lines[0] == header and lines.count(header) == 1,
             "phone_instrumentation_report_unknown")
    _require(not re.search(r"(?im)^\s*(?:Unknown command:|Bad activity command|Permission Denial|Can't find service:|Exception occurred while dumping)", text),
             "phone_instrumentation_report_error")
    patterns = [r"\s*Total persistent processes: \d+\s*",
                r"\s*mProcessesReady=(?:true|false) mSystemReady=(?:true|false) mBooted=(?:true|false) mFactoryTest=\d+\s*",
                r"\s*mForceBackgroundCheck=(?:true|false)\s*"]
    markers = [[i for i, line in enumerate(lines) if re.fullmatch(pattern, line)] for pattern in patterns]
    _require(all(len(items) == 1 for items in markers), "phone_instrumentation_report_incomplete")
    _require(markers[0][0] < markers[1][0] < markers[2][0]
             == max(i for i, line in enumerate(lines) if line.strip()), "phone_instrumentation_report_incomplete")
    headers = [i for i, line in enumerate(lines) if re.fullmatch(r"\s*Active instrumentation:\s*", line)]
    records = [(i, re.fullmatch(r"\s*Instrumentation #(\d+):\s+(ActiveInstrumentation\{[^\r\n]*\})\s*", line))
               for i, line in enumerate(lines) if re.match(r"\s*Instrumentation #", line)]
    _require(len(headers) <= 1 and all(match for _, match in records), "phone_instrumentation_section_invalid")
    if not headers:
        _require(not records and "ActiveInstrumentation{" not in text, "phone_instrumentation_unattributed_reference")
    else:
        _require(bool(records) and all(headers[0] < i < markers[0][0] for i, _ in records)
                 and [int(match[1]) for _, match in records] == list(range(len(records))),
                 "phone_instrumentation_section_invalid")
    # Android15 ActiveInstrumentation.toString includes dynamic FINISHED/N procs.
    # Instance hash + the complete ComponentName form the identity; neither the
    # package alone nor those mutable state fields can establish ownership.
    result = []
    for _, match in records:
        identity = re.fullmatch(r"ActiveInstrumentation\{([0-9a-fA-F]+) \{([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)\}( FINISHED)? (\d+) procs\}", match[2])
        _require(identity is not None, "phone_instrumentation_identity_unknown")
        result.append({"instance": identity[1].lower(), "component": identity[2],
                       "finished": bool(identity[3]), "processCount": int(identity[4])})
    return tuple(result)


def instrumentation_identity(records):
    return tuple((row["instance"], row["component"]) for row in records)


def session_token_identity(value, expected_token):
    """Only the real UiA2 singleton session's exact vendor field is evidence."""
    _require(isinstance(value, dict) and "error" not in value and isinstance(value.get("value"), list)
             and len(value["value"]) == 1, "phone_token_session_report_invalid")
    item = value["value"][0]
    _require(isinstance(item, dict) and "error" not in item, "phone_token_session_report_invalid")
    sid = item.get("id")
    _require(isinstance(sid, str) and re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", sid)
             and item.get("sessionId") == sid,
             "phone_token_session_identity_invalid")
    caps = item.get("capabilities")
    _require(isinstance(caps, dict) and type(caps.get(OWNERSHIP_CAPABILITY)) is str
             and caps[OWNERSHIP_CAPABILITY] == expected_token, "phone_creation_token_unverified")
    return sid


def parse_net_listeners(text, family):
    _require(isinstance(text, str) and text.endswith("\n"), "phone_socket_report_incomplete")
    lines = text.splitlines(); head = lines[0].split() if lines else []
    _require(len(head) == 12 and head[:2] == ["sl", "local_address"]
             and head[2] in {"rem_address", "remote_address"} and head[3:] == [
                 "st", "tx_queue", "rx_queue", "tr", "tm->when", "retrnsmt", "uid", "timeout", "inode"],
             "phone_socket_report_unknown")
    width = 8 if family == "tcp" else 32
    address = r"[0-9a-fA-F]{" + str(width) + r"}:[0-9a-fA-F]{4}"
    matches = []
    for line in lines[1:]:
        if not line.strip(): continue
        fields = line.split()
        _require(len(fields) >= 10 and re.fullmatch(r"[0-9]+:", fields[0])
                 and re.fullmatch(address, fields[1]) and re.fullmatch(address, fields[2])
                 and re.fullmatch(r"[0-9a-fA-F]{2}", fields[3])
                 and re.fullmatch(r"[0-9]+", fields[7]) and re.fullmatch(r"[0-9]+", fields[9]),
                 "phone_socket_row_invalid")
        if int(fields[1].rsplit(":", 1)[1], 16) == 6790 and fields[3].upper() == "0A":
            matches.append({"family": family, "uid": int(fields[7]), "inode": int(fields[9])})
    return matches


def parse_fd_sockets(text):
    _require(isinstance(text, str) and text.endswith("\n"), "phone_fd_report_incomplete")
    sockets = set(); count = 0
    for line in text.splitlines():
        if not line.strip() or re.fullmatch(r"total [0-9]+", line.strip()): continue
        match = re.fullmatch(r"l[rwxstST-]{9}\s+[0-9]+\s+\S+\s+\S+\s+[0-9]+\s+.+?\s+([0-9]+)\s+->\s+(.+)", line.strip())
        _require(match is not None, "phone_fd_row_invalid")
        count += 1
        inode = re.fullmatch(r"socket:\[([0-9]+)\]", match[2])
        if inode: sockets.add(int(inode[1]))
    _require(count > 0, "phone_fd_inventory_empty")
    return sockets


def read_sessions_json(port, deadline, clock):
    """Bounded local HTTP only; no proxy, redirect, session creation or retry."""
    _require(type(port) is int and 1 <= port <= 65535, "phone_proof_port_invalid")
    def remaining():
        left = deadline - clock()
        _require(left > 0, "phone_owner_budget_exhausted")
        return min(left, 10)
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "phone_proof_json_duplicate_key")
            result[key] = value
        return result
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=remaining()) as stream:
            stream.settimeout(remaining())
            stream.sendall(b"GET /sessions HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: application/json\r\nConnection: close\r\n\r\n")
            raw = b""; body_at = None; size = None
            while True:
                stream.settimeout(remaining())
                chunk = stream.recv(65536)
                if not chunk: raise CreationReadTransportError()
                raw += chunk
                if body_at is None:
                    split = raw.find(b"\r\n\r\n")
                    _require(split <= 16384 and (split >= 0 or len(raw) <= 16384), "phone_proof_http_header_invalid")
                    if split < 0: continue
                    header = raw[:split].decode("ascii").split("\r\n")
                    _require(re.fullmatch(r"HTTP/1\.[01] 200(?: .*)?", header[0]), "phone_proof_http_status_unverified")
                    headers = {}
                    for line in header[1:]:
                        key, separator, val = line.partition(":")
                        key = key.lower()
                        _require(separator and re.fullmatch(r"[a-z0-9-]+", key) and key not in headers, "phone_proof_http_header_invalid")
                        headers[key] = val.strip()
                    _require("transfer-encoding" not in headers and re.fullmatch(r"[0-9]+", headers.get("content-length", "")),
                             "phone_proof_http_length_unverified")
                    size = int(headers["content-length"])
                    _require(0 < size <= 1024 * 1024, "phone_proof_http_body_too_large")
                    body_at = split + 4
                _require(len(raw) <= body_at + size, "phone_proof_http_trailing_bytes")
                if len(raw) == body_at + size:
                    return json.loads(raw[body_at:].decode("utf-8"), object_pairs_hook=unique_object)
    except OwnershipError: raise
    except OSError:
        raise CreationReadTransportError() from None
    except (ValueError, UnicodeError):
        raise OwnershipError("phone_proof_http_unavailable") from None


def parse_processes(text):
    _require(isinstance(text, str), "phone_process_report_invalid")
    lines = [line.strip().split(maxsplit=2) for line in text.splitlines() if line.strip()]
    _require(bool(lines) and lines[0] == ["PID", "UID", "NAME"], "phone_process_report_unknown")
    result = []
    for fields in lines[1:]:
        _require(len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit(), "phone_process_report_invalid")
        if fields[2].startswith("io.appium.uiautomator2.server"):
            _require(fields[2] in PACKAGES, "phone_process_name_unknown")
            result.append({"pid": int(fields[0]), "uid": int(fields[1]), "name": fields[2]})
    _require(len(result) <= 2 and len({row["name"] for row in result}) == len(result),
             "phone_process_ownership_ambiguous")
    return result


def start_ticks(text, pid):
    _require(isinstance(text, str) and text.startswith(str(pid) + " ("), "phone_process_stat_invalid")
    try:
        fields = text.rsplit(") ", 1)[1].split()
        value = fields[19]  # field22: the first token after comm is field3.
    except (IndexError, TypeError):
        raise OwnershipError("phone_process_stat_invalid") from None
    _require(value.isdigit(), "phone_process_stat_invalid")
    return int(value)


def stop_script(identity):
    """One run-as shell transaction rechecks identity immediately before TERM.

    It never accepts arbitrary commands or negative/group PIDs. No network call
    separates the final /proc checks and kill. This is not a kernel pidfd: the
    narrow same-UID PID-reuse race between read and signal remains. A lost
    response is never replayed.
    """
    _require(isinstance(identity, dict) and type(identity.get("pid")) is int and identity["pid"] > 1
             and type(identity.get("uid")) is int and identity["uid"] > 0
             and type(identity.get("startTicks")) is int and identity["startTicks"] >= 0
             and identity.get("name") == "io.appium.uiautomator2.server", "phone_stop_identity_invalid")
    pid, uid, ticks, name = (identity[key] for key in ("pid", "uid", "startTicks", "name"))
    return (f'[ "$(id -u)" = {uid} ] || exit 40; '
            f'[ -e /proc/{pid} ] || {{ echo OWNED_ALREADY_GONE; exit 0; }}; '
            f'stat=$(cat /proc/{pid}/stat) || exit 41; '
            f'case "$stat" in "{pid} ("*") "*) ;; *) exit 42;; esac; '
            'rest=${stat##*) }; set -- $rest; [ "$#" -ge 20 ] || exit 43; shift 19; '
            f'[ "$1" = {ticks} ] || exit 44; '
            f'cmd=$(tr "\\000" "\\n" < /proc/{pid}/cmdline) || exit 45; '
            f'[ "$cmd" = {shlex.quote(name)} ] || exit 46; '
            f'kill -TERM {pid} || exit 47; echo OWNED_SIGNAL_SENT')


class PhoneOwnership:
    def __init__(self, link, server_url, host, host_probe):
        parsed = urlsplit(server_url)
        _require(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"},
                 "phone_owner_local_appium_required")
        self.client = link._default_client()
        self.port = parsed.port or 80
        self.host, self.host_probe = dict(host), host_probe
        self.initial = None
        self.owned = None
        self._creation_transport = None
        self.creation_token = "jingye-" + uuid.uuid4().hex
        self.capture_transport = None
        self.capture_diagnostic = None
        self.proof_forward = None
        self.proof_forwards = []
        self.capture_read_retries = 0
        self.stopped = set()
        self.session_id = None
        self.cleanup_receipt = {}
        self.failure_witness = None
        self.failure_witness_error = None

    def begin_remote(self):
        """Optional evidence, collected after idle prepare and before Remote.

        A missing/unreadable log does not prevent an ordinary healthy creation;
        it prevents automatic release/retry of a failed creation.
        """
        _require(self.initial is not None and not self.initial["processes"]
                 and not self.initial["instruments"], "phone_failure_baseline_missing")
        try:
            self.failure_witness = creation.FailureWitness(
                self.host, self.host_probe, self.port, self.creation_token)
        except Exception:
            self.failure_witness_error = "creation_log_checkpoint_unavailable"

    def _host_same(self):
        _require(self.host_probe(self.port) == self.host, "phone_owner_appium_host_changed")

    @contextmanager
    def _operation(self, budget=60):
        self._host_same()
        with self.client._operation() as outer:
            deadline = self.client.clock() + budget
            if outer is not None:
                deadline = min(outer, deadline)
            yield deadline
        self._host_same()

    def _command(self, serial, args, deadline, timeout=15, *, global_command=False):
        _require(deadline > self.client.clock(), "phone_owner_budget_exhausted")
        safe_read = (args == ["forward", "--list"] or args[:2] in (
            ["shell", "ps"], ["shell", "cat"], ["shell", "dumpsys"])
            or (args[:2] == ["shell", "run-as"] and len(args) > 3 and args[3] in {"id", "cat", "ls"}))
        try:
            command = args if global_command else ["-s", serial, *args]
            result = self.client._exec(command, min(timeout, deadline - self.client.clock()), deadline=deadline)
        except (OSError, TimeoutError, subprocess.TimeoutExpired):
            if safe_read: raise CreationReadTransportError() from None
            raise  # A lost forward mutation or signal response is never replayed.
        if safe_read and result.returncode != 0:
            message = str(getattr(result, "stderr", "") or "") + "\n" + str(result.stdout or "")
            if re.search(r"(?im)^(?:adb:\s*)?(?:error:\s*)?(?:device(?: [^\r\n]+)? (?:not found|offline)|no devices/emulators found|closed)\s*$", message):
                raise CreationReadTransportError()
        _require(result.returncode == 0 and isinstance(result.stdout, str), "phone_owner_read_failed")
        return result.stdout

    def _snapshot(self, deadline):
        self._host_same()
        try:
            serial = self.client._ensure(deadline)  # The real helper verifies same-phone identity.
        except (OSError, TimeoutError, subprocess.TimeoutExpired):
            raise CreationReadTransportError() from None
        rows = parse_processes(self._command(serial, ["shell", "ps", "-A", "-o", "PID,UID,NAME"], deadline))
        for row in rows:
            package, pid = row["name"], str(row["pid"])
            uid = self._command(serial, ["shell", "run-as", package, "id", "-u"], deadline).strip()
            _require(uid.isdigit() and int(uid) == row["uid"], "phone_process_uid_unverified")
            cmdline = self._command(serial, ["shell", "run-as", package, "cat", f"/proc/{pid}/cmdline"], deadline)
            _require(cmdline.rstrip("\x00") == package, "phone_process_cmdline_unverified")
            row["startTicks"] = start_ticks(self._command(serial,
                ["shell", "run-as", package, "cat", f"/proc/{pid}/stat"], deadline), int(pid))
        instruments = parse_instrumentation(self._command(serial, ["shell", "dumpsys", "activity", "processes"], deadline, 20))
        forwards = parse_forwards(self._command(serial, ["forward", "--list"], deadline))
        self._host_same()
        return {"serial": serial, "processes": rows, "instruments": instruments, "forwards": forwards}

    def prepare(self):
        _require(self.initial is None and self.creation_transport is None, "phone_prepare_state_invalid")
        with self._operation(budget=300) as deadline:
            while True:
                try:
                    snapshot = self._snapshot(deadline)
                    _require(not snapshot["processes"] and not snapshot["instruments"], "phone_not_idle_before_remote")
                    self.initial = snapshot
                    self._creation_transport = snapshot["serial"]
                    break
                except CreationReadTransportError:
                    _require(self.client.clock() < deadline, "phone_owner_budget_exhausted")
                    self.client.sleep(min(0.5, deadline - self.client.clock()))
        return self

    @property
    def creation_transport(self):
        return self._creation_transport

    def _socket_owner(self, serial, process, deadline):
        listeners = []
        for family in ("tcp", "tcp6"):
            listeners.extend(parse_net_listeners(self._command(serial, ["shell", "cat", "/proc/net/" + family], deadline), family))
        _require(len(listeners) == 1 and listeners[0]["uid"] == process["uid"] and listeners[0]["inode"] > 0,
                 "phone_socket_ownership_unverified")
        sockets = parse_fd_sockets(self._command(serial, ["shell", "run-as", process["name"], "ls", "-l",
                                                       f'/proc/{process["pid"]}/fd'], deadline))
        _require(listeners[0]["inode"] in sockets, "phone_socket_pid_unverified")
        return listeners[0]

    def _prove_changed_creation(self, snapshot, deadline, *, failed_empty=False):
        serial = snapshot["serial"]; target = snapshot["processes"][0]
        # Reserve the final ten seconds of the same operation budget for removal
        # of the one known-created proof forward. No command resets that budget.
        proof_deadline = deadline - 10
        _require(proof_deadline > self.client.clock(), "phone_creation_proof_budget_exhausted")
        before_socket = self._socket_owner(serial, target, proof_deadline)
        self.proof_forward = {"creationAttempted": True, "created": False, "removed": False}
        self.proof_forwards.append(self.proof_forward)
        # tcp:0 lets the existing ADB server reserve a fresh local port without
        # rebinding any existing mapping. Lost allocation response stays unknown.
        port_text = self._command(serial, ["forward", "--no-rebind", "tcp:0", "tcp:6790"], proof_deadline).strip()
        _require(re.fullmatch(r"[0-9]+", port_text) and 1 <= int(port_text) <= 65535, "phone_proof_forward_port_unknown")
        port = int(port_text); entry = (serial, "tcp:" + str(port), "tcp:6790")
        _require(entry not in snapshot["forwards"], "phone_proof_forward_not_new")
        self.proof_forward.update(created=True, entry=entry)
        try:
            rows = parse_forwards(self._command(serial, ["forward", "--list"], proof_deadline))
            _require(rows.count(entry) == 1 and sum(row[1] == entry[1] for row in rows) == 1,
                     "phone_proof_forward_scope_changed")
            response = read_sessions_json(port, proof_deadline, self.client.clock)
            if failed_empty:
                _require(self.failure_witness is not None, "phone_failure_witness_missing")
                self.failure_witness.verify()
                _require(creation.empty_sessions(response), "phone_failed_creation_downstream_not_empty")
                session = None
            else:
                session = session_token_identity(response, self.creation_token)
            _require(self._socket_owner(serial, target, proof_deadline) == before_socket, "phone_proof_socket_changed")
            after = self._snapshot(proof_deadline)
            _require(after["serial"] == serial and after["processes"] == snapshot["processes"]
                     and after["instruments"] == snapshot["instruments"], "phone_creation_snapshot_changed")
            return {"method": "exclusive_failed_request_empty_server_pid_socket" if failed_empty
                            else "server_session_token_and_pid_socket", "downstreamSessionId": session,
                    "tokenVerified": not failed_empty, "socket": before_socket, "stableSnapshotVerified": True}
        finally:
            # Remove only the precise mapping whose successful creation is known.
            # Unknown allocation or changed scope never permits a blanket remove.
            rows = parse_forwards(self._command(serial, ["forward", "--list"], deadline, global_command=True))
            local_rows = [row for row in rows if row[1] == entry[1]]
            _require(not local_rows or local_rows == [entry], "phone_proof_forward_cleanup_scope_changed")
            if local_rows:
                self._command(serial, ["forward", "--remove", entry[1]], deadline)
            else:
                self.proof_forward["alreadyAbsent"] = True
            rows = parse_forwards(self._command(serial, ["forward", "--list"], deadline, global_command=True))
            _require(not any(row[1] == entry[1] for row in rows), "phone_proof_forward_cleanup_unverified")
            self.proof_forward["removed"] = True

    def finish_failed_creation(self):
        """Only a completed, exclusive failed request can use this path.

        This deliberately does not call capture with a fabricated session ID.
        No result is inferred from an exception string or a /status response.
        A checkpointed idle phone, exact request token in the same host's log,
        failed terminal and absence of any competing mutations establish the
        launch scope. The verified empty downstream server must also belong to
        the exact stable process/socket. Normal active sessions still require
        capture token + explicit quit via finish_after_quit.
        """
        _require(self.initial is not None and self.owned is None and self.session_id is None
                 and self.failure_witness is not None, "phone_failure_witness_missing")
        receipt = self.failure_witness.verify()
        with self._operation(budget=300) as deadline:
            while True:
                try:
                    snapshot = self._snapshot(deadline)
                    break
                except CreationReadTransportError:
                    _require(self.client.clock() < deadline, "phone_owner_budget_exhausted")
                    self.client.sleep(min(0.5, deadline-self.client.clock()))
            self.failure_witness.verify()
            new_forwards = [r for r in snapshot["forwards"] if r not in self.initial["forwards"]]
            _require(all(r[0] == self.creation_transport and r[2] == "tcp:6790" for r in new_forwards),
                     "phone_failed_creation_forward_scope_unknown")
            if snapshot["processes"] or snapshot["instruments"]:
                _require(receipt["instrumentationLaunchObserved"]
                         and len(snapshot["processes"]) == len(snapshot["instruments"]) == 1
                         and snapshot["processes"][0]["name"] == "io.appium.uiautomator2.server"
                         and snapshot["instruments"][0]["component"].split("/", 1)[0]
                             == "io.appium.uiautomator2.server.test"
                         and snapshot["instruments"][0]["finished"] is False
                         and snapshot["instruments"][0]["processCount"] == 1,
                         "phone_failed_creation_residual_unverified")
                self._prove_changed_creation(snapshot, deadline, failed_empty=True)
                _require(self._snapshot(deadline) == snapshot, "phone_failed_creation_snapshot_changed")
                self.failure_witness.verify()
                self.owned = {**snapshot, "forwards": new_forwards}
                for row in snapshot["processes"]:
                    key = (row["pid"], row["uid"], row["startTicks"], row["name"])
                    _require(key not in self.stopped, "phone_owned_stop_already_attempted")
                    self.stopped.add(key)  # Never replay a signal after a lost response.
                    self._host_same()
                    serial = self.client._ensure(deadline)
                    _require(serial == snapshot["serial"], "phone_endpoint_changed_before_stop")
                    output = self._command(serial, ["shell", "run-as", row["name"], "sh", "-c",
                        shlex.quote(stop_script(row))], deadline)
                    _require(output.strip() in {"OWNED_SIGNAL_SENT", "OWNED_ALREADY_GONE"},
                             "phone_owned_stop_unconfirmed")
            end = min(deadline, self.client.clock()+12)
            while True:
                current = self._snapshot(deadline)
                if self.owned is not None:
                    self._known_residual(current)
                else:
                    _require(not current["processes"] and not current["instruments"],
                             "phone_failed_creation_new_process")
                remaining = [r for r in current["forwards"] if r not in self.initial["forwards"]]
                _require(all(r in new_forwards for r in remaining), "phone_failed_creation_new_forward")
                if not current["processes"] and not current["instruments"]:
                    for row in remaining:
                        _require(row[0] == current["serial"], "phone_failed_creation_stale_forward")
                        self.failure_witness.verify()
                        self._command(current["serial"], ["forward", "--remove", row[1]], deadline)
                    if not remaining:
                        self.failure_witness.verify()
                        self.cleanup_receipt = {**receipt, "physicalTerminalVerified": True,
                            "processCount": 0, "instrumentationCount": 0, "ownedForwardCount": 0,
                            "signalsAttempted": len(self.stopped)}
                        return self.cleanup_receipt
                _require(self.client.clock() < end, "phone_failed_creation_cleanup_unconfirmed")
                self.client.sleep(min(0.5, end-self.client.clock()))

    def capture(self, session_id):
        _require(self.initial is not None and self.owned is None and isinstance(session_id, str)
                 and re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", session_id),
                 "phone_capture_state_invalid")
        self.session_id = session_id
        with self._operation(budget=300) as deadline:
            while True:
                try:
                    snapshot = self._snapshot(deadline)
                    self.capture_transport = snapshot["serial"]
                    self.capture_diagnostic = {"authoritative": False, "snapshot": snapshot}
                    _require(len(snapshot["processes"]) == 1
                             and snapshot["processes"][0]["name"] == "io.appium.uiautomator2.server"
                             and len(snapshot["instruments"]) == 1
                             and snapshot["instruments"][0]["component"].split("/", 1)[0]
                                 == "io.appium.uiautomator2.server.test"
                             and snapshot["instruments"][0]["finished"] is False
                             and snapshot["instruments"][0]["processCount"] == 1,
                             "phone_created_instrumentation_unverified")
                    new_forwards = [row for row in snapshot["forwards"] if row not in self.initial["forwards"]]
                    _require(all(row[0] == self.creation_transport for row in new_forwards), "phone_created_forwards_unverified")
                    changed = snapshot["serial"] != self.creation_transport
                    if changed:
                        proof = self._prove_changed_creation(snapshot, deadline)
                        end = self._snapshot(deadline)
                        _require(end == snapshot, "phone_creation_snapshot_changed_after_proof")
                    else:
                        _require(bool(new_forwards), "phone_created_forwards_unverified")
                        proof = {"method": "same_transport_created_forward"}
                    owned = {**snapshot, "forwards": new_forwards, "creationTransport": self.creation_transport,
                             "captureTransport": self.capture_transport, "creationProof": proof}
                    break
                except CreationReadTransportError:
                    # Never repeat a mutation with an unknown result, or leave a
                    # known-created proof mapping behind before the next proof.
                    _require(all(not row["creationAttempted"] or (row["created"] and row["removed"])
                                 for row in self.proof_forwards), "phone_creation_forward_cleanup_unknown")
                    _require(self.client.clock() < deadline, "phone_owner_budget_exhausted")
                    self.capture_read_retries += 1
                    self.client.sleep(min(0.5, deadline - self.client.clock()))
        self.owned = owned
        self.capture_diagnostic["authoritative"] = True
        if self.capture_transport != self.creation_transport:
            # The caller must normal-quit this exact created session before any
            # settings/window command is sent through Appium's obsolete endpoint.
            raise CreationTransportChanged()

    def _known_residual(self, snapshot):
        if self.owned is None:
            _require(not snapshot["processes"] and not snapshot["instruments"], "phone_cleanup_capture_missing")
            return
        _require(all(row in self.owned["processes"] for row in snapshot["processes"]),
                 "phone_cleanup_process_identity_changed")
        _require(not snapshot["instruments"] or instrumentation_identity(snapshot["instruments"])
                 == instrumentation_identity(self.owned["instruments"]),
                 "phone_cleanup_instrumentation_identity_changed")

    def finish_after_quit(self, session_id):
        """Caller must invoke only after its one explicit quit returned normally."""
        _require(self.initial is not None and self.session_id == session_id,
                 "phone_cleanup_baseline_or_session_missing")
        with self._operation(budget=300) as deadline:
            snapshot = self._snapshot(deadline)
            self._known_residual(snapshot)
            self.cleanup_receipt = {"endpointChanged": snapshot["serial"] != self.initial["serial"],
                                    "signalsAttempted": len(self.stopped), "stopOutcomes": [],
                                    "physicalTerminalVerified": False}
            if self.owned is not None and snapshot["serial"] != self.creation_transport:
                for row in snapshot["processes"]:
                    key = (row["pid"], row["uid"], row["startTicks"], row["name"])
                    _require(key not in self.stopped, "phone_owned_stop_already_attempted")
                    self.stopped.add(key)  # A timeout must not replay the signal.
                    self.cleanup_receipt["signalsAttempted"] = len(self.stopped)
                    self._host_same()
                    verified = self.client._ensure(deadline)
                    _require(verified == snapshot["serial"], "phone_endpoint_changed_before_stop")
                    output = self._command(verified, ["shell", "run-as", row["name"], "sh", "-c",
                        shlex.quote(stop_script(row))], deadline)
                    _require(output.strip() in {"OWNED_SIGNAL_SENT", "OWNED_ALREADY_GONE"},
                             "phone_owned_stop_unconfirmed")
                    self.cleanup_receipt["stopOutcomes"].append(output.strip())
            # Same-port cleanup is observed, never replaced with a force-stop.
            end = min(deadline, self.client.clock() + 12)
            while True:
                snapshot = self._snapshot(deadline)
                self._known_residual(snapshot)
                owned_forwards = ([row for row in snapshot["forwards"] if row not in self.initial["forwards"]]
                                  if self.owned is None else self.owned["forwards"])
                if not snapshot["processes"] and not snapshot["instruments"] and not (
                        set(snapshot["forwards"]) & set(owned_forwards)):
                    self.cleanup_receipt.update(physicalTerminalVerified=True, processCount=0,
                                                instrumentationCount=0, ownedForwardCount=0)
                    return
                _require(self.client.clock() < end, "phone_cleanup_terminal_unverified")
                self.client.sleep(min(0.5, end - self.client.clock()))


def prepare(link, server_url, host, *, host_probe):
    return PhoneOwnership(link, server_url, host, host_probe).prepare()
