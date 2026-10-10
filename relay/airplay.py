"""AirPlay-specific relay behavior: keep the relay's own name visible in the client during
connection, by rewriting the device name in the *first* plaintext `/info` response.

AirPlay 2's control channel starts with a plaintext RTSP-style exchange (GET /info) before
pair-verify/pair-setup negotiate an encrypted session. That first response is the one place
a client's "connecting to..." UI reads the device's name from -- everything after it is
opaque, authenticated HAP protocol data that must never be touched, or the pairing's
integrity check fails and the connection breaks outright. So this only ever inspects the
one first message on a connection; `PortRelay` falls back to plain byte-piping immediately
after, whether or not this hook did anything.
"""

from __future__ import annotations

import asyncio
import logging
import plistlib
import re
from typing import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

_CONTENT_LENGTH_RE = re.compile(rb"Content-Length:\s*\d+", re.IGNORECASE)
_FIND_CONTENT_LENGTH_RE = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)
_PLIST_CONTENT_TYPE = b"application/x-apple-binary-plist"

# AirPlay feature bit 30 (RAOP) advertises the legacy realtime/RAOP audio path. This
# speaker plays audio fine over AirPlay 2 *buffered* mode (feature bits 40/41, the path the
# Music app uses) but stays silent over the realtime path the Spotify app selects -- see
# FINDINGS.md issue #2: the transport is proven clean end-to-end and source-IP spoofing was
# ruled out, so the failure is inside the speaker's own encrypted realtime decode/session
# path on EOL (2019) firmware, unreachable from the relay. The realtime-vs-buffered stream
# type is chosen inside the encrypted SETUP the relay can't read or rewrite, and this
# speaker's /info carries no audioFormats to filter -- so the one plaintext capability lever
# left is the feature mask (in /info's `features` int and the mDNS `features`/`ft` TXT
# records). Clearing this bit presents the receiver as buffered-only, nudging senders onto
# the path that works.
RAOP_FEATURE_BIT = 30


def clear_feature_bit(mask: int, bit: int = RAOP_FEATURE_BIT) -> int:
    return mask & ~(1 << bit)


def _parse_int(token: str) -> int:
    token = token.strip()
    try:
        return int(token, 0)  # handles "0x"-prefixed and plain decimal
    except ValueError:
        return int(token, 16)  # bare hex, no prefix


def rewrite_feature_mask_txt(value: bytes) -> bytes:
    """Clear the RAOP bit in an AirPlay feature-mask TXT value, preserving its shape.

    AirPlay TXT feature masks are either a single integer or two comma-separated 32-bit
    halves ("0xLOW,0xHIGH", low word first -- e.g. Zola's "0x445F8A00,0x1C340"). Anything
    that doesn't parse as one of those is returned unchanged."""
    try:
        parts = [p for p in value.decode().split(",") if p.strip()]
        nums = [_parse_int(p) for p in parts]
    except (UnicodeDecodeError, ValueError):
        return value
    if len(nums) == 2:
        mask = clear_feature_bit(nums[0] | (nums[1] << 32))
        return ("0x%X,0x%X" % (mask & 0xFFFFFFFF, mask >> 32)).encode()
    if len(nums) == 1:
        return ("0x%X" % clear_feature_bit(nums[0])).encode()
    return value


# TXT keys carrying the feature mask: `features` on _airplay._tcp, `ft` on _raop._tcp.
_FEATURE_TXT_KEYS = (b"features", b"ft")


def rewrite_feature_properties(properties: dict) -> dict:
    """Return a copy of an mDNS TXT properties dict with the RAOP bit cleared in any
    feature-mask value. Non-AirPlay services simply have neither key and pass through."""
    result = dict(properties)
    for key in _FEATURE_TXT_KEYS:
        if result.get(key):
            result[key] = rewrite_feature_mask_txt(result[key])
    return result


def rewrite_info_response(
    header: bytes, body: bytes, new_name: str, force_buffered: bool = False
) -> bytes:
    """Return `header + body`, with the plist body's "name" replaced (and, when
    `force_buffered`, the RAOP feature bit cleared in `features`) if this looks like a
    /info response. Anything that doesn't match (wrong content type, unparseable body,
    nothing to change) is returned unchanged."""
    if _PLIST_CONTENT_TYPE not in header:
        return header + body
    try:
        info = plistlib.loads(body)
    except Exception:
        return header + body
    if not isinstance(info, dict):
        return header + body
    changed = False
    if "name" in info:
        info["name"] = new_name
        changed = True
    if force_buffered and isinstance(info.get("features"), int):
        reduced = clear_feature_bit(info["features"])
        if reduced != info["features"]:
            info["features"] = reduced
            changed = True
    if not changed:
        return header + body
    new_body = plistlib.dumps(info, fmt=plistlib.FMT_BINARY)
    new_header = _CONTENT_LENGTH_RE.sub(f"Content-Length: {len(new_body)}".encode(), header, count=1)
    return new_header + new_body


def name_rewriter(
    new_name: str, force_buffered: bool = False
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    """Build a `PortRelay(on_first_server_message=...)` hook that rewrites the first /info
    response's device name (and, when `force_buffered`, clears its RAOP feature bit)."""

    async def handle_first_message(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        header = await reader.readuntil(b"\r\n\r\n")
        length_match = _FIND_CONTENT_LENGTH_RE.search(header)
        body = await reader.readexactly(int(length_match.group(1))) if length_match else b""
        writer.write(rewrite_info_response(header, body, new_name, force_buffered))
        await writer.drain()

    return handle_first_message
