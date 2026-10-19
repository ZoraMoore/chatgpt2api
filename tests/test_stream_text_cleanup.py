import os
import time
import unittest

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-admin-key")
from services.protocol.conversation import sanitize_output_text


class StreamTextCleanupTests(unittest.TestCase):
    def test_whitespace_before_punctuation_preserves_existing_behavior(self):
        self.assertEqual("hello, world!\nnext", sanitize_output_text("hello \t, world\n !\nnext"))
        self.assertEqual("中文：保留  文本。", sanitize_output_text("中文：保留  文本。"))
        self.assertEqual("!end", sanitize_output_text(" \t\n!end"))

    def test_long_whitespace_stream_does_not_block_python_threads(self):
        text = "prefix" + " \n\t" * 100000 + "suffix"
        started = time.monotonic()
        self.assertEqual(text, sanitize_output_text(text))
        self.assertLess(time.monotonic() - started, 1.5)


if __name__ == "__main__":
    unittest.main()
