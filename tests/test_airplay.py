import asyncio
import plistlib
import unittest

from relay.airplay import name_rewriter, rewrite_info_response


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
