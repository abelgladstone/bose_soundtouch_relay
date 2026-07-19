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


def rewrite_info_response(header: bytes, body: bytes, new_name: str) -> bytes:
    """Return `header + body`, with the plist body's "name" replaced if this looks like
    a /info response. Anything that doesn't match (wrong content type, unparseable body,
    no "name" key) is returned unchanged."""
    if _PLIST_CONTENT_TYPE not in header:
        return header + body
    try:
        info = plistlib.loads(body)
    except Exception:
        return header + body
    if not isinstance(info, dict) or "name" not in info:
        return header + body
    info["name"] = new_name
    new_body = plistlib.dumps(info, fmt=plistlib.FMT_BINARY)
    new_header = _CONTENT_LENGTH_RE.sub(f"Content-Length: {len(new_body)}".encode(), header, count=1)
    return new_header + new_body


def name_rewriter(
    new_name: str,
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    """Build a `PortRelay(on_first_server_message=...)` hook bound to `new_name`."""

    async def handle_first_message(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        header = await reader.readuntil(b"\r\n\r\n")
        length_match = _FIND_CONTENT_LENGTH_RE.search(header)
        body = await reader.readexactly(int(length_match.group(1))) if length_match else b""
        writer.write(rewrite_info_response(header, body, new_name))
        await writer.drain()

    return handle_first_message
