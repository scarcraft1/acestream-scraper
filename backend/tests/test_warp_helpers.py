"""WARP helpers started by warp-setup.sh: inbound connections and the SOCKS port."""
import asyncio
import importlib.util
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / "warp-setup.sh"
SOCKS = ROOT / "docker" / "scripts" / "warp-socks.py"


def _load_socks():
    spec = importlib.util.spec_from_file_location("warp_socks", SOCKS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_configure(tmp_path, **extra_env):
    """Run `warp-setup.sh configure` with fake warp-cli and helper scripts that
    only record that they were started (and stay alive, like the real ones)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    cli = bin_dir / "warp-cli"
    cli.write_text("#!/bin/sh\necho 'Status update: Connected'\n")
    cli.chmod(0o755)
    (scripts / "warp-inbound.sh").write_text(
        'echo inbound >> "$WARP_TEST_DIR/started"\nexec sleep 30\n')
    (scripts / "warp-socks.py").write_text(
        "import os, sys, time\n"
        "open(os.path.join(os.environ['WARP_TEST_DIR'], 'started'), 'a')"
        ".write('socks ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "time.sleep(30)\n")
    env = {
        **os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "WARP_TEST_DIR": str(tmp_path), "LOG_DIR": str(tmp_path / "logs"),
        "ENABLE_WARP": "true", "WARP_ENABLE_NAT": "false",
        "WARP_REQUIRED_COMMANDS": "warp-cli", "WARP_READY_ATTEMPTS": "2",
        "WARP_READY_INTERVAL": "0.01", "WARP_LICENSE_KEY": "",
        "WARP_SCRIPTS_DIR": str(scripts), "WARP_RUN_DIR": str(run_dir),
        **extra_env,
    }
    result = subprocess.run(["bash", str(SETUP), "configure"], env=env,
                            capture_output=True, text=True, timeout=10)
    return result, run_dir


def started(tmp_path, expect, timeout=5):
    path = tmp_path / "started"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = path.read_text().splitlines() if path.exists() else []
        if len(lines) >= expect:
            return lines
        time.sleep(0.05)
    return path.read_text().splitlines() if path.exists() else []


def kill_helpers(run_dir):
    for pidfile in run_dir.glob("warp-*.pid"):
        try:
            os.kill(int(pidfile.read_text()), 9)
        except (ProcessLookupError, ValueError):
            pass


def test_no_helpers_by_default(tmp_path):
    result, run_dir = run_configure(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not list(run_dir.glob("warp-*.pid"))
    assert not (tmp_path / "started").exists()


def test_helpers_start_once_across_reconfigures(tmp_path):
    env = {"WARP_ALLOW_INBOUND": "true", "WARP_SOCKS_PORT": "1080"}
    try:
        for _ in range(3):  # configure runs again after every warp-svc restart
            result, run_dir = run_configure(tmp_path, **env)
            assert result.returncode == 0, result.stderr
        lines = started(tmp_path, 2)
        time.sleep(0.3)  # a duplicate would show up here
        lines = started(tmp_path, 2)
        assert sorted(lines) == ["inbound", "socks --port 1080"]
    finally:
        kill_helpers(tmp_path / "run")


def test_helper_restarts_after_it_died(tmp_path):
    env = {"WARP_SOCKS_PORT": "1080"}
    try:
        run_configure(tmp_path, **env)
        assert started(tmp_path, 1) == ["socks --port 1080"]
        kill_helpers(tmp_path / "run")
        time.sleep(0.2)
        run_configure(tmp_path, **env)
        assert started(tmp_path, 2) == ["socks --port 1080", "socks --port 1080"]
    finally:
        kill_helpers(tmp_path / "run")


def _socks_connect(port, host, target_port, domain=True):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"\x05\x01\x00")
    assert s.recv(2) == b"\x05\x00"
    if domain:
        addr = b"\x03" + bytes([len(host)]) + host.encode()
    else:
        addr = b"\x01" + socket.inet_aton(host)
    s.sendall(b"\x05\x01\x00" + addr + struct.pack("!H", target_port))
    reply = s.recv(10)
    return s, reply


def test_socks_relays_by_name_and_by_address():
    warp_socks = _load_socks()

    async def scenario():
        async def echo(reader, writer):
            data = await reader.read(100)
            writer.write(b"echo:" + data)
            await writer.drain()
            writer.close()

        echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        echo_port = echo_server.sockets[0].getsockname()[1]
        socks_server = await asyncio.start_server(warp_socks.handle, "127.0.0.1", 0)
        socks_port = socks_server.sockets[0].getsockname()[1]

        def client(domain):
            s, reply = _socks_connect(socks_port, "localhost" if domain else "127.0.0.1",
                                      echo_port, domain)
            assert reply[:2] == b"\x05\x00", reply
            s.sendall(b"hola")
            data = s.recv(100)
            s.close()
            return data

        try:
            for domain in (True, False):
                assert await asyncio.to_thread(client, domain) == b"echo:hola"
        finally:
            echo_server.close()
            socks_server.close()

    asyncio.run(scenario())


def test_socks_reports_refused_and_unsupported_commands():
    warp_socks = _load_socks()

    async def scenario():
        socks_server = await asyncio.start_server(warp_socks.handle, "127.0.0.1", 0)
        socks_port = socks_server.sockets[0].getsockname()[1]
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        dead_port = closed.getsockname()[1]
        closed.close()

        def refused():
            s, reply = _socks_connect(socks_port, "127.0.0.1", dead_port, domain=False)
            s.close()
            return reply[1]

        def bind_command():
            s = socket.create_connection(("127.0.0.1", socks_port), timeout=5)
            s.sendall(b"\x05\x01\x00")
            s.recv(2)
            s.sendall(b"\x05\x02\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", 80))
            reply = s.recv(10)
            s.close()
            return reply[1]

        try:
            assert await asyncio.to_thread(refused) == warp_socks.REP_REFUSED
            assert await asyncio.to_thread(bind_command) == warp_socks.REP_CMD_UNSUPPORTED
        finally:
            socks_server.close()

    asyncio.run(scenario())


def test_dockerfile_fork_installs_the_helpers():
    text = (ROOT / "Dockerfile.fork").read_text()
    for name in ("warp-setup.sh", "warp-inbound.sh", "warp-socks.py"):
        assert name in text, name
