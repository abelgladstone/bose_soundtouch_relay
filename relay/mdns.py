"""mDNS impersonation.

Single-NIC design: the relay sits on the speaker's own VLAN (same subnet, so the speaker's
"only replies to on-link peers" bug never triggers) and relies on the network's existing
cross-VLAN mDNS reflection (already confirmed working -- see FINDINGS.md finding #1) to
carry its announcement to other VLANs. No second interface is needed for this: the relay
just needs to be a *reachable* alternative to the speaker, not sit inline with it.

Because the speaker's own (unusable) announcement is still being reflected too, the
relay's copy uses a distinct instance name and hostname so the two don't collide as
conflicting mDNS records on the wire -- clients will see both, but only the relay's copy
actually connects.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import threading
from typing import Awaitable, Callable

from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from relay.airplay import rewrite_feature_properties

_LOGGER = logging.getLogger(__name__)

# (service type, port, the relay's display name for this service) -> None
OnService = Callable[[str, int, str], Awaitable[None]]

# Embedded mDNS responders (this speaker included) commonly do a remove-then-re-add of a
# service record when its state changes -- e.g. AirPlay's active/inactive TXT flags on a
# source switch -- rather than a true in-place update. Unregistering the relay's
# impersonated copy the instant the real one disappears would send a goodbye packet that
# flashes "Zola (relay)" out of every client's mDNS cache network-wide for however long
# the re-add takes, leaving only the real (unreachable) "Zola" visible in the meantime.
# Give the real record this long to reappear before actually tearing the impersonated
# copy down. A live capture of an actual source change (2026-07-21) showed the real
# record flapping on and off repeatedly for ~11.5s before settling -- not one quick
# blip -- so this needs real margin above that, not just enough to cover a single blip.
_REMOVAL_GRACE_PERIOD_SECONDS = 20.0


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]+", "-", text).strip("-").lower() or "relay"


def relay_display_name(name: str, type_: str, suffix: str) -> str:
    """The human-readable instance label a DNS-SD name would show, with the suffix applied.

    e.g. ("Zola._soundtouch._tcp.local.", "_soundtouch._tcp.local.", " (relay)") -> "Zola (relay)"
    """
    instance = name[: -len(type_)].rstrip(".") if name.endswith(type_) else name
    return f"{instance}{suffix}"


def relay_instance_name(name: str, type_: str, suffix: str) -> str:
    """Rewrite a DNS-SD name's instance portion, keeping its service type intact.

    e.g. ("Zola._soundtouch._tcp.local.", "_soundtouch._tcp.local.", " (relay)")
      -> "Zola (relay)._soundtouch._tcp.local."
    """
    return f"{relay_display_name(name, type_, suffix)}.{type_}"


def impersonated_service_info(
    type_: str,
    name: str,
    info: ServiceInfo,
    relay_host: str,
    name_suffix: str,
    force_buffered: bool = False,
) -> ServiceInfo:
    """Build a ServiceInfo for the same service, addressed at the relay instead of the speaker.

    When `force_buffered`, the RAOP feature bit is cleared in the impersonated copy's
    `features`/`ft` TXT records so the advertisement matches the buffered-only /info the
    relay serves (see relay/airplay.py; FINDINGS.md issue #2)."""
    new_name = relay_instance_name(name, type_, name_suffix)
    server = f"{_slugify(new_name)}.local."
    properties = dict(info.properties or {})
    if force_buffered:
        properties = rewrite_feature_properties(properties)
    return ServiceInfo(
        type_,
        new_name,
        addresses=[socket.inet_aton(relay_host)],
        port=info.port,
        properties=properties,
        server=server,
    )


class ImpersonationBridge(ServiceListener):
    def __init__(
        self,
        service_types: list[str],
        speaker_host: str,
        relay_host: str,
        on_service: OnService,
        loop: asyncio.AbstractEventLoop,
        name_suffix: str = " (relay)",
        force_buffered: bool = False,
    ):
        self._service_types = service_types
        self._speaker_host = speaker_host
        self._relay_host = relay_host
        self._name_suffix = name_suffix
        self._force_buffered = force_buffered
        self._on_service = on_service
        self._loop = loop
        self._zc: Zeroconf | None = None
        self._browsers: list[ServiceBrowser] = []
        self._published: dict[str, ServiceInfo] = {}
        self._pending_removals: dict[str, threading.Timer] = {}

    def start(self) -> None:
        # One Zeroconf instance on the relay's single NIC: it browses for the speaker's
        # real announcements and publishes the relay's own copies on the very same
        # interface, trusting the network's existing multicast reflection to carry the
        # publish across VLANs (same mechanism that already reflects the speaker's).
        self._zc = Zeroconf(interfaces=[self._relay_host])
        self._browsers = [
            ServiceBrowser(self._zc, service_type, self) for service_type in self._service_types
        ]
        _LOGGER.info("mDNS bridge: browsing/publishing %s via %s", self._service_types, self._relay_host)

    def stop(self) -> None:
        for timer in self._pending_removals.values():
            timer.cancel()
        self._pending_removals.clear()
        for name, info in list(self._published.items()):
            self._unregister(name, info)
        if self._zc:
            self._zc.close()

    def _unregister(self, name: str, info: ServiceInfo) -> None:
        self._published.pop(name, None)
        try:
            self._zc.unregister_service(info)
        except Exception:
            _LOGGER.debug("unregister failed for %s (already gone?)", name, exc_info=True)

    def _schedule(self, type_: str, port: int, display_name: str) -> None:
        # The callback is what actually starts the TCP relay for a discovered port; if it
        # dies silently, the impersonated announcement points at a port nobody listens on.
        # So surface any failure loudly instead of letting the task exception evaporate.
        def _log_failure(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception():
                _LOGGER.error(
                    "on_service failed for %s port %d -- relay for this port may be missing",
                    type_, port, exc_info=task.exception(),
                )

        def _start() -> None:
            asyncio.ensure_future(self._on_service(type_, port, display_name)).add_done_callback(
                _log_failure
            )

        self._loop.call_soon_threadsafe(_start)

    # -- ServiceListener protocol (invoked on zeroconf's own thread) --------------------

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        if self._name_suffix in name:
            return  # this is our own published copy being browsed back; ignore it
        pending_removal = self._pending_removals.pop(name, None)
        if pending_removal:
            pending_removal.cancel()  # real record reappeared -- don't tear ours down
        info = zc.get_service_info(type_, name, timeout=3000)
        if not info or info.port is None:
            return
        if self._speaker_host not in info.parsed_addresses():
            return  # some other device on the network -- not the speaker we're relaying
        fake = impersonated_service_info(
            type_, name, info, self._relay_host, self._name_suffix, self._force_buffered
        )
        try:
            if name in self._published:
                self._zc.update_service(fake)
            else:
                self._zc.register_service(fake)
            self._published[name] = fake
        except Exception:
            _LOGGER.warning("failed to publish impersonated service %s", name, exc_info=True)
            return
        _LOGGER.info(
            "impersonating %s (%s) -> port %d via %s (as %s)",
            name, type_, info.port, self._relay_host, fake.name,
        )
        self._schedule(type_, info.port, relay_display_name(name, type_, self._name_suffix))

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        if name not in self._published:
            return
        # A repeated remove_service for the same name (seen live: the real record can
        # flap several times before settling) must reset the debounce clock, not stack a
        # second timer -- an uncancelled earlier timer would still fire on its own
        # original schedule and unregister the entry out from under a later, still-
        # pending removal, undoing the whole point of debouncing.
        existing = self._pending_removals.get(name)
        if existing:
            existing.cancel()
        timer = threading.Timer(
            _REMOVAL_GRACE_PERIOD_SECONDS, self._finish_removal, args=(name,)
        )
        timer.daemon = True
        self._pending_removals[name] = timer
        timer.start()

    def _finish_removal(self, name: str) -> None:
        self._pending_removals.pop(name, None)
        info = self._published.get(name)
        if info:
            _LOGGER.info("%s did not reappear within grace period -- unpublishing relay copy", name)
            self._unregister(name, info)
