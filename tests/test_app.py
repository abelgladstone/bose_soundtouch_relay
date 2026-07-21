import asyncio
import unittest

from relay.app import ensure_airplay_event_ports, ensure_airplay_ptp_ports, make_on_service
from relay.mdns import ImpersonationBridge


class _RecordingManager:
    """Stands in for RelayManager, recording each ensure/ensure_udp it was asked for."""

    def __init__(self):
        self.calls = []
        self.udp_calls = []

    async def ensure(self, port, on_first_server_message=None):
        self.calls.append((port, on_first_server_message))

    async def ensure_udp(self, port):
        self.udp_calls.append(port)


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


class EnsureAirplayEventPortsTest(unittest.IsolatedAsyncioTestCase):
    async def test_ensures_every_port_in_range_tcp_and_udp(self):
        manager = _RecordingManager()
        await ensure_airplay_event_ports(manager, range(40000, 40005))
        self.assertEqual([call[0] for call in manager.calls], [40000, 40001, 40002, 40003, 40004])
        self.assertEqual(manager.udp_calls, [40000, 40001, 40002, 40003, 40004])

    async def test_one_bad_port_does_not_stop_the_rest(self):
        manager = _RecordingManager()

        async def ensure(port, on_first_server_message=None):
            if port == 40002:
                raise OSError("address already in use")
            manager.calls.append((port, on_first_server_message))

        manager.ensure = ensure
        with self.assertLogs("relay.app", level="WARNING"):
            await ensure_airplay_event_ports(manager, range(40000, 40005))
        self.assertEqual([call[0] for call in manager.calls], [40000, 40001, 40003, 40004])
        # The UDP side of the failed TCP port is still attempted.
        self.assertEqual(manager.udp_calls, [40000, 40001, 40002, 40003, 40004])


class EnsureAirplayPtpPortsTest(unittest.IsolatedAsyncioTestCase):
    async def test_ensures_both_ptp_ports(self):
        manager = _RecordingManager()
        await ensure_airplay_ptp_ports(manager)
        self.assertEqual(manager.udp_calls, [319, 320])

    async def test_one_bad_port_does_not_stop_the_rest(self):
        manager = _RecordingManager()

        async def ensure_udp(port):
            if port == 319:
                raise OSError("address already in use")
            manager.udp_calls.append(port)

        manager.ensure_udp = ensure_udp
        with self.assertLogs("relay.app", level="ERROR"):
            await ensure_airplay_ptp_ports(manager)
        self.assertEqual(manager.udp_calls, [320])


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
