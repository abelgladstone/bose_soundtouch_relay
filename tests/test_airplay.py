import asyncio
import plistlib
import unittest

from relay.airplay import (
    RAOP_FEATURE_BIT,
    clear_feature_bit,
    name_rewriter,
    rewrite_feature_mask_txt,
    rewrite_feature_properties,
    rewrite_info_response,
)

# Zola's real advertised feature mask (from a live /info fetch and its mDNS TXT records):
# low word 0x445F8A00 has the RAOP bit (30 = 0x40000000) set; clearing it yields 0x045F8A00.
_ZOLA_FEATURES = 0x1C340445F8A00
_ZOLA_FEATURES_NO_RAOP = 0x1C340045F8A00


def _plist_response(cseq: int, body_dict: dict) -> bytes:
    body = plistlib.dumps(body_dict, fmt=plistlib.FMT_BINARY)
    header = (
        b"RTSP/1.0 200 OK\r\n"
        b"Content-Type: application/x-apple-binary-plist\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"CSeq: " + str(cseq).encode() + b"\r\n\r\n"
    )
    return header + body


class RewriteInfoResponseTest(unittest.TestCase):
    def test_rewrites_name_and_content_length(self):
        response = _plist_response(0, {"name": "Zola", "model": "SoundTouch 300"})
        header, body = response.split(b"\r\n\r\n", 1)
        rewritten = rewrite_info_response(header + b"\r\n\r\n", body, "Zola (relay)")

        new_header, new_body = rewritten.split(b"\r\n\r\n", 1)
        info = plistlib.loads(new_body)
        self.assertEqual(info["name"], "Zola (relay)")
        self.assertEqual(info["model"], "SoundTouch 300")  # untouched
        self.assertIn(f"Content-Length: {len(new_body)}".encode(), new_header)

    def test_leaves_non_plist_response_unchanged(self):
        header = b"RTSP/1.0 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n"
        body = b"\x01\x02\x03opaque-crypto-bytes"
        self.assertEqual(rewrite_info_response(header, body, "Zola (relay)"), header + body)

    def test_leaves_unparseable_body_unchanged(self):
        header = b"RTSP/1.0 200 OK\r\nContent-Type: application/x-apple-binary-plist\r\n\r\n"
        body = b"not actually a plist"
        self.assertEqual(rewrite_info_response(header, body, "Zola (relay)"), header + body)

    def test_leaves_plist_without_name_key_unchanged(self):
        response = _plist_response(0, {"model": "SoundTouch 300"})
        header, body = response.split(b"\r\n\r\n", 1)
        header += b"\r\n\r\n"
        self.assertEqual(rewrite_info_response(header, body, "Zola (relay)"), header + body)


class ClearFeatureBitTest(unittest.TestCase):
    def test_clears_raop_bit_leaving_others(self):
        self.assertEqual(clear_feature_bit(_ZOLA_FEATURES), _ZOLA_FEATURES_NO_RAOP)
        # bit 30 gone, buffered/PTP bits 40/41 untouched
        self.assertEqual(clear_feature_bit(_ZOLA_FEATURES) >> RAOP_FEATURE_BIT & 1, 0)
        self.assertEqual(clear_feature_bit(_ZOLA_FEATURES) >> 40 & 1, 1)
        self.assertEqual(clear_feature_bit(_ZOLA_FEATURES) >> 41 & 1, 1)

    def test_already_clear_is_noop(self):
        self.assertEqual(clear_feature_bit(_ZOLA_FEATURES_NO_RAOP), _ZOLA_FEATURES_NO_RAOP)


class RewriteFeatureMaskTxtTest(unittest.TestCase):
    def test_two_part_low_high_form(self):
        self.assertEqual(rewrite_feature_mask_txt(b"0x445F8A00,0x1C340"), b"0x45F8A00,0x1C340")

    def test_single_integer_form(self):
        self.assertEqual(rewrite_feature_mask_txt(b"0x445F8A00"), b"0x45F8A00")

    def test_unparseable_returned_unchanged(self):
        self.assertEqual(rewrite_feature_mask_txt(b"not-a-mask"), b"not-a-mask")


class RewriteFeaturePropertiesTest(unittest.TestCase):
    def test_rewrites_airplay_features_and_raop_ft(self):
        props = rewrite_feature_properties({b"features": b"0x445F8A00,0x1C340"})
        self.assertEqual(props[b"features"], b"0x45F8A00,0x1C340")
        props = rewrite_feature_properties({b"ft": b"0x445F8A00,0x1C340"})
        self.assertEqual(props[b"ft"], b"0x45F8A00,0x1C340")

    def test_leaves_non_feature_properties_untouched(self):
        original = {b"CPath": b"/zc", b"VERSION": b"1.0"}
        self.assertEqual(rewrite_feature_properties(original), original)

    def test_does_not_mutate_input(self):
        original = {b"features": b"0x445F8A00,0x1C340"}
        rewrite_feature_properties(original)
        self.assertEqual(original[b"features"], b"0x445F8A00,0x1C340")


class RewriteInfoForceBufferedTest(unittest.TestCase):
    def _info_response(self, body_dict):
        body = plistlib.dumps(body_dict, fmt=plistlib.FMT_BINARY)
        header = (
            b"RTSP/1.0 200 OK\r\n"
            b"Content-Type: application/x-apple-binary-plist\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
        )
        return header, body

    def test_clears_features_bit_and_rewrites_name(self):
        header, body = self._info_response({"name": "Zola", "features": _ZOLA_FEATURES})
        rewritten = rewrite_info_response(header, body, "Zola (relay)", force_buffered=True)
        new_header, new_body = rewritten.split(b"\r\n\r\n", 1)
        info = plistlib.loads(new_body)
        self.assertEqual(info["name"], "Zola (relay)")
        self.assertEqual(info["features"], _ZOLA_FEATURES_NO_RAOP)
        self.assertIn(f"Content-Length: {len(new_body)}".encode(), new_header)

    def test_features_untouched_when_force_buffered_off(self):
        header, body = self._info_response({"name": "Zola", "features": _ZOLA_FEATURES})
        rewritten = rewrite_info_response(header, body, "Zola (relay)", force_buffered=False)
        _, new_body = rewritten.split(b"\r\n\r\n", 1)
        self.assertEqual(plistlib.loads(new_body)["features"], _ZOLA_FEATURES)

    def test_no_name_but_force_buffered_still_clears_features(self):
        header, body = self._info_response({"features": _ZOLA_FEATURES})
        rewritten = rewrite_info_response(header, body, "Zola (relay)", force_buffered=True)
        _, new_body = rewritten.split(b"\r\n\r\n", 1)
        self.assertEqual(plistlib.loads(new_body)["features"], _ZOLA_FEATURES_NO_RAOP)


class NameRewriterHookTest(unittest.IsolatedAsyncioTestCase):
    async def test_hook_rewrites_first_message_end_to_end(self):
        response = _plist_response(0, {"name": "Zola"})

        server_reader = asyncio.StreamReader()
        server_reader.feed_data(response)
        server_reader.feed_eof()

        received = bytearray()

        class FakeWriter:
            def write(self, data: bytes) -> None:
                received.extend(data)

            async def drain(self) -> None:
                pass

        hook = name_rewriter("Zola (relay)")
        await hook(server_reader, FakeWriter())

        _, body = bytes(received).split(b"\r\n\r\n", 1)
        self.assertEqual(plistlib.loads(body)["name"], "Zola (relay)")


if __name__ == "__main__":
    unittest.main()
