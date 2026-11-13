"""Durable, fail-closed ownership of one local Appium host's session lifecycle.

Import performs no I/O. Only the wireless session factory calls begin(). A
successful master-list removal or /status response never clears this journal.
Records are cleared only after the caller's explicit quit returned normally,
and only for the same PID/creation-time host and reservation owner.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit
import uuid


class GuardError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise GuardError(code)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _server(server_url):
    try:
        parsed = urlsplit(server_url)
        _require(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
                 "appium_guard_local_http_required")
        port = parsed.port or 80
        _require(1 <= port <= 65535, "appium_guard_port_invalid")
        return "local:" + str(port), port
    except (TypeError, ValueError):
        raise GuardError("appium_guard_server_invalid") from None


def _host(value):
    _require(isinstance(value, dict) and type(value.get("pid")) is int and value["pid"] > 0
             and isinstance(value.get("createdAtUtc"), str)
             and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{1,7}Z", value["createdAtUtc"]),
             "appium_guard_host_identity_invalid")
    try:
        datetime.datetime.fromisoformat(value["createdAtUtc"].replace("Z", "+00:00"))
    except ValueError:
        raise GuardError("appium_guard_host_identity_invalid") from None
    return {"pid": value["pid"], "createdAtUtc": value["createdAtUtc"]}


def query_local_host(port):
    """Read listener ownership and CIM birth time; never start/stop a process."""
    _require(os.name == "nt", "appium_guard_windows_host_required")
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = r"""
$ErrorActionPreference='Stop'
try {
 $owners=@(Get-NetTCPConnection -State Listen -LocalPort PORT_VALUE -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique)
 if($owners.Count -ne 1){throw 'listener_not_unique'}
 $proc=Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId='+[int]$owners[0]) -ErrorAction Stop
 if(-not $proc -or $proc.Name -ne 'node.exe' -or -not $proc.CreationDate -or -not $proc.CommandLine -or $proc.CommandLine -notmatch '(?i)appium'){throw 'host_not_verified'}
 [ordered]@{pid=[int]$proc.ProcessId;createdAtUtc=$proc.CreationDate.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")} | ConvertTo-Json -Compress
} catch {exit 1}
""".replace("PORT_VALUE", str(int(port)))
    try:
        result = subprocess.run([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                stdin=subprocess.DEVNULL, timeout=20, check=False)
        _require(result.returncode == 0, "appium_guard_host_query_failed")
        return _host(json.loads(result.stdout.lstrip("\ufeff")))
    except (OSError, ValueError, subprocess.SubprocessError):
        raise GuardError("appium_guard_host_query_failed") from None


class Reservation:
    def __init__(self, guard, owner, host):
        self.guard, self.owner, self.host = guard, owner, host

    def created(self, session_id):
        self.guard.created(self, session_id)

    def uncertain(self, reason):
        self.guard.uncertain(self, reason)

    def closed(self):
        self.guard.closed(self)

    def failed_creation_closed(self, receipt):
        self.guard.failed_creation_closed(self, receipt)


class SessionGuard:
    def __init__(self, path, server_url, *, host_probe=None):
        self.path = Path(path)
        _require(self.path.is_absolute(), "appium_guard_path_must_be_absolute")
        self.server_key, self.port = _server(server_url)
        self.host_probe = host_probe or query_local_host

    def _current_host(self):
        try:
            return _host(self.host_probe(self.port))
        except GuardError:
            raise
        except Exception:
            raise GuardError("appium_guard_host_query_failed") from None

    @contextmanager
    def _lock(self):
        descriptor = None
        acquired = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            deadline = time.monotonic() + 5
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise GuardError("appium_guard_lock_unavailable") from None
                    time.sleep(0.05)
            yield
        except OSError:
            raise GuardError("appium_guard_storage_unavailable") from None
        finally:
            if descriptor is not None:
                try:
                    if acquired:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    raise GuardError("appium_guard_lock_release_failed") from None
                finally:
                    try:
                        os.close(descriptor)
                    except OSError:
                        raise GuardError("appium_guard_lock_release_failed") from None

    def _read(self):
        if not self.path.exists():
            return {"schema": 1, "server": self.server_key, "record": None, "history": []}
        try:
            raw = self.path.read_bytes()
            _require(len(raw) <= 2 * 1024 * 1024, "appium_guard_journal_invalid")
            value = json.loads(raw)
            _require(isinstance(value, dict) and type(value.get("schema")) is int
                     and value.get("schema") == 1 and value.get("server") == self.server_key
                     and isinstance(value.get("history"), list) and "record" in value,
                     "appium_guard_journal_invalid")
            record = value["record"]
            if record is not None:
                _require(isinstance(record, dict) and isinstance(record.get("owner"), str)
                         and re.fullmatch(r"[0-9a-f]{32}", record["owner"])
                         and record.get("state") in {"creating", "active", "creation_unknown", "cleanup_uncertain"}
                         and record.get("scope") in {"host", "unbound"}, "appium_guard_journal_invalid")
                _host(record.get("host"))
            return value
        except (OSError, ValueError, TypeError):
            raise GuardError("appium_guard_journal_unreadable_or_invalid") from None

    def _write(self, value):
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".part")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=True, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError, TypeError):
            raise GuardError("appium_guard_journal_write_failed") from None
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

    def _owned(self, value, reservation):
        record = value["record"]
        _require(record is not None and record.get("owner") == reservation.owner,
                 "appium_guard_reservation_not_owned")
        return record

    def begin(self):
        with self._lock():
            value = self._read()
            current = self._current_host()
            previous = value["record"]
            if previous is not None:
                _require(previous["scope"] != "unbound", "appium_guard_unbound_uncertainty")
                # A host can change while Remote is in flight. If the client
                # dies before created()/uncertain() is durable, the new host
                # might already own that unknown session. Birth changes alone
                # cannot discharge an unbound creation intent.
                _require(previous["state"] not in {"creating", "creation_unknown"},
                         "appium_guard_creation_outcome_unknown_across_hosts")
                _require(previous["host"] != current, "appium_guard_same_host_unresolved")
                value["history"].append({"event": "host_instance_changed", "atUtc": _now(),
                                         "previousRecord": previous, "newHost": current})
            owner = uuid.uuid4().hex
            value["record"] = {"owner": owner, "host": current, "scope": "host", "state": "creating",
                               "sessionId": None, "atUtc": _now(), "clientPid": os.getpid()}
            self._write(value)
            return Reservation(self, owner, current)

    def created(self, reservation, session_id):
        _require(isinstance(session_id, str) and re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", session_id),
            "appium_guard_session_id_invalid")
        with self._lock():
            value = self._read()
            record = self._owned(value, reservation)
            _require(self._current_host() == reservation.host == record["host"]
                     and record["state"] == "creating", "appium_guard_host_changed_during_creation")
            record.update(state="active", sessionId=session_id, updatedAtUtc=_now())
            self._write(value)

    def uncertain(self, reservation, reason):
        _require(reason in {"creation_unknown", "cleanup_uncertain"}, "appium_guard_reason_invalid")
        with self._lock():
            value = self._read()
            record = self._owned(value, reservation)
            try:
                current = self._current_host()
            except GuardError:
                record["scope"] = "unbound"
            else:
                if current != record["host"]:
                    value["history"].append({"event": "host_changed_during_uncertain_operation", "atUtc": _now(),
                                             "previousRecord": dict(record), "newHost": current})
                    record["host"] = current
            record.update(state=reason, updatedAtUtc=_now())
            self._write(value)

    def closed(self, reservation):
        with self._lock():
            value = self._read()
            record = self._owned(value, reservation)
            _require(record["state"] == "active" and record["scope"] == "host"
                     and self._current_host() == reservation.host == record["host"],
                     "appium_guard_close_not_proven_for_same_host")
            value["lastClosed"] = {**record, "state": "closed", "closedAtUtc": _now()}
            value["record"] = None
            self._write(value)

    def failed_creation_closed(self, reservation, receipt):
        _require(isinstance(receipt, dict) and all(receipt.get(k) is True for k in
                 ("terminalFailed", "exclusiveRequest", "masterNotAdded", "physicalTerminalVerified"))
                 and all(type(receipt.get(k)) is int and receipt[k] == 0 for k in
                         ("processCount", "instrumentationCount", "ownedForwardCount")),
                 "appium_guard_failed_creation_proof_missing")
        with self._lock():
            value = self._read()
            record = self._owned(value, reservation)
            _require(record["state"] in {"creating", "creation_unknown"} and record["sessionId"] is None
                     and record["scope"] == "host"
                     and self._current_host() == reservation.host == record["host"],
                     "appium_guard_failed_creation_host_or_state_changed")
            value["lastClosed"] = {**record, "state": "failed_creation_closed", "closedAtUtc": _now(),
                                   "cleanupReceipt": receipt}
            value["record"] = None
            self._write(value)


def begin(server_url):
    """Use the wireless JSON's directory, or its explicit appium_guard_path."""
    _require(os.environ.get("JINGYE_WIRELESS_ENABLED") == "1", "appium_guard_wireless_not_enabled")
    config_path = os.environ.get("JINGYE_WIRELESS_CONFIG")
    _require(bool(config_path), "appium_guard_config_path_missing")
    try:
        path = Path(config_path)
        _require(path.is_absolute(), "appium_guard_config_path_must_be_absolute")
        raw = path.read_bytes()
        _require(len(raw) <= 1024 * 1024, "appium_guard_config_invalid")
        config = json.loads(raw)
        _require(isinstance(config, dict), "appium_guard_config_invalid")
        _, port = _server(server_url)
        explicit = config.get("appium_guard_path")
        _require(explicit is None or isinstance(explicit, str) and explicit, "appium_guard_config_invalid")
        guard_path = Path(explicit) if explicit else path.parent / f"appium-session-guard-{port}.json"
        return SessionGuard(guard_path, server_url).begin()
    except (OSError, ValueError, TypeError):
        raise GuardError("appium_guard_config_unreadable_or_invalid") from None
