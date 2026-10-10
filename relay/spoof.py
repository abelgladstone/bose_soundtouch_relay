"""Send a UDP datagram with an arbitrary spoofed source address.

Why this exists: a live capture of a Spotify-app AirPlay session (issue #2, see
FINDINGS.md) showed the relay forwarding the client's realtime-audio RTP stream to the
speaker perfectly -- 1:1 packet delivery, sub-millisecond latency, strictly sequential
RTP sequence numbers -- yet the speaker stayed silent while reporting PLAY_STATE. Every
other relayed UDP flow (PTP, other apps' AirPlay timing/control) is sourced from the
relay's own address and works fine, so the working theory is that the speaker's
realtime-audio path validates or derives its SRTP session against the *original*
client's address, which the relay's normal forwarding replaces with its own.

This sends the client-to-speaker leg of a UDP relay with the original client's real
source IP:port preserved, so the speaker sees packets as if they came directly from the
phone. This only works because the relay and speaker share an L2 segment (no routing/ARP
surprises) -- see relay/config.py's single-NIC design note. The speaker-to-client leg is
unaffected (still sourced from the relay's own address, which is what mDNS impersonation
already told the client to expect).
"""

from __future__ import annotations

import socket
import struct


def _ones_complement_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    total = (total & 0xFFFF) + (total >> 16)
    total += total >> 16
    return (~total) & 0xFFFF


def build_spoofed_packet(src: tuple[str, int], dst: tuple[str, int], payload: bytes) -> bytes:
    """Craft a raw IPv4+UDP packet with an arbitrary source address. Split out from
    SpoofedUdpSender.send so packet construction is testable without a raw socket
    (which needs CAP_NET_RAW/root)."""
    src_ip, src_port = src
    dst_ip, dst_port = dst
    udp_len = 8 + len(payload)

    pseudo_header = struct.pack(
        "!4s4sBBH",
        socket.inet_aton(src_ip), socket.inet_aton(dst_ip),
        0, socket.IPPROTO_UDP, udp_len,
    )
    udp_header = struct.pack("!HHHH", src_port, dst_port, udp_len, 0)
    udp_checksum = _ones_complement_checksum(pseudo_header + udp_header + payload)
    udp_header = struct.pack("!HHHH", src_port, dst_port, udp_len, udp_checksum)

    total_len = 20 + udp_len
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45, 0, total_len, 0, 0,
        64, socket.IPPROTO_UDP, 0,
        socket.inet_aton(src_ip), socket.inet_aton(dst_ip),
    )
    ip_checksum = _ones_complement_checksum(ip_header)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45, 0, total_len, 0, 0,
        64, socket.IPPROTO_UDP, ip_checksum,
        socket.inet_aton(src_ip), socket.inet_aton(dst_ip),
    )

    return ip_header + udp_header + payload


class SpoofedUdpSender:
    """One raw socket, shared across every relayed port, that can send a UDP datagram
    claiming to be from any source address -- no per-port socket needed since the
    kernel routes/ARPs purely off the destination address in the crafted IP header."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)

    def send(self, src: tuple[str, int], dst: tuple[str, int], payload: bytes) -> None:
        packet = build_spoofed_packet(src, dst, payload)
        self._sock.sendto(packet, (dst[0], dst[1]))

    def close(self) -> None:
        self._sock.close()
