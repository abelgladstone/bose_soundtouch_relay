import asyncio
import unittest

from relay.app import make_on_service
from relay.mdns import ImpersonationBridge


class _RecordingManager:
    """Stands in for RelayManager.ensure, recording each (port, hook) it was asked for."""

    def __init__(self):
        self.calls = []

    async def ensure(self, port, on_first_server_message=None):
        self.calls.append((port, on_first_server_message))


class MakeOnServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_airplay_service_gets_name_rewrite_hook(self):
        manager = _RecordingManager()
        await make_on_service(manager)("_airplay._tcp.local.", 7000, "Zola (relay)")
        (port, hook), = manager.calls
        self.assertEqual(port, 7000)
        self.assertIsNotNone(hook)

    async def test_raop_service_gets_plain_relay(self):
        manager = _RecordingManager()
        await make_on_service(manager)("_raop._tcp.local.", 7000, "AABBCCDDEEFF@Zola (relay)")
        (port, hook), = manager.calls
        self.assertEqual(port, 7000)
        self.assertIsNone(hook)

    async def test_other_services_get_plain_relay(self):
        manager = _RecordingManager()
        on_service = make_on_service(manager)
        await on_service("_soundtouch._tcp.local.", 8090, "Zola (relay)")
        await on_service("_spotify-connect._tcp.local.", 8200, "Zola (relay)")
        self.assertEqual(manager.calls, [(8090, None), (8200, None)])


class ScheduleFailureLoggingTest(unittest.IsolatedAsyncioTestCase):
    async def test_on_service_exception_is_logged(self):
        async def broken_on_service(type_, port, display_name):
            raise RuntimeError("boom")

        bridge = ImpersonationBridge(
            service_types=[],
            speaker_host="192.0.2.20",
            relay_host="192.0.2.10",
            on_service=broken_on_service,
            loop=asyncio.get_running_loop(),
        )
        with self.assertLogs("relay.mdns", level="ERROR") as captured:
            bridge._schedule("_airplay._tcp.local.", 7000, "Zola (relay)")
            await asyncio.sleep(0.05)
        self.assertIn("port 7000", captured.output[0])


if __name__ == "__main__":
    unittest.main()
