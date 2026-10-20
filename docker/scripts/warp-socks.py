#!/usr/bin/env python3
"""Minimal SOCKS5 proxy (no auth, CONNECT only) for WARP in tunnel mode.

In tunnel mode everything the container sends leaves through WARP, but other
programs (a media centre add-on on the host, another container) cannot use
that tunnel. warp-cli's own proxy mode would give them a SOCKS port, at the
price of switching the tunnel off for the scraper. This server keeps the
tunnel and offers the port: what it relays is sent from inside the container,
so it leaves through WARP too.

Domain names are resolved here (socks5h), so lookups also go through WARP.
Only CONNECT is supported; BIND and UDP ASSOCIATE are refused.

    warp-socks.py --port 1080 [--host 0.0.0.0]
"""
import argparse
import asyncio
import ipaddress
import logging
import socket
import struct

log = logging.getLogger("warp-socks")

VERSION = 5
NO_AUTH = 0
NO_ACCEPTABLE = 0xFF
CONNECT = 1
ATYP_IPV4, ATYP_DOMAIN, ATYP_IPV6 = 1, 3, 4
REP_OK, REP_FAILURE, REP_UNREACHABLE, REP_REFUSED = 0, 1, 4, 5
REP_CMD_UNSUPPORTED, REP_ATYP_UNSUPPORTED = 7, 8
CONNECT_TIMEOUT = 30


async def _read_request(reader):
    """Return (host, port) from a CONNECT request, or raise ValueError(rep)."""
    ver, cmd, _rsv, atyp = await reader.readexactly(4)
    if ver != VERSION:
        raise ValueError(REP_FAILURE)
    if atyp == ATYP_IPV4:
        host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
    elif atyp == ATYP_IPV6:
        host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
    elif atyp == ATYP_DOMAIN:
        length = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(length)).decode("idna")
    else:
        raise ValueError(REP_ATYP_UNSUPPORTED)
    port = struct.unpack("!H", await reader.readexactly(2))[0]
    if cmd != CONNECT:
        raise ValueError(REP_CMD_UNSUPPORTED)
    return host, port


def _reply(rep, bound=("0.0.0.0", 0)):
    try:
        addr = ipaddress.ip_address(bound[0])
    except ValueError:
        addr = ipaddress.IPv4Address("0.0.0.0")
    atyp = ATYP_IPV6 if addr.version == 6 else ATYP_IPV4
    return bytes([VERSION, rep, 0, atyp]) + addr.packed + struct.pack("!H", bound[1])


async def _pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def handle(reader, writer):
    peer = writer.get_extra_info("peername")
    try:
        ver, nmethods = await reader.readexactly(2)
        methods = await reader.readexactly(nmethods)
        if ver != VERSION or NO_AUTH not in methods:
            writer.write(bytes([VERSION, NO_ACCEPTABLE]))
            await writer.drain()
            return
        writer.write(bytes([VERSION, NO_AUTH]))
        await writer.drain()
        try:
            host, port = await _read_request(reader)
        except ValueError as e:
            writer.write(_reply(e.args[0]))
            await writer.drain()
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), CONNECT_TIMEOUT)
        except (socket.gaierror, asyncio.TimeoutError, OSError) as e:
            rep = REP_REFUSED if isinstance(e, ConnectionRefusedError) else REP_UNREACHABLE
            log.info("%s -> %s:%s failed: %s", peer, host, port, e)
            writer.write(_reply(rep))
            await writer.drain()
            return
        writer.write(_reply(REP_OK, up_writer.get_extra_info("sockname")[:2]))
        await writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def serve(host, port):
    server = await asyncio.start_server(handle, host, port)
    log.info("SOCKS5 listening on %s:%s", host, port)
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s warp-socks: %(message)s")
    asyncio.run(serve(args.host, args.port))


if __name__ == "__main__":
    main()
