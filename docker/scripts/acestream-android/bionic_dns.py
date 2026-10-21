"""Share Android's fixed DNS socket across bundled engine processes.

The upstream listener removes its socket in finally, including after a failed
bind. A second engine must therefore never call it while another owns the path.
The lock stays open for the lifetime of the listener. When its engine exits,
the other engine's waiting thread takes over and removes the stale socket.
"""
import fcntl
import ipaddress
import os
import socket
import struct
import threading
import time


def container_resolver(resolver_type, log, path="/etc/resolv.conf"):
    """Follow Docker/WARP resolver changes without restarting either engine.

    Keep the last valid servers during a partial rewrite. Refresh before DNS
    work, not on a timer, so reconnects also work after a long idle period.
    """
    class ReloadingResolver(resolver_type):
        def __init__(self):
            super().__init__(configure=False)
            self._configuration_lock = threading.Lock()
            self._configuration_error = None
            self._refresh(required=True)

        def _refresh(self, required=False):
            with self._configuration_lock:
                try:
                    with open(path) as handle:
                        content = handle.read(65537)
                    if len(content) > 65536:
                        raise ValueError("resolver configuration exceeds 64 KiB")
                    servers = []
                    for line in content.splitlines():
                        fields = line.split("#", 1)[0].split()
                        if len(fields) == 2 and fields[0] == "nameserver":
                            ipaddress.ip_address(fields[1])
                            if fields[1] not in servers:
                                servers.append(fields[1])
                    if not servers:
                        raise ValueError("no nameservers configured")
                    if list(self.nameservers) != servers:
                        self.nameservers = servers
                        log("bionic DNS resolver configuration updated")
                    self._configuration_error = None
                except (OSError, ValueError) as exc:
                    if required:
                        raise
                    error = type(exc).__name__
                    if self._configuration_error != error:
                        log("bionic DNS configuration unavailable; retaining last valid servers")
                        self._configuration_error = error

        def resolve(self, *args, **kwargs):
            self._refresh()
            return super().resolve(*args, **kwargs)

    return ReloadingResolver()


# Bionic getaddrinfo error codes (bionic/libc/include/netdb.h).
EAI_AGAIN = 2
EAI_NODATA = 7
EAI_NONAME = 8


def _error_code(exc):
    """A missing name is final; anything else (timeouts, no reachable server,
    a malformed query) is temporary, so the engine retries instead of
    deciding it is offline. dnspython is matched by class name: this module
    must import without it."""
    name = type(exc).__name__
    if name == "NXDOMAIN":
        return EAI_NONAME
    if name == "NoAnswer":
        return EAI_NODATA
    return EAI_AGAIN


def _query_host(query):
    """The host after the getaddrinfo command, wherever the command starts.

    The payload's listener took the second word, which is wrong when the
    client prefixes a sequence number."""
    words = query.split(b"\0", 1)[0].split()
    for index, word in enumerate(words[:-1]):
        if word == b"getaddrinfo":
            return words[index + 1].decode("utf-8", "replace")
    raise ValueError("not a getaddrinfo query")


def _addrinfo_response(ip):
    # Same single IPv4 addrinfo as the payload's listener.
    return struct.pack("!4s I I I I I I I 4s I I I I", b"222", 1, 0, 2, 1, 6,
                       16, 0x02000050, socket.inet_aton(ip), 0, 0, 0, 0)


def _error_response(code):
    # The payload's listener sent the code as a little-endian int.
    return struct.pack("!4s I I", b"401", 4, code << 24)


class _FailureLog:
    """Logs failing queries, at most `limit` per minute, plus a count of the rest."""

    def __init__(self, log, limit=20):
        self._log, self._limit = log, limit
        self._lock = threading.Lock()
        self._minute, self._logged, self._dropped = None, 0, 0

    def __call__(self, query, exc, code):
        minute = int(time.monotonic() // 60)
        with self._lock:
            if minute != self._minute:
                if self._dropped:
                    self._log("bionic DNS: {} more failed queries not logged".format(self._dropped))
                self._minute, self._logged, self._dropped = minute, 0, 0
            if self._logged >= self._limit:
                self._dropped += 1
                return
            self._logged += 1
        self._log("bionic DNS query failed: {!r} -> {} (EAI {})".format(
            query[:160], type(exc).__name__, code))


def dnsproxy_listener(log, socket_path="/dev/socket/dnsproxyd", backlog=64):
    """A listener for run_shared_listener that answers clients concurrently.

    The payload's listener served one client at a time with a backlog of one,
    so simultaneous lookups were refused, and it answered every failure,
    including timeouts, with EAI_NODATA. Its reply was also sent outside its
    error handling: a client that went away took the listener down."""
    failed = _FailureLog(log)

    def answer(client, resolver):
        with client:
            query = b""
            try:
                client.settimeout(30)
                query = client.recv(1024)
                address = resolver.resolve(_query_host(query))[0].address
                response = _addrinfo_response(address)
            except Exception as exc:
                code = _error_code(exc)
                failed(query, exc, code)
                response = _error_response(code)
            try:
                client.sendall(response)
            except OSError:
                pass

    def listener(resolver):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(socket_path)
                server.listen(backlog)
                while True:
                    client, _ = server.accept()
                    threading.Thread(target=answer, args=(client, resolver),
                                     name="dnsproxyd-client", daemon=True).start()
        finally:
            try:
                os.unlink(socket_path)
            except FileNotFoundError:
                pass

    return listener


def run_shared_listener(listener, resolver, socket_path="/dev/socket/dnsproxyd"):
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    while True:
        try:
            # Never unlink the lock file: all engines must lock the same inode.
            with open(socket_path + ".lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    os.unlink(socket_path)
                except FileNotFoundError:
                    pass
                listener(resolver)
        except Exception as exc:
            print("bionic DNS listener failed; retrying: {}".format(exc), flush=True)
        time.sleep(1)


def start_shared_dns(listener, resolver):
    thread = threading.Thread(
        target=run_shared_listener, args=(listener, resolver),
        name="shared-dnsproxyd", daemon=True,
    )
    thread.start()
    return thread
