"""Relay configuration.

Single-NIC design: the relay needs only one address, on the speaker's own VLAN/subnet --
see relay/mdns.py for why a second interface isn't required.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"

# Port 8090 (SoundTouch API) and 8200 (Spotify Connect Zeroconf, path /zc) are always
# discovered via mDNS TXT records, so they don't need to be listed here. Port 8080 (the
# "gabbo" push-notification WebSocket) rides alongside the API on the same host but isn't
# itself mDNS-advertised as a separate service, so it must be relayed unconditionally.
DEFAULT_SERVICE_TYPES = [
    "_soundtouch._tcp.local.",
    "_spotify-connect._tcp.local.",
    "_airplay._tcp.local.",
    "_raop._tcp.local.",
]
DEFAULT_EXTRA_PORTS = [8080]
DEFAULT_NAME_SUFFIX = " (relay)"


@dataclass
class Config:
    speaker_host: str
    relay_host: str
    service_types: list[str] = field(default_factory=lambda: list(DEFAULT_SERVICE_TYPES))
    extra_ports: list[int] = field(default_factory=lambda: list(DEFAULT_EXTRA_PORTS))
    name_suffix: str = DEFAULT_NAME_SUFFIX

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        data = tomllib.loads(path.read_text()) if path.exists() else {}
        speaker = data.get("speaker", {})
        relay = data.get("relay", {})

        # Env vars win over config.toml so the container is configurable without a rebuild,
        # matching bose_st300's BOSE_HOST convention.
        speaker_host = os.environ.get("SPEAKER_HOST") or speaker.get("host")
        relay_host = os.environ.get("RELAY_HOST") or relay.get("host")

        missing = [
            name
            for name, value in (
                ("speaker.host / SPEAKER_HOST", speaker_host),
                ("relay.host / RELAY_HOST", relay_host),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"missing required config: {', '.join(missing)}")

        return cls(
            speaker_host=speaker_host,
            relay_host=relay_host,
            service_types=speaker.get("service_types", list(DEFAULT_SERVICE_TYPES)),
            extra_ports=speaker.get("extra_ports", list(DEFAULT_EXTRA_PORTS)),
            name_suffix=relay.get("name_suffix", DEFAULT_NAME_SUFFIX),
        )
