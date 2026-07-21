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
from typing import Awaitable, Callable

from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

_LOGGER = logging.getLogger(__name__)

# (service type, port, the relay's display name for this service) -> None
OnService = Callable[[str, int, str], Awaitable[None]]


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
    type_: str, name: str, info: ServiceInfo, relay_host: str, name_suffix: str
) -> ServiceInfo:
    """Build a ServiceInfo for the same service, addressed at the relay instead of the speaker."""
    new_name = relay_instance_name(name, type_, name_suffix)
    server = f"{_slugify(new_name)}.local."
    return ServiceInfo(
        type_,
        new_name,
        addresses=[socket.inet_aton(relay_host)],
        port=info.port,
        properties=dict(info.properties or {}),
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
    ):
        self._service_types = service_types
        self._speaker_host = speaker_host
        self._relay_host = relay_host
        self._name_suffix = name_suffix
        self._on_service = on_service
        self._loop = loop
        self._zc: Zeroconf | None = None
        self._browsers: list[ServiceBrowser] = []
        self._published: dict[str, ServiceInfo] = {}

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
        info = zc.get_service_info(type_, name, timeout=3000)
        if not info or info.port is None:
            return
        if self._speaker_host not in info.parsed_addresses():
            return  # some other device on the network -- not the speaker we're relaying
        fake = impersonated_service_info(type_, name, info, self._relay_host, self._name_suffix)
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
        info = self._published.get(name)
        if info:
            self._unregister(name, info)
