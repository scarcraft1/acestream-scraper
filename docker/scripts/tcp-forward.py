#!/usr/bin/env python3
"""Forward a TCP port to another address, byte for byte.

The embedded Tor listens on 127.0.0.1:9050 inside the container, which a
published port cannot reach. This exposes it on another port of the
container's interfaces:

    tcp-forward.py --port 9150 --to 127.0.0.1:9050
"""
import argparse
import asyncio
import logging

log = logging.getLogger("tcp-forward")


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


def handler(target_host, target_port):
    async def handle(reader, writer):
        try:
            up_reader, up_writer = await asyncio.open_connection(target_host, target_port)
        except OSError as e:
            log.info("cannot reach %s:%s: %s", target_host, target_port, e)
            writer.close()
            return
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
    return handle


async def serve(host, port, target_host, target_port):
    server = await asyncio.start_server(handler(target_host, target_port), host, port)
    log.info("forwarding %s:%s -> %s:%s", host, port, target_host, target_port)
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--to", required=True, help="host:port")
    args = parser.parse_args()
    target_host, _, target_port = args.to.rpartition(":")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s tcp-forward: %(message)s")
    asyncio.run(serve(args.host, args.port, target_host, int(target_port)))


if __name__ == "__main__":
    main()
