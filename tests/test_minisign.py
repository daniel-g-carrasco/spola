#!/usr/bin/env python3
"""Minisign signatures checked with the standard library: Ed25519 against
RFC 8032's vectors, and signatures made by minisign 0.12 itself."""

import base64
import unittest

import _setup  # noqa: F401

from spola import minisign

# a key of the tests' own (minisign -G -W), never Spola's
PUBLIC_KEY = "RWR7SJiRpYyOYfMs5NsaotSrG3B4wGxiTvXOWkzu4JCT3e240QSuHElq"
PAYLOAD = b"installer" * 1000
# minisign -S -m payload -t "timestamp:1792398099 file:spola-setup-v9-windows-x64.exe hashed" (and v8)
SIGNED_V9 = (
    "untrusted comment: signature from minisign secret key\n"
    "RUR7SJiRpYyOYTjY8y9f5xQs4K0f1/VnY5nfhx92xYh0ROEhmhIgOa2L0LoAiY/j94qPDaF0/iYheQNA+394mJGnMBXICA7BTA0=\n"
    "trusted comment: timestamp:1792398099 file:spola-setup-v9-windows-x64.exe hashed\n"
    "Qs+9uJ0rlFwspS9yeKoD/xfGxiBTlMs0BIzyXEVY+tRp+oh26MKy3J3MmQuZMaRh4seA9ImGsaGouHgIzgYfDg==\n")
SIGNED_V8 = (
    "untrusted comment: signature from minisign secret key\n"
    "RUR7SJiRpYyOYTjY8y9f5xQs4K0f1/VnY5nfhx92xYh0ROEhmhIgOa2L0LoAiY/j94qPDaF0/iYheQNA+394mJGnMBXICA7BTA0=\n"
    "trusted comment: timestamp:1792398099 file:spola-setup-v8-windows-x64.exe hashed\n"
    "VMcRAgf4XsA2FSeekO08xvUjl0zYLCU+vpe9mCCJ/KfpgB9pAAOnQhZFMVowHAmE16JaTXRW+jA1dRWK+y3BAw==\n")


def digest_of(data: bytes) -> bytes:
    h = minisign.blake2b()
    h.update(data)
    return h.digest()


class Ed25519Test(unittest.TestCase):
    VECTORS = [   # RFC 8032, 7.1, tests 1 to 3: public key, message, signature
        ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0"
         "595bbe24655141438e7a100b"),
        ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eae"
         "b4302aeeb00d291612bb0c00"),
        ("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
         "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9"
         "716ed28dc027beceea1ec40a"),
    ]

    def test_the_vectors_of_rfc_8032(self):
        for public, message, signature in self.VECTORS:
            public, message, signature = bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)
            self.assertTrue(minisign.ed25519_verify(public, message, signature))
            self.assertFalse(minisign.ed25519_verify(public, message + b"x", signature))
            self.assertFalse(minisign.ed25519_verify(public, message, signature[:-1] + bytes([signature[-1] ^ 1])))
            # S not below the group's order: refused, never reduced
            s = int.from_bytes(signature[32:], "little") + minisign.L
            self.assertFalse(minisign.ed25519_verify(public, message, signature[:32] + s.to_bytes(32, "little")))


class MinisignTest(unittest.TestCase):
    def test_a_signature_made_by_minisign(self):
        self.assertEqual(minisign.verify(digest_of(PAYLOAD), SIGNED_V9, PUBLIC_KEY),
                         "timestamp:1792398099 file:spola-setup-v9-windows-x64.exe hashed")
        # as a file written on Windows
        self.assertTrue(minisign.verify(digest_of(PAYLOAD), SIGNED_V9.replace("\n", "\r\n"), PUBLIC_KEY))

    def test_what_is_refused(self):
        with self.assertRaisesRegex(minisign.SignatureError, "does not match the file"):
            minisign.verify(digest_of(PAYLOAD + b"!"), SIGNED_V9, PUBLIC_KEY)
        # another file's name in the comment, the comment's own signature kept
        lines = SIGNED_V9.split("\n")
        lines[2] = SIGNED_V8.split("\n")[2]
        with self.assertRaisesRegex(minisign.SignatureError, "trusted comment"):
            minisign.verify(digest_of(PAYLOAD), "\n".join(lines), PUBLIC_KEY)
        # Spola's own key: not the one that signed it
        from spola import updates
        with self.assertRaisesRegex(minisign.SignatureError, "another key"):
            minisign.verify(digest_of(PAYLOAD), SIGNED_V9, updates.PUBLIC_KEY)
        # the legacy form, over the whole file
        raw = base64.b64decode(lines[1])
        lines = SIGNED_V9.split("\n")
        lines[1] = base64.b64encode(b"Ed" + raw[2:]).decode()
        with self.assertRaisesRegex(minisign.SignatureError, "prehashed"):
            minisign.verify(digest_of(PAYLOAD), "\n".join(lines), PUBLIC_KEY)
        for text in ("", "<html>Not Found</html>", SIGNED_V9.replace("RUR7", "RUR*")):
            with self.assertRaises(minisign.SignatureError):
                minisign.verify(digest_of(PAYLOAD), text, PUBLIC_KEY)


if __name__ == "__main__":
    unittest.main()
