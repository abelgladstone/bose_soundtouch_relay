import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relay.config import Config


TOML = """
[speaker]
host = "192.0.2.20"
extra_ports = [8080, 9000]

[relay]
host = "192.0.2.10"
"""


class ConfigTest(unittest.TestCase):
    def test_loads_from_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(TOML)
            config = Config.load(path)
        self.assertEqual(config.speaker_host, "192.0.2.20")
        self.assertEqual(config.relay_host, "192.0.2.10")
        self.assertEqual(config.extra_ports, [8080, 9000])
        self.assertIn("_soundtouch._tcp.local.", config.service_types)

    def test_env_vars_override_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(TOML)
            with mock.patch.dict(
                "os.environ",
                {"SPEAKER_HOST": "192.0.2.21", "RELAY_HOST": "192.0.2.99"},
                clear=False,
            ):
                config = Config.load(path)
        self.assertEqual(config.speaker_host, "192.0.2.21")
        self.assertEqual(config.relay_host, "192.0.2.99")

    def test_missing_required_field_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[speaker]\nhost = \"192.0.2.20\"\n")
            with mock.patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(RuntimeError):
                    Config.load(path)


if __name__ == "__main__":
    unittest.main()
