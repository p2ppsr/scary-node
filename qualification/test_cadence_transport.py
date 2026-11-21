import hashlib
import io
import unittest

from cadence_transport import CHUNK, MAGIC, read_message


class PositiveBlockFraming(unittest.TestCase):
    def test_streamed_body_must_match_the_complete_fixture_and_checksum(self):
        block = b"R" * (CHUNK + 17)
        digest = hashlib.sha256(block)
        checksum = hashlib.sha256(digest.digest()).digest()[:4]
        header = MAGIC + b"block".ljust(12, b"\0") + len(block).to_bytes(4, "little")
        message = header + checksum + block
        self.assertEqual(
            read_message(io.BytesIO(message), (len(block), digest.hexdigest())),
            (b"block", digest.hexdigest()),
        )
        with self.assertRaisesRegex(ValueError, "readback differs"):
            read_message(io.BytesIO(message), (len(block), "0" * 64))
        with self.assertRaisesRegex(ValueError, "checksum"):
            read_message(
                io.BytesIO(header + bytes(4) + block), (len(block), digest.hexdigest())
            )
        with self.assertRaises(EOFError):
            read_message(io.BytesIO(message[:-1]), (len(block), digest.hexdigest()))
