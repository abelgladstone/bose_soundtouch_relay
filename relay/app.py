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
