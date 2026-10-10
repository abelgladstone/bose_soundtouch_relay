"""Generic bidirectional UDP relay.

AirPlay 2 buffered audio (which this speaker advertises: feature bits 40/41,
SupportsBufferedAudio + SupportsPTP) sends the audio itself over TCP but gates playback
on PTP clock synchronization over UDP ports 319/320. Those datagrams go to whatever
address mDNS advertised -- the relay -- so without a UDP path the speaker's clock never
syncs with the sender and it sits in BUFFERING_STATE until the session times out.

Unlike TCP there is no per-connection state to key on, so each `UdpPortRelay` is a
single socket that shuttles datagrams between exactly two peers: the speaker (known by
address) and whichever non-speaker peer sent a datagram most recently (the client).
One household streams to one speaker at a time, so a single most-recent-client mapping
is all that's needed. PTP payloads identify clocks by clock identity, not IP address,
so plain datagram forwarding is protocol-correct -- nothing needs rewriting.
"""

from __future__ import annotations

import asyncio
import logging

from relay.spoof import SpoofedUdpSender

_LOGGER = logging.getLogger(__name__)


class _RelayProtocol(asyncio.DatagramProtocol):
    def __init__(self, relay: "UdpPortRelay"):
        self._relay = relay
        self._transport: asyncio.DatagramTransport | None = None
        self.closed: asyncio.Future[None] = asyncio.get_event_loop().create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.closed.done():
            self.closed.set_result(None)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        relay = self._relay
        if addr[0] == relay.target_host:
            if relay.client_addr is None:
                _LOGGER.debug(
                    "udp %s:%d: datagram from target before any client; dropping",
                    relay.bind_host, relay.port,
                )
                return
            self._transport.sendto(data, relay.client_addr)
        else:
            relay.client_addr = addr
            if relay.spoof_sender is not None:
                relay.spoof_sender.send(addr, (relay.target_host, relay.target_port), data)
            else:
                self._transport.sendto(data, (relay.target_host, relay.target_port))


class UdpPortRelay:
    """Binds (bind_host, port) and shuttles datagrams between the target and the
    most recent non-target peer, both ways, from the one bound socket -- so forwarded
    datagrams keep the relay's well-known source port (PTP peers expect 319/320)."""

    def __init__(
        self,
        bind_host: str,
        port: int,
        target_host: str,
        target_port: int | None = None,
        spoof_sender: SpoofedUdpSender | None = None,
    ):
        self.bind_host = bind_host
        self.port = port
        self.target_host = target_host
        self.target_port = port if target_port is None else target_port
        self.client_addr: tuple[str, int] | None = None
        self.spoof_sender = spoof_sender
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _RelayProtocol | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: _RelayProtocol(self), local_addr=(self.bind_host, self.port)
        )
        _LOGGER.info(
            "listening on udp %s:%d <-> %s:%d",
            self.bind_host, self.port, self.target_host, self.target_port,
        )

    async def stop(self) -> None:
        if self._transport is None:
            return
        self._transport.close()
        await self._protocol.closed  # socket is only really released on connection_lost
        self._transport = None
        self._protocol = None
