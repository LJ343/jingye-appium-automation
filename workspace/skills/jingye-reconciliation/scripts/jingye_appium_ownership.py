"""Wireless-only ownership of this client's UiA2 processes. Import performs no I/O.

A successful WebDriver quit is necessary, but cannot prove phone cleanup when
Appium's ADB object still targets a vanished TLS port. Only a captured process
identity from a previously idle phone may be stopped, after a normal quit and
an independently verified endpoint change. No package-wide stop is used.
"""
from __future__ import annotations

from contextlib import contextmanager
import re
import shlex
from urllib.parse import urlsplit


PACKAGES = frozenset({"io.appium.uiautomator2.server", "io.appium.uiautomator2.server.test"})


class OwnershipError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


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
        self.stopped = set()
        self.session_id = None
        self.cleanup_receipt = {}

    def _host_same(self):
        _require(self.host_probe(self.port) == self.host, "phone_owner_appium_host_changed")

    @contextmanager
    def _operation(self):
        self._host_same()
        with self.client._operation() as outer:
            deadline = self.client.clock() + 60
            if outer is not None:
                deadline = min(outer, deadline)
            yield deadline
        self._host_same()

    def _command(self, serial, args, deadline, timeout=15):
        _require(deadline > self.client.clock(), "phone_owner_budget_exhausted")
        result = self.client._exec(["-s", serial, *args], min(timeout, deadline - self.client.clock()), deadline=deadline)
        _require(result.returncode == 0 and isinstance(result.stdout, str), "phone_owner_read_failed")
        return result.stdout

    def _snapshot(self, deadline):
        self._host_same()
        serial = self.client._ensure(deadline)  # The real helper verifies same-phone identity.
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
        with self._operation() as deadline:
            snapshot = self._snapshot(deadline)
            _require(not snapshot["processes"] and not snapshot["instruments"], "phone_not_idle_before_remote")
            self.initial = snapshot
        return self

    def capture(self, session_id):
        _require(self.initial is not None and self.owned is None and isinstance(session_id, str)
                 and re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", session_id),
                 "phone_capture_state_invalid")
        self.session_id = session_id
        with self._operation() as deadline:
            snapshot = self._snapshot(deadline)
            _require(snapshot["serial"] == self.initial["serial"], "phone_endpoint_changed_during_remote")
            _require(len(snapshot["processes"]) == 1
                     and snapshot["processes"][0]["name"] == "io.appium.uiautomator2.server"
                     and len(snapshot["instruments"]) == 1
                     and snapshot["instruments"][0]["component"].split("/", 1)[0]
                         == "io.appium.uiautomator2.server.test"
                     and snapshot["instruments"][0]["finished"] is False
                     and snapshot["instruments"][0]["processCount"] == 1,
                     "phone_created_instrumentation_unverified")
            new_forwards = [row for row in snapshot["forwards"] if row not in self.initial["forwards"]]
            _require(bool(new_forwards) and all(row[0] == snapshot["serial"] for row in new_forwards),
                     "phone_created_forwards_unverified")
            self.owned = {**snapshot, "forwards": new_forwards}

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
        with self._operation() as deadline:
            snapshot = self._snapshot(deadline)
            self._known_residual(snapshot)
            self.cleanup_receipt = {"endpointChanged": snapshot["serial"] != self.initial["serial"],
                                    "signalsAttempted": len(self.stopped), "stopOutcomes": [],
                                    "physicalTerminalVerified": False}
            if self.owned is not None and snapshot["serial"] != self.owned["serial"]:
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
