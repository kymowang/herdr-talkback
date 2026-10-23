"""Newline JSON transport: Unix domain sockets or Windows named pipes."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path


def pipe_name(path):
    # Herdr's interprocess GenericNamespaced transport prefixes the path verbatim.
    return '\\\\.\\pipe\\' + str(path)


async def open_local_connection(path, limit=64 * 1024):
    if os.name != 'nt':
        return await asyncio.open_unix_connection(str(path), limit=limit)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=limit)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.create_pipe_connection(lambda: protocol, pipe_name(path))
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)


class LocalServer:
    def __init__(self, servers, path):
        self.servers = servers
        self.path = Path(path)
        self.closed = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.close()
        await self.wait_closed()

    async def serve_forever(self):
        await self.closed.wait()

    def close(self):
        if self.closed.is_set():
            return
        for server in self.servers:
            server.close()
        self.path.unlink(missing_ok=True)
        self.closed.set()

    async def wait_closed(self):
        for server in self.servers:
            if hasattr(server, 'wait_closed'):
                await server.wait_closed()


async def start_local_server(handler, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        server = await asyncio.start_unix_server(handler, path=str(path))
        return LocalServer([server], path)
    loop = asyncio.get_running_loop()

    def factory():
        reader = asyncio.StreamReader()
        return asyncio.StreamReaderProtocol(reader, handler)

    servers = await loop.start_serving_pipe(factory, pipe_name(path))
    path.write_text('named-pipe\n', encoding='utf-8')
    return LocalServer(servers, path)


def local_endpoint_running(path):
    try:
        if os.name == 'nt':
            # Opening a pipe is synchronous and only used by the startup guard.
            with open(pipe_name(path), 'r+b', buffering=0):
                return True
        else:
            import socket

            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(0.3)
                sock.connect(str(path))
                return True
    except OSError:
        return False
