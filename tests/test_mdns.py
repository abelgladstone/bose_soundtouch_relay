import socket
import unittest

from zeroconf import ServiceInfo

from relay.mdns import impersonated_service_info, relay_instance_name


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


if __name__ == "__main__":
    unittest.main()
