import asyncio
import socket
import time
import unittest
from unittest.mock import MagicMock

from zeroconf import ServiceInfo

from relay.mdns import ImpersonationBridge, impersonated_service_info, relay_instance_name


class RelayInstanceNameTest(unittest.TestCase):
    def test_inserts_suffix_before_service_type(self):
        name = relay_instance_name(
            "Zola._soundtouch._tcp.local.", "_soundtouch._tcp.local.", " (relay)"
        )
        self.assertEqual(name, "Zola (relay)._soundtouch._tcp.local.")


class ImpersonatedServiceInfoTest(unittest.TestCase):
    def test_rewrites_address_keeps_port_and_properties(self):
        real = ServiceInfo(
            "_soundtouch._tcp.local.",
            "Zola._soundtouch._tcp.local.",
            addresses=[socket.inet_aton("192.0.2.20")],
            port=8090,
            properties={
                b"DESCRIPTION": b"SoundTouch",
                b"MAC": b"AABBCCDDEEFF",
                b"MANUFACTURER": b"Bose Corporation",
            },
            server="zola.local.",
        )

        fake = impersonated_service_info(
            "_soundtouch._tcp.local.", "Zola._soundtouch._tcp.local.", real, "192.0.2.10", " (relay)"
        )

        self.assertEqual(fake.parsed_addresses(), ["192.0.2.10"])
        self.assertEqual(fake.port, 8090)
        self.assertEqual(fake.properties[b"MANUFACTURER"], b"Bose Corporation")
        self.assertEqual(fake.name, "Zola (relay)._soundtouch._tcp.local.")
        self.assertEqual(fake.type, real.type)
        self.assertNotEqual(fake.server, real.server)  # distinct hostname avoids A-record conflicts

    def test_force_buffered_clears_raop_feature_bit_in_txt(self):
        real = ServiceInfo(
            "_airplay._tcp.local.",
            "Zola._airplay._tcp.local.",
            addresses=[socket.inet_aton("192.0.2.20")],
            port=7000,
            properties={b"features": b"0x445F8A00,0x1C340", b"deviceid": b"AA:BB:CC:DD:EE:FF"},
        )
        fake = impersonated_service_info(
            "_airplay._tcp.local.", "Zola._airplay._tcp.local.", real, "192.0.2.10",
            " (relay)", force_buffered=True,
        )
        self.assertEqual(fake.properties[b"features"], b"0x45F8A00,0x1C340")
        self.assertEqual(fake.properties[b"deviceid"], b"AA:BB:CC:DD:EE:FF")  # untouched
        self.assertEqual(real.properties[b"features"], b"0x445F8A00,0x1C340")  # original intact

    def test_force_buffered_off_keeps_features_verbatim(self):
        real = ServiceInfo(
            "_airplay._tcp.local.",
            "Zola._airplay._tcp.local.",
            addresses=[socket.inet_aton("192.0.2.20")],
            port=7000,
            properties={b"features": b"0x445F8A00,0x1C340"},
        )
        fake = impersonated_service_info(
            "_airplay._tcp.local.", "Zola._airplay._tcp.local.", real, "192.0.2.10", " (relay)"
        )
        self.assertEqual(fake.properties[b"features"], b"0x445F8A00,0x1C340")

    def test_original_properties_not_mutated_by_reference(self):
        real = ServiceInfo(
            "_spotify-connect._tcp.local.",
            "Zola._spotify-connect._tcp.local.",
            addresses=[socket.inet_aton("192.0.2.20")],
            port=8200,
            properties={b"CPath": b"/zc", b"VERSION": b"1.0"},
        )
        fake = impersonated_service_info(
            "_spotify-connect._tcp.local.", "Zola._spotify-connect._tcp.local.", real, "192.0.2.10", " (relay)"
        )
        fake.properties[b"CPath"] = b"/mutated"
        self.assertEqual(real.properties[b"CPath"], b"/zc")


class DebouncedRemovalTest(unittest.IsolatedAsyncioTestCase):
    """remove_service must not tear down the impersonated copy immediately -- see
    relay/mdns.py's _REMOVAL_GRACE_PERIOD_SECONDS comment: embedded mDNS responders often
    do a quick remove-then-re-add on a state change, and an immediate unregister would
    flash "Zola (relay)" out of every client's cache for however long the re-add takes."""

    async def asyncSetUp(self):
        self.zc = MagicMock()
        self.zc.get_service_info.return_value = None  # keep add_service a no-op past cancellation
        self.bridge = ImpersonationBridge(
            service_types=["_airplay._tcp.local."],
            speaker_host="192.0.2.20",
            relay_host="192.0.2.10",
            on_service=self._noop_on_service,
            loop=asyncio.get_running_loop(),
        )
        self.bridge._zc = self.zc
        self.name = "Zola._airplay._tcp.local."
        self.bridge._published[self.name] = MagicMock()

    async def asyncTearDown(self):
        for timer in self.bridge._pending_removals.values():
            timer.cancel()

    @staticmethod
    async def _noop_on_service(type_, port, display_name):
        pass

    async def test_removal_is_not_immediate(self):
        self.bridge.remove_service(self.zc, "_airplay._tcp.local.", self.name)
        self.assertIn(self.name, self.bridge._pending_removals)
        self.zc.unregister_service.assert_not_called()

    async def test_reappearance_within_grace_period_cancels_removal(self):
        self.bridge.remove_service(self.zc, "_airplay._tcp.local.", self.name)
        self.bridge.add_service(self.zc, "_airplay._tcp.local.", self.name)
        self.assertNotIn(self.name, self.bridge._pending_removals)

        await asyncio.sleep(0.05)  # past when a non-debounced removal would fire
        self.zc.unregister_service.assert_not_called()
        self.assertIn(self.name, self.bridge._published)  # never actually torn down

    async def test_repeated_removal_resets_the_debounce_clock(self):
        # Regression test: a real capture showed the speaker's mDNS record flapping
        # several times in a row during a source change. An earlier version of this fix
        # let each remove_service call start a new timer without cancelling the previous
        # one, so an orphaned first timer could still fire and unregister the entry out
        # from under a second, still-pending removal -- defeating the debounce entirely.
        with unittest.mock.patch("relay.mdns._REMOVAL_GRACE_PERIOD_SECONDS", 0.06):
            self.bridge.remove_service(self.zc, "_airplay._tcp.local.", self.name)
            await asyncio.sleep(0.04)
            self.bridge.remove_service(self.zc, "_airplay._tcp.local.", self.name)  # flap again

            await asyncio.sleep(0.04)  # past the first call's original 0.06s deadline
            self.zc.unregister_service.assert_not_called()
            self.assertIn(self.name, self.bridge._published)

            deadline = time.time() + 2
            while self.name in self.bridge._published and time.time() < deadline:
                await asyncio.sleep(0.01)
        self.zc.unregister_service.assert_called_once()

    async def test_removal_completes_if_nothing_reappears(self):
        with unittest.mock.patch("relay.mdns._REMOVAL_GRACE_PERIOD_SECONDS", 0.05):
            self.bridge.remove_service(self.zc, "_airplay._tcp.local.", self.name)
            deadline = time.time() + 2
            while self.name in self.bridge._published and time.time() < deadline:
                await asyncio.sleep(0.01)

        self.zc.unregister_service.assert_called_once()
        self.assertNotIn(self.name, self.bridge._published)
        self.assertNotIn(self.name, self.bridge._pending_removals)


if __name__ == "__main__":
    unittest.main()
