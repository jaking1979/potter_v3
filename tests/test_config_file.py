import os
import tempfile
import unittest
from pathlib import Path

from model_broker.config_file import read_candidates_file
from model_broker.config_json import MAX_CONFIG_BYTES


class FileConfigTests(unittest.TestCase):
    def test_regular_file_round_trip_and_refusals(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_bytes(b"[]")
            self.assertEqual(read_candidates_file(p), ())
            p.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "^malformed_configuration$"):
                read_candidates_file(p)
            p.write_bytes(b"[{\"model\": 1, \"model\": 2}]")
            with self.assertRaisesRegex(ValueError, "^malformed_configuration$"):
                read_candidates_file(p)
            with self.assertRaisesRegex(ValueError, "^malformed_configuration$"):
                read_candidates_file(Path(d) / "missing")
            with self.assertRaisesRegex(ValueError, "^malformed_configuration$"):
                read_candidates_file(Path(d))
            if hasattr(os, "mkfifo"):
                pipe = Path(d) / "pipe"
                os.mkfifo(pipe)
                with self.assertRaisesRegex(ValueError, "^malformed_configuration$"):
                    read_candidates_file(pipe)
