from __future__ import annotations

import base64
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "isolated-pow-test-key")

from utils.pow import _pow_generate, build_proof_token


CONFIG = [1080, "fixture-date", 4294705152, 0, "fixture-agent",
          "https://example.invalid/sdk.js", "", "en-US", "en-US", 0]
# 由 JavaScript Math.imul 实现独立计算的向量，不包含真实挑战或账号信息。
ANSWER = (
    "WzEwODAsImZpeHR1cmUtZGF0ZSIsNDI5NDcwNTE1MiwwLCJmaXh0dXJlLWFnZW50IiwiaHR0"
    "cHM6Ly9leGFtcGxlLmludmFsaWQvc2RrLmpzIiwiIiwiZW4tVVMiLCJlbi1VUyIsMF0="
)


class ProofProtocolTests(unittest.TestCase):
    def test_javascript_hash_vector_accepts_exact_boundary(self):
        with patch("utils.pow.time.monotonic", return_value=10):
            self.assertEqual((ANSWER, True), _pow_generate("fixture-seed", "04c9f5c4", CONFIG, limit=1))

    def test_javascript_hash_vector_rejects_lower_boundary(self):
        with patch("utils.pow.time.monotonic", return_value=10):
            self.assertEqual(("", False), _pow_generate("fixture-seed", "04c9f5c3", CONFIG, limit=1))

    def test_hex_prefix_accepts_odd_length_and_uppercase(self):
        with patch("utils.pow.time.monotonic", return_value=10):
            for difficulty in ("0", "04C9F5C4"):
                with self.subTest(difficulty=difficulty):
                    self.assertEqual((ANSWER, True), _pow_generate("fixture-seed", difficulty, CONFIG, limit=1))

    def test_wire_token_includes_success_suffix(self):
        with patch("utils.pow.build_pow_config", return_value=CONFIG), \
             patch("utils.pow.time.monotonic", return_value=10):
            token = build_proof_token("fixture-seed", "04c9f5c4", "fixture-agent")
        self.assertEqual("gAAAAAB" + ANSWER + "~S", token)
        self.assertEqual(CONFIG, json.loads(base64.b64decode(token[7:-2], validate=True)))

    def test_elapsed_field_uses_time_without_mutating_input(self):
        with patch("utils.pow.time.monotonic", side_effect=[10, 10.125]):
            answer, solved = _pow_generate("fixture-seed", "ffffffff", CONFIG, limit=1)
        payload = json.loads(base64.b64decode(answer))
        self.assertTrue(solved)
        self.assertEqual(125, payload[9])
        self.assertEqual(0, CONFIG[9])

    def test_time_budget_stops_before_hashing(self):
        with patch("utils.pow.time.monotonic", side_effect=[10, 13]), \
             patch("utils.pow.SentinelTokenGenerator._fnv1a_32") as hash_value:
            self.assertEqual(("", False), _pow_generate("fixture-seed", "0", CONFIG))
        hash_value.assert_not_called()

    def test_exhausted_search_does_not_emit_a_success_token(self):
        with patch("utils.pow._pow_generate", return_value=("", False)):
            with self.assertRaisesRegex(RuntimeError, "failed to solve proof token"):
                build_proof_token("fixture-seed", "0", "fixture-agent")

    def test_rejects_untrusted_challenge_values(self):
        for seed in (None, "", "x" * 4097, "line\nbreak", "非 ASCII"):
            with self.subTest(seed_type=type(seed).__name__):
                with self.assertRaisesRegex(ValueError, "seed"):
                    _pow_generate(seed, "0", CONFIG)
        for difficulty in (None, "", "x", "0" * 9, "-1", [], True):
            with self.subTest(difficulty=difficulty):
                with self.assertRaisesRegex(ValueError, "difficulty"):
                    _pow_generate("fixture-seed", difficulty, CONFIG)
        for limit in (0, -1, 500001, True, "1"):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(ValueError, "attempt limit"):
                    _pow_generate("fixture-seed", "0", CONFIG, limit=limit)

    def test_rejects_oversized_or_malformed_config(self):
        for config in (None, [], [0] * 65, ["x" * 65536] + CONFIG):
            with self.assertRaisesRegex(ValueError, "configuration"):
                _pow_generate("fixture-seed", "0", config)


if __name__ == "__main__":
    unittest.main()
