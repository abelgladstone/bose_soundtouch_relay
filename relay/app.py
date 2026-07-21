"""Entry point: brings up the TCP relays and mDNS impersonation for the SoundTouch speaker."""

from __future__ import annotations

import asyncio
import logging
import signal

from relay.airplay import name_rewriter
from relay.config import Config
from relay.mdns import ImpersonationBridge, OnService
from relay.proxy import RelayManager

_LOGGER = logging.getLogger(__name__)

# _raop._tcp shares the same control port/connection as _airplay._tcp for this speaker, but
# its instance name has an ugly MAC-address prefix (e.g. "AABBCCDDEEFF@Zola"); only
# _airplay._tcp's clean name ("Zola") is used for the /info rewrite. If _raop is discovered
# first it just gets a plain (unhooked) relay -- RelayManager.ensure upgrades that to a
# hooked one once _airplay._tcp is seen, without ever downgrading back.
_AIRPLAY_INFO_SERVICE_TYPE = "_airplay._tcp.local."

# AirPlay 2 opens a second, dynamically-numbered "event channel" connection during pairing,
# and the port number is embedded in the encrypted pair-verify/pair-setup exchange -- the
# relay can never read it off the wire. Trying to *discover* it by probing the speaker in
# advance doesn't work either (measured against the real device: a full-range scan takes
# 2-3s, past the client's own ~2s patience, and the speaker doesn't reliably respond to
# probes under load anyway). So instead of guessing which port and racing to open a
# listener before the client tries it, the relay just permanently listens on every port in
# this range from startup and forwards on demand: whichever port the client picks, a
# listener is already there, and the relay only ever opens its own connection to the
# speaker at the moment a real client connects to it -- it never probes the speaker itself,
# so there's no race and no scan traffic hitting an already fragile embedded stack.
#
# Deliberately stops short of 65535: this range doubles as every TCP listening port the
# relay host itself binds to on that interface, and with network_mode: host those sockets
# aren't sandboxed -- they compete for the same pool the kernel hands out as *source* ports
# for any outbound connection the host (or any other container, via Docker's SNAT) makes.
# Binding the full 32768-65535 once starved the whole host of outbound ports and broke
# unrelated services. The host's own ephemeral range must be narrowed to a reserved slice
# above this range (net.ipv4.ip_local_port_range, see README.md) so the two never overlap.
AIRPLAY_EVENT_PORT_RANGE = range(32768, 61001)

# AirPlay 2 buffered audio (this speaker advertises feature bits 40/41:
# SupportsBufferedAudio + SupportsPTP) carries the audio itself over TCP but gates
# playback on PTP clock sync over UDP 319/320, sent to whatever address mDNS advertised
# -- the relay. Without a UDP path the speaker's clock never syncs and it stalls in
# BUFFERING_STATE until the session times out (~10s), which was the observed failure.
AIRPLAY_PTP_PORTS = (319, 320)


async def ensure_airplay_ptp_ports(
    manager: RelayManager, ports: tuple[int, ...] = AIRPLAY_PTP_PORTS
) -> None:
    for port in ports:
        try:
            await manager.ensure_udp(port)
        except OSError:
            # E.g. a PTP daemon on the relay host already owns the port -- log loudly,
            # since AirPlay playback cannot work without this path, but keep running.
            _LOGGER.error("could not open UDP relay for PTP port %d", port, exc_info=True)


async def ensure_airplay_event_ports(
    manager: RelayManager, ports: range = AIRPLAY_EVENT_PORT_RANGE
) -> None:
    # Both TCP and UDP, same range: besides the TCP event channel, the encrypted SETUP
    # exchange also names dynamically-numbered *UDP* ports on the speaker for
    # timing/control (observed live: a real session sent steady UDP to two ports in this
    # range on the relay; with no listener those datagrams silently died while the
    # client kept retrying). Same standby approach, same-port forwarding.
    for port in ports:
        try:
            await manager.ensure(port)
        except OSError:
            # E.g. something else on the relay host already has this exact port bound --
            # log and keep going so one collision can't stop the rest of the range.
            _LOGGER.warning("could not open standby relay for port %d", port, exc_info=True)
        try:
            await manager.ensure_udp(port)
        except OSError:
            _LOGGER.warning("could not open standby UDP relay for port %d", port, exc_info=True)


def make_on_service(manager: RelayManager) -> OnService:
    """Build the mDNS-discovery callback: relay every discovered port, with the AirPlay
    /info name-rewrite hook attached only for _airplay._tcp itself."""

    async def on_service(type_: str, port: int, display_name: str) -> None:
        if type_ == _AIRPLAY_INFO_SERVICE_TYPE:
            await manager.ensure(port, on_first_server_message=name_rewriter(display_name))
        else:
            await manager.ensure(port)

    return on_service


async def run(config: Config) -> None:
    manager = RelayManager(config.relay_host, config.speaker_host)

    # Ports that always need relaying regardless of what mDNS discovers this run.
    for port in config.extra_ports:
        await manager.ensure(port)

    if _AIRPLAY_INFO_SERVICE_TYPE in config.service_types:
        await ensure_airplay_event_ports(manager)
        await ensure_airplay_ptp_ports(manager)

    loop = asyncio.get_running_loop()

    bridge = ImpersonationBridge(
        service_types=config.service_types,
        speaker_host=config.speaker_host,
        relay_host=config.relay_host,
        on_service=make_on_service(manager),
        loop=loop,
        name_suffix=config.name_suffix,
    )
    bridge.start()
    _LOGGER.info("relay running: %s -> %s", config.relay_host, config.speaker_host)

    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        _LOGGER.info("shutting down")
        bridge.stop()
        await manager.stop_all()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = Config.load()
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
