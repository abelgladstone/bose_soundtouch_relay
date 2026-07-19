"""Generic bidirectional TCP relay.

Each `PortRelay` listens on one (bind_host, port) and, for every incoming connection,
opens a same-subnet connection to (target_host, target_port) and pipes bytes both ways.
This is what lets clients on VLAN1 reach a speaker that only replies to on-link peers: the
relay itself is on-link with the speaker, so the speaker never sees a cross-subnet client.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)
_CHUNK_SIZE = 65536

# Given the server->client reader/writer, do whatever protocol-specific inspection/rewrite
# is needed for the first message on the connection, then return so plain byte-piping takes
# over for everything after. Must consume from `reader` and write to `writer` itself for
# whatever it inspects -- nothing already consumed here is seen again by the pump.
FirstMessageHook = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(_CHUNK_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()


class PortRelay:
    """Listens on (bind_host, port) and pipes every connection to (target_host, target_port)."""

    def __init__(
        self,
        bind_host: str,
        port: int,
        target_host: str,
        target_port: int | None = None,
        on_first_server_message: FirstMessageHook | None = None,
    ):
        self.bind_host = bind_host
        self.port = port
        self.target_host = target_host
        self.target_port = port if target_port is None else target_port
        self.has_hook = on_first_server_message is not None
        self._on_first_server_message = on_first_server_message
        self._server: asyncio.AbstractServer | None = None

    async def _handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        peer = client_writer.get_extra_info("peername")
        try:
            server_reader, server_writer = await asyncio.open_connection(
                self.target_host, self.target_port
            )
        except OSError as err:
            _LOGGER.warning(
                "%s:%d: cannot reach target %s:%d (%s)",
                self.bind_host, self.port, self.target_host, self.target_port, err,
            )
            client_writer.close()
            return

        _LOGGER.debug(
            "%s:%d: %s <-> %s:%d", self.bind_host, self.port, peer, self.target_host, self.target_port
        )
        await asyncio.gather(
            _pump(client_reader, server_writer),
            self._relay_server_to_client(server_reader, client_writer),
        )

    async def _relay_server_to_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._on_first_server_message:
            try:
                await self._on_first_server_message(reader, writer)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError):
                # Connection ended, or didn't look like the message we expected -- either
                # way, whatever bytes were already consumed are gone; just carry on with
                # whatever's left so a malformed/short first message doesn't wedge the relay.
                _LOGGER.debug("first-message hook did not complete cleanly", exc_info=True)
        await _pump(reader, writer)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.bind_host, self.port)
        _LOGGER.info(
            "listening on %s:%d -> %s:%d", self.bind_host, self.port, self.target_host, self.target_port
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None


class RelayManager:
    """Tracks active PortRelays by port so mDNS discovery can add/remove them idempotently."""

    def __init__(self, bind_host: str, target_host: str):
        self.bind_host = bind_host
        self.target_host = target_host
        self._relays: dict[int, PortRelay] = {}
        self._lock = asyncio.Lock()

    async def ensure(
        self, port: int, on_first_server_message: FirstMessageHook | None = None
    ) -> None:
        async with self._lock:
            existing = self._relays.get(port)
            if existing:
                if on_first_server_message is not None and not existing.has_hook:
                    await existing.stop()  # upgrade a plain relay once we learn it needs the hook
                else:
                    return  # already relaying this port, and not downgrading a hooked one
            relay = PortRelay(
                self.bind_host, port, self.target_host, on_first_server_message=on_first_server_message
            )
            await relay.start()
            self._relays[port] = relay

    async def remove(self, port: int) -> None:
        async with self._lock:
            relay = self._relays.pop(port, None)
        if relay:
            await relay.stop()

    async def stop_all(self) -> None:
        async with self._lock:
            relays = list(self._relays.values())
            self._relays.clear()
        for relay in relays:
            await relay.stop()
