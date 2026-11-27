#!/usr/bin/env python3
"""Credentials never show their secrets in repr, and so in logs or tracebacks."""

import unittest

import _setup  # noqa: F401

from spola.app.account import Credentials


class CredentialsReprTest(unittest.TestCase):
    def test_repr_hides_obscured_and_password(self):
        creds = Credentials("someone@example.invalid", "OBSCURED-MARKER", "PASSWORD-MARKER")
        text = repr(creds)
        self.assertIn("someone@example.invalid", text)
        self.assertNotIn("OBSCURED-MARKER", text)
        self.assertNotIn("PASSWORD-MARKER", text)
        self.assertEqual(creds.obscured, "OBSCURED-MARKER")


if __name__ == "__main__":
    unittest.main()
