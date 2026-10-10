import socket
import struct
import unittest

from relay.spoof import build_spoofed_packet


def _parse(packet: bytes):
    ver_ihl, tos, total_len, ident, flags_frag, ttl, proto, ip_checksum, src, dst = (
        struct.unpack("!BBHHHBBH4s4s", packet[:20])
    )
    src_port, dst_port, udp_len, udp_checksum = struct.unpack("!HHHH", packet[20:28])
    payload = packet[28:]
    return {
        "version": ver_ihl >> 4,
        "ihl": ver_ihl & 0x0F,
        "total_len": total_len,
        "ttl": ttl,
        "proto": proto,
        "ip_checksum": ip_checksum,
        "src_ip": socket.inet_ntoa(src),
        "dst_ip": socket.inet_ntoa(dst),
        "src_port": src_port,
        "dst_port": dst_port,
        "udp_len": udp_len,
        "udp_checksum": udp_checksum,
        "payload": payload,
    }


def _ip_checksum_ok(packet: bytes) -> bool:
    header = packet[:20]
    total = sum(struct.unpack("!10H", header))
    total = (total & 0xFFFF) + (total >> 16)
    total += total >> 16
    return (~total) & 0xFFFF == 0


class BuildSpoofedPacketTest(unittest.TestCase):
    def test_fields_match_requested_addresses(self):
        packet = build_spoofed_packet(("198.51.100.37", 65287), ("192.0.2.20", 49123), b"payload")
        parsed = _parse(packet)
        self.assertEqual(parsed["version"], 4)
        self.assertEqual(parsed["ihl"], 5)
        self.assertEqual(parsed["proto"], socket.IPPROTO_UDP)
        self.assertEqual(parsed["src_ip"], "198.51.100.37")
        self.assertEqual(parsed["dst_ip"], "192.0.2.20")
        self.assertEqual(parsed["src_port"], 65287)
        self.assertEqual(parsed["dst_port"], 49123)
        self.assertEqual(parsed["payload"], b"payload")
        self.assertEqual(parsed["total_len"], 20 + 8 + len(b"payload"))
        self.assertEqual(parsed["udp_len"], 8 + len(b"payload"))

    def test_ip_checksum_is_valid(self):
        packet = build_spoofed_packet(("198.51.100.37", 65287), ("192.0.2.20", 49123), b"payload")
        self.assertTrue(_ip_checksum_ok(packet))

    def test_udp_checksum_is_nonzero_and_deterministic(self):
        packet1 = build_spoofed_packet(("198.51.100.37", 65287), ("192.0.2.20", 49123), b"payload")
        packet2 = build_spoofed_packet(("198.51.100.37", 65287), ("192.0.2.20", 49123), b"payload")
        self.assertEqual(_parse(packet1)["udp_checksum"], _parse(packet2)["udp_checksum"])
        self.assertNotEqual(_parse(packet1)["udp_checksum"], 0)

    def test_odd_length_payload_is_handled(self):
        packet = build_spoofed_packet(("198.51.100.37", 65287), ("192.0.2.20", 49123), b"odd")
        parsed = _parse(packet)
        self.assertEqual(parsed["payload"], b"odd")


if __name__ == "__main__":
    unittest.main()
