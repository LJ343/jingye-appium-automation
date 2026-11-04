"""Bounded endpoint discovery for an explicitly allowlisted wireless ADB phone.

This helper never invokes adb, pairs, changes device settings, sends application
data, or scans a subnet.  TCP probes only connect and immediately close.  The
caller must authenticate each candidate using its existing ADB server and check
the expected device identity before returning True from verify_candidate.

The verifier receives (endpoint, remaining_seconds) and MUST bound all its work
by that budget.  Python cannot safely preempt an arbitrary callback.  Keep one
EndpointDiscovery instance for the lifetime of the recovery loop so its scan
cursor and completed-sweep cooldown remain effective. Partial scans resume in
memory; a new instance/process starts a new sweep. Call discover only after the normal online/cache
checks fail; successful normal operation must not trigger background scanning.
"""

from __future__ import annotations

import errno
import ipaddress
import math
import selectors
import socket
import threading
import time
from collections.abc import Callable, Iterable
from collections import deque


_CONFIG_KEYS = frozenset({
    "enabled", "allowed_ipv4", "port_range", "max_concurrency",
    "connect_timeout_s", "total_deadline_s", "scan_cooldown_s",
    "max_verify_candidates",
    "verify_timeout_s",
})
_CONNECTING = frozenset({
    errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
    10035, 10036, 10037,  # Winsock nonblocking connect results
})


def _number(value: object, name: str, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not lower <= result <= upper:
        raise ValueError(f"{name} must be in [{lower}, {upper}]")
    return result


def _integer(value: object, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer in [{lower}, {upper}]")
    return value


class EndpointDiscovery:
    """Prefer cached/mDNS candidates, then perform one cooled, bounded scan.

    `mdns_endpoints` is supplied by the caller's existing, read-only mDNS query;
    entries outside the configured IPv4 allowlist are silently rejected.  A
    returned endpoint is always one accepted by verify_candidate, never merely
    an open TCP port.  None means unavailable, cancelled, disabled, on cooldown,
    or budget exhausted; last_report distinguishes these cases.
    """

    def __init__(self, config: dict):
        if not isinstance(config, dict):
            raise ValueError("discovery config must be an object")
        unknown = set(config) - _CONFIG_KEYS
        if unknown:
            raise ValueError("unknown discovery config keys: " + ", ".join(sorted(unknown)))
        enabled = config.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        raw_addresses = config.get("allowed_ipv4", [])
        if not isinstance(raw_addresses, (list, tuple)) or len(raw_addresses) > 4:
            raise ValueError("allowed_ipv4 must be an explicit list of at most four IPv4 addresses")
        addresses = []
        for value in raw_addresses:
            if not isinstance(value, str):
                raise ValueError("allowlist entries must be literal IPv4 strings")
            try:
                address = ipaddress.IPv4Address(value)
            except ipaddress.AddressValueError as exc:
                raise ValueError("allowlist entries must be literal IPv4 strings") from exc
            if (str(address) != value or address.is_multicast or address.is_unspecified
                    or address.is_loopback or address.is_reserved
                    or value == "255.255.255.255"):
                raise ValueError("allowlist entries must be canonical unicast phone IPv4 addresses")
            if value not in addresses:
                addresses.append(value)
        if enabled and not addresses:
            raise ValueError("enabled discovery requires an explicit IPv4 allowlist")
        raw_range = config.get("port_range", [37000, 50000])
        if not isinstance(raw_range, (list, tuple)) or len(raw_range) != 2:
            raise ValueError("port_range must contain inclusive lower and upper bounds")
        lower = _integer(raw_range[0], "port_range lower", 1024, 65535)
        upper = _integer(raw_range[1], "port_range upper", lower, 65535)
        if upper - lower + 1 > 32768:
            raise ValueError("port_range is too wide")
        self.enabled = enabled
        self.allowed_ipv4 = tuple(addresses)
        self.port_range = (lower, upper)
        self.max_concurrency = _integer(config.get("max_concurrency", 128), "max_concurrency", 1, 128)
        self.connect_timeout_s = _number(config.get("connect_timeout_s", 0.35), "connect_timeout_s", 0.01, 5)
        self.total_deadline_s = _number(config.get("total_deadline_s", 45), "total_deadline_s", 0.05, 120)
        self.scan_cooldown_s = _number(config.get("scan_cooldown_s", 60), "scan_cooldown_s", 60, 3600)
        self.max_verify_candidates = _integer(config.get("max_verify_candidates", 16), "max_verify_candidates", 1, 128)
        self.verify_timeout_s = _number(config.get("verify_timeout_s", 8), "verify_timeout_s", 0.05, 30)
        self._last_scan_finished: float | None = None
        self._port_count = upper - lower + 1
        self._endpoint_count = len(self.allowed_ipv4) * self._port_count
        self._cursor = 0
        self._fresh_remaining = self._endpoint_count
        self._deferred: deque[int] = deque()
        self._deferred_set: set[int] = set()
        self._call_lock = threading.Lock()
        self.last_report: dict = {}

    def _endpoint(self, value: object) -> str | None:
        if not isinstance(value, str) or value.count(":") != 1:
            return None
        host, raw_port = value.split(":", 1)
        if host not in self.allowed_ipv4 or not raw_port.isascii() or not raw_port.isdecimal():
            return None
        port = int(raw_port)
        return f"{host}:{port}" if 1 <= port <= 65535 else None

    def discover(
        self,
        cached_endpoint: str | None = None,
        mdns_endpoints: Iterable[str] = (),
        verify_candidate: Callable[[str, float], bool] | None = None,
        *,
        cancel_event: threading.Event | None = None,
        max_seconds: float | None = None,
    ) -> str | None:
        if not callable(verify_candidate):
            raise ValueError("an identity-checking verify_candidate callback is required")
        budget = self.total_deadline_s
        if max_seconds is not None:
            budget = min(budget, _number(max_seconds, "max_seconds", 0, 86400))
        if not self._call_lock.acquire(blocking=False):
            return None
        started = time.monotonic()
        deadline = started + budget
        scan_started = False
        retries: deque[int] = deque()
        report = {
            "reason": "unavailable", "cached_attempts": 0, "mdns_attempts": 0,
            "tcp_attempts": 0, "open_ports": 0, "verified_candidates": 0,
            "verification_errors": 0, "rejected_endpoints": 0,
            "probe_errors": 0,
            "scan_complete": False, "cancelled": False,
            "resumed_scan": False, "scan_remaining": self._fresh_remaining + len(self._deferred),
        }
        self.last_report = report
        attempted: set[str] = set()

        def interrupted() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                report.update(reason="cancelled", cancelled=True)
                return True
            if time.monotonic() >= deadline:
                report["reason"] = "deadline"
                return True
            return False

        def verify(value: object, source: str) -> str | None:
            endpoint = self._endpoint(value)
            if endpoint is None:
                report["rejected_endpoints"] += 1
                return None
            if endpoint in attempted or interrupted():
                return None
            if report["verified_candidates"] >= self.max_verify_candidates:
                report["reason"] = "candidate_limit"
                return None
            attempted.add(endpoint)
            report["verified_candidates"] += 1
            if source in ("cached", "mdns"):
                report[source + "_attempts"] += 1
            try:
                accepted = verify_candidate(endpoint, max(0.0, min(
                    self.verify_timeout_s, budget, deadline - time.monotonic())))
            except Exception as error:
                # The transport owner's fatal control failures (e.g. a pinned
                # binary/server mismatch or unavailable logging) must end the
                # caller's operation, never turn into a loop scanning more ports.
                if getattr(error, "wireless_discovery_fatal", False) is True:
                    report["reason"] = "fatal_verifier_error"
                    raise
                report["verification_errors"] += 1
                return None
            if interrupted():
                return None
            if accepted is True:
                report["reason"] = "verified_" + source
                return endpoint
            return None

        try:
            if cached_endpoint is not None:
                result = verify(cached_endpoint, "cached")
                if result is not None:
                    return result
            # Bound even a malformed/infinite provider iterable; never enumerate
            # arbitrary LAN services or accumulate an unbounded service list.
            for index, endpoint in enumerate(mdns_endpoints):
                if index >= 128 or interrupted():
                    break
                result = verify(endpoint, "mdns")
                if result is not None:
                    return result
                if report["reason"] == "candidate_limit":
                    return None
            if interrupted():
                return None
            if not self.enabled:
                report["reason"] = "disabled"
                return None
            now = time.monotonic()
            if self._last_scan_finished is not None and now - self._last_scan_finished < self.scan_cooldown_s:
                report["reason"] = "cooldown"
                return None
            scan_started = True
            if self._fresh_remaining == 0 and not self._deferred:
                self._fresh_remaining = self._endpoint_count
            report["resumed_scan"] = (self._fresh_remaining != self._endpoint_count or bool(self._deferred))
            # New ports precede interrupted probes. This guarantees progress even
            # when a parent repeatedly grants less than one connect timeout.
            # Each deferred index is retried at most once per discover call.
            retries = self._deferred
            self._deferred = deque()
            pending: dict[socket.socket, tuple[str, int, float]] = {}
            ready_candidates: deque[tuple[str, int]] = deque()

            def defer(index: int) -> None:
                if index not in self._deferred_set:
                    self._deferred_set.add(index)
                    self._deferred.append(index)

            def verify_tcp(endpoint: str, index: int) -> str | None:
                try:
                    # Close already-ready sockets and retain their candidates
                    # before the synchronous verifier can advance wall time.
                    for key, _ in selector.select(0):
                        probe = key.fileobj
                        state = pending.get(probe)
                        if state is None:
                            continue
                        other_endpoint, other_index, _ = state
                        try:
                            connected = probe.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0
                        except OSError:
                            connected = False
                        close_probe(probe)
                        if connected:
                            report["open_ports"] += 1
                            ready_candidates.append((other_endpoint, other_index))
                    # Unfinished connects must not turn into false timeouts while
                    # ADB verification blocks; retry them in a later slice.
                    for probe in tuple(pending):
                        _, other_index, _ = pending[probe]
                        defer(other_index)
                        close_probe(probe)
                    result = verify(endpoint, "tcp")
                except Exception:
                    defer(index)
                    raise
                if result is None and report["reason"] in {"deadline", "cancelled", "candidate_limit"}:
                    defer(index)
                return result

            with selectors.DefaultSelector() as selector:
                def close_probe(probe: socket.socket) -> None:
                    pending.pop(probe, None)
                    try:
                        selector.unregister(probe)
                    except (KeyError, OSError, ValueError):
                        pass
                    probe.close()

                try:
                    while not interrupted():
                        if report["reason"] == "candidate_limit":
                            return None
                        if ready_candidates:
                            endpoint, index = ready_candidates.popleft()
                            result = verify_tcp(endpoint, index)
                            if result is not None:
                                return result
                            continue
                        while ((self._fresh_remaining or retries)
                               and len(pending) + len(ready_candidates) < self.max_concurrency and not interrupted()):
                            fresh = bool(self._fresh_remaining)
                            index = self._cursor if fresh else retries[0]
                            host = self.allowed_ipv4[index // self._port_count]
                            port = self.port_range[0] + index % self._port_count
                            endpoint = f"{host}:{port}"
                            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            attempted_probe = False
                            try:
                                probe.setblocking(False)
                                # Advance only once a real TCP attempt is about to
                                # start. Interrupted/unchecked results are requeued.
                                if fresh:
                                    self._cursor = (index + 1) % self._endpoint_count
                                    self._fresh_remaining -= 1
                                else:
                                    retries.popleft()
                                    self._deferred_set.remove(index)
                                report["tcp_attempts"] += 1
                                attempted_probe = True
                                code = probe.connect_ex((host, port))
                                if code in (0, errno.EISCONN, 10056):
                                    probe.close()
                                    report["open_ports"] += 1
                                    result = verify_tcp(endpoint, index)
                                    if result is not None:
                                        return result
                                    if report["reason"] == "candidate_limit":
                                        return None
                                elif code in _CONNECTING:
                                    pending[probe] = (endpoint, index, time.monotonic() + self.connect_timeout_s)
                                    selector.register(probe, selectors.EVENT_WRITE)
                                else:
                                    probe.close()
                            except OSError:
                                if attempted_probe:
                                    defer(index)
                                close_probe(probe)
                                if not attempted_probe:
                                    raise
                        if ready_candidates:
                            continue
                        if not self._fresh_remaining and not retries and not pending:
                            if self._deferred:
                                report["reason"] = "scan_partial"
                            else:
                                report.update(reason="scan_complete", scan_complete=True)
                            return None
                        if interrupted():
                            return None
                        now = time.monotonic()
                        for probe, (_, _, expires) in tuple(pending.items()):
                            if now >= expires:
                                close_probe(probe)
                        if not pending:
                            continue
                        wait = max(0.0, min(0.1, deadline - now,
                                           min(expires for _, _, expires in pending.values()) - now))
                        for key, _ in selector.select(wait):
                            probe = key.fileobj
                            state = pending.get(probe)
                            if state is None:
                                continue
                            endpoint, index, expires = state
                            try:
                                connected = (time.monotonic() < expires
                                             and probe.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0)
                            except OSError:
                                connected = False
                            close_probe(probe)
                            if connected:
                                if interrupted():
                                    defer(index)
                                else:
                                    report["open_ports"] += 1
                                    result = verify_tcp(endpoint, index)
                                    if result is not None:
                                        return result
                                    if report["reason"] == "candidate_limit":
                                        return None
                finally:
                    for _, index in ready_candidates:
                        defer(index)
                    for probe in tuple(pending):
                        _, index, expires = pending[probe]
                        if time.monotonic() < expires:
                            defer(index)
                        close_probe(probe)
            return None
        except OSError:
            report.update(reason="probe_error", probe_errors=report["probe_errors"] + 1)
            return None
        finally:
            if retries:
                self._deferred.extendleft(reversed(retries))
            if scan_started and report["scan_complete"]:
                self._last_scan_finished = time.monotonic()
            report["scan_remaining"] = self._fresh_remaining + len(self._deferred)
            report["elapsed_s"] = round(time.monotonic() - started, 6)
            self._call_lock.release()
