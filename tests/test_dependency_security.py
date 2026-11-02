"""Synthetic regressions for the JWT boundary inherited through the MCP SDK."""
from __future__ import annotations

import unittest

import jwt


class JwtDependencySecurityTests(unittest.TestCase):
    key = "synthetic-local-regression-key-only-0000000000000000"

    def test_unverified_decode_does_not_mutate_options_or_disable_later_claim_checks(self):
        for method in (jwt.decode, jwt.decode_complete):
            with self.subTest(method=method.__name__):
                expired = jwt.encode({"sub": "fixture", "exp": 1}, self.key, algorithm="HS256")
                options = {"verify_signature": False}
                method(expired, options=options)
                self.assertEqual(options, {"verify_signature": False})
                options["verify_signature"] = True
                with self.assertRaises(jwt.ExpiredSignatureError):
                    method(expired, self.key, algorithms=["HS256"], options=options)

    def test_reused_options_preserve_audience_verification(self):
        token = jwt.encode({"sub": "fixture", "aud": "wrong-audience"}, self.key, algorithm="HS256")
        options = {"verify_signature": False}
        jwt.decode(token, options=options)
        options["verify_signature"] = True
        with self.assertRaises(jwt.InvalidAudienceError):
            jwt.decode(token, self.key, algorithms=["HS256"], audience="expected-audience", options=options)

    def test_valid_signed_token_still_verifies(self):
        token = jwt.encode({"sub": "fixture", "aud": "expected-audience"}, self.key, algorithm="HS256")
        claims = jwt.decode(token, self.key, algorithms=["HS256"], audience="expected-audience")
        self.assertEqual(claims["sub"], "fixture")

    def test_wrong_signature_still_fails(self):
        token = jwt.encode({"sub": "fixture"}, self.key, algorithm="HS256")
        with self.assertRaises(jwt.InvalidSignatureError):
            jwt.decode(token, self.key + "-different", algorithms=["HS256"])
