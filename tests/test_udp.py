import asyncio
import socket
import unittest

from relay.proxy import RelayManager
from relay.udp import UdpPortRelay


def _udp_socket(host: str, port: int = 0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    sock.setblocking(False)
    return sock


async def _recvfrom(sock: socket.socket) -> tuple[bytes, tuple[str, int]]:
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.sock_recvfrom(sock, 4096), timeout=2)


class UdpPortRelayTest(unittest.IsolatedAsyncioTestCase):
    """The relay binds 127.0.0.1; the fake speaker lives on 127.0.0.2 (same loopback
    trick as RelayManagerTest) so speaker- and client-originated datagrams are
    distinguishable by address, as in the real deployment."""

    async def asyncSetUp(self):
        # Real PTP is port-symmetric (319<->319); mirror that by giving the fake
        # speaker the same port number as the relay, on the other loopback address.
        self.speaker = _udp_socket("127.0.0.2")
        self.port = self.speaker.getsockname()[1]
        self.relay = UdpPortRelay("127.0.0.1", self.port, "127.0.0.2")
        await self.relay.start()
        self.client = _udp_socket("127.0.0.1")

    async def asyncTearDown(self):
        await self.relay.stop()
        self.speaker.close()
        self.client.close()

    async def test_client_datagram_forwarded_to_speaker(self):
        await asyncio.get_running_loop().sock_sendto(
            self.client, b"sync", ("127.0.0.1", self.port)
        )
        data, addr = await _recvfrom(self.speaker)
        self.assertEqual(data, b"sync")
        # Forwarded from the relay's own well-known port, as PTP peers expect.
        self.assertEqual(addr, ("127.0.0.1", self.port))

    async def test_speaker_datagram_returned_to_last_client(self):
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(self.client, b"sync", ("127.0.0.1", self.port))
        await _recvfrom(self.speaker)

        await loop.sock_sendto(self.speaker, b"delay_resp", ("127.0.0.1", self.port))
        data, addr = await _recvfrom(self.client)
        self.assertEqual(data, b"delay_resp")
        self.assertEqual(addr, ("127.0.0.1", self.port))

    async def test_speaker_datagram_before_any_client_is_dropped(self):
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(self.speaker, b"announce", ("127.0.0.1", self.port))
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(loop.sock_recvfrom(self.client, 4096), timeout=0.2)

    async def test_newest_client_wins(self):
        loop = asyncio.get_running_loop()
        old_client = self.client
        new_client = _udp_socket("127.0.0.1")
        try:
            await loop.sock_sendto(old_client, b"a", ("127.0.0.1", self.port))
            await _recvfrom(self.speaker)
            await loop.sock_sendto(new_client, b"b", ("127.0.0.1", self.port))
            await _recvfrom(self.speaker)

            await loop.sock_sendto(self.speaker, b"reply", ("127.0.0.1", self.port))
            data, _ = await _recvfrom(new_client)
            self.assertEqual(data, b"reply")
        finally:
            new_client.close()


class RelayManagerUdpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = RelayManager("127.0.0.1", "127.0.0.2")
        # Grab a free UDP port number for the manager to bind.
        probe = _udp_socket("127.0.0.1")
        self.port = probe.getsockname()[1]
        probe.close()

    async def asyncTearDown(self):
        await self.manager.stop_all()

    async def test_ensure_udp_is_idempotent(self):
        await self.manager.ensure_udp(self.port)
        await self.manager.ensure_udp(self.port)
        self.assertEqual(len(self.manager._udp_relays), 1)

    async def test_stop_all_covers_udp_relays(self):
        await self.manager.ensure_udp(self.port)
        await self.manager.stop_all()
        self.assertEqual(self.manager._udp_relays, {})
        # Port is free again -- rebinding proves the socket was really closed.
        sock = _udp_socket("127.0.0.1", self.port)
        sock.close()


if __name__ == "__main__":
    unittest.main()
