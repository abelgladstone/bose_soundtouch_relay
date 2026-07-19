import asyncio
import unittest

from relay.proxy import PortRelay, RelayManager


async def _echo_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


class PortRelayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.echo_server = await asyncio.start_server(_echo_handler, "127.0.0.1", 0)
        self.echo_port = self.echo_server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.echo_server.close()
        await self.echo_server.wait_closed()

    async def test_relays_bytes_both_ways(self):
        relay = PortRelay("127.0.0.1", 0, "127.0.0.1", self.echo_port)
        await relay.start()
        relay_port = relay._server.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"hello relay")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(1024), timeout=2)
        self.assertEqual(data, b"hello relay")

        writer.close()
        await relay.stop()

    async def test_unreachable_target_closes_client(self):
        # Bind and release a port so we know nothing listens there anymore.
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()

        relay = PortRelay("127.0.0.1", 0, "127.0.0.1", dead_port)
        await relay.start()
        relay_port = relay._server.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        data = await asyncio.wait_for(reader.read(1024), timeout=2)
        self.assertEqual(data, b"")

        writer.close()
        await relay.stop()


class RelayManagerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Manager binds and targets the *same* port number (mirroring the real deployment,
        # where the relay's VLAN1 port and the speaker's VLAN2 port match), so the echo
        # server needs to live on a different loopback address to avoid a bind collision.
        self.echo_server = await asyncio.start_server(_echo_handler, "127.0.0.2", 0)
        self.echo_port = self.echo_server.sockets[0].getsockname()[1]
        self.manager = RelayManager("127.0.0.1", "127.0.0.2")

    async def asyncTearDown(self):
        await self.manager.stop_all()
        self.echo_server.close()
        await self.echo_server.wait_closed()

    async def test_ensure_is_idempotent(self):
        await self.manager.ensure(self.echo_port)
        await self.manager.ensure(self.echo_port)
        self.assertEqual(len(self.manager._relays), 1)

    async def test_ensure_then_remove(self):
        await self.manager.ensure(self.echo_port)
        self.assertIn(self.echo_port, self.manager._relays)
        await self.manager.remove(self.echo_port)
        self.assertNotIn(self.echo_port, self.manager._relays)

    async def test_ensure_upgrades_plain_relay_to_hooked(self):
        await self.manager.ensure(self.echo_port)
        self.assertFalse(self.manager._relays[self.echo_port].has_hook)

        async def hook(reader, writer):
            pass

        await self.manager.ensure(self.echo_port, on_first_server_message=hook)
        self.assertTrue(self.manager._relays[self.echo_port].has_hook)
        self.assertEqual(len(self.manager._relays), 1)

    async def test_ensure_does_not_downgrade_hooked_relay(self):
        async def hook(reader, writer):
            pass

        await self.manager.ensure(self.echo_port, on_first_server_message=hook)
        await self.manager.ensure(self.echo_port)  # no hook this time
        self.assertTrue(self.manager._relays[self.echo_port].has_hook)


if __name__ == "__main__":
    unittest.main()
