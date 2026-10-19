"""Minisign signatures, verified with the standard library only: Spola's
releases sign the Windows installer (the key's secret half in the CI, the
public half here), and an update runs only an installer signed with it.

Ed25519 as in RFC 8032 (its reference verification, section 6: slow by the
standards of C, a few milliseconds for the one signature of an update);
the signature of minisign's prehashed form ("ED"), over the BLAKE2b-512 of
the file, and its global signature over the trusted comment, which names
the file: a signature of another release's installer is not taken for
this one's.
"""

from __future__ import annotations

import base64
import binascii
import hashlib

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493       # the group's order


class SignatureError(Exception):
    pass


def _inverse(x: int) -> int:
    return pow(x, P - 2, P)


D = -121665 * _inverse(121666) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _add(a: tuple, b: tuple) -> tuple:
    """Two points in extended coordinates (X, Y, Z, T)."""
    e, h = (a[1] - a[0]) * (b[1] - b[0]) % P, (a[1] + a[0]) * (b[1] + b[0]) % P
    c, d = 2 * a[3] * b[3] * D % P, 2 * a[2] * b[2] % P
    e, f, g, h = h - e, d - c, d + c, h + e
    return e * f, g * h, f * g, e * h


def _multiply(s: int, point: tuple) -> tuple:
    result = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            result = _add(result, point)
        point = _add(point, point)
        s >>= 1
    return result


def _equal(a: tuple, b: tuple) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _x_of(y: int, sign: int) -> int | None:
    if y >= P:
        return None
    x2 = (y * y - 1) * _inverse(D * y * y + 1) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    return P - x if (x & 1) != sign else x


def _point(data: bytes) -> tuple | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _x_of(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


_GY = 4 * _inverse(5) % P
_G = (_x_of(_GY, 0), _GY, 1, _x_of(_GY, 0) * _GY % P)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(signature) != 64:
        return False
    a, r = _point(public), _point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= L:
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % L
    return _equal(_multiply(s, _G), _add(r, _multiply(k, a)))


def _decode(text: str, length: int, what: str) -> bytes:
    try:
        data = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        data = b""
    if len(data) != length:
        raise SignatureError(f"not a minisign {what}")
    return data


def blake2b():
    """The hash minisign signs, to be fed with the file as it comes."""
    return hashlib.blake2b(digest_size=64)


def verify(digest: bytes, signature: str, public_key: str) -> str:
    """The trusted comment of a minisign signature (the .minisig file's
    text) of a file whose BLAKE2b-512 is digest, made with the key whose
    public half is public_key (the base64 line of minisign.pub); raises
    SignatureError when it is not."""
    lines = [line.rstrip("\r") for line in signature.split("\n")]
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:") \
            or not lines[2].startswith("trusted comment: "):
        raise SignatureError("not a minisign signature")
    key = _decode(public_key, 42, "public key")
    sig = _decode(lines[1], 74, "signature")
    global_sig = _decode(lines[3], 64, "signature")
    if key[:2] != b"Ed":
        raise SignatureError("not an Ed25519 key")
    if sig[:2] != b"ED":
        # "Ed", the legacy form over the whole file: never made by the CI
        raise SignatureError("not a prehashed signature")
    if sig[2:10] != key[2:10]:
        raise SignatureError("signed with another key")
    trusted = lines[2][len("trusted comment: "):]
    if not ed25519_verify(key[10:], digest, sig[10:]):
        raise SignatureError("the signature does not match the file")
    if not ed25519_verify(key[10:], sig[10:] + trusted.encode("utf-8"), global_sig):
        raise SignatureError("the signature does not match its trusted comment")
    return trusted
