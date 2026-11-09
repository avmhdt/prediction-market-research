"""Optional Kalshi request signing for read-only GETs (FR-02, NFR-07).

The private key is read from ``KALSHI_PRIVATE_KEY_PATH`` at request time only. It is never
copied, cached, logged or used for anything but signing GET requests. This module is the
only place in the project that loads a private key (checked by ``scripts/safety_grep.py``).

Signed message: ``str(timestamp_ms) + METHOD + path``, where ``path`` excludes the query
string. RSA keys sign with RSA-PSS (SHA-256, MGF1-SHA-256, salt length = digest length);
Ed25519 keys sign per RFC 8032. The key type is detected from the PEM.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

HEADER_KEY = "KALSHI-ACCESS-KEY"
HEADER_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"
HEADER_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"


class KalshiAuthError(RuntimeError):
    """Raised for an unusable key file or a non-GET signing request."""


def _message(ts_ms: int, method: str, path: str) -> bytes:
    path_only = urlsplit(path).path if "://" in path else path.split("?", 1)[0]
    return f"{ts_ms}{method.upper()}{path_only}".encode()


def sign_request(key_pem_path: str, ts_ms: int, method: str, path: str) -> str:
    """Return the base64 signature of ``ts_ms + METHOD + path`` (query string removed).

    Args:
        key_pem_path: path to a PEM private key (RSA or Ed25519), unencrypted.
        ts_ms: request timestamp in milliseconds since the Unix epoch.
        method: HTTP method; only ``GET`` is signed.
        path: request path such as ``/trade-api/v2/markets?limit=1`` (query is dropped).
    """
    if method.upper() != "GET":
        raise KalshiAuthError("only GET requests are signed: this project never trades")
    pem = Path(key_pem_path).read_bytes()
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        raise KalshiAuthError("KALSHI_PRIVATE_KEY_PATH does not hold a usable PEM key") from exc
    finally:
        del pem
    msg = _message(ts_ms, method, path)
    if isinstance(key, rsa.RSAPrivateKey):
        sig = key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    elif isinstance(key, ed25519.Ed25519PrivateKey):
        sig = key.sign(msg)
    else:
        raise KalshiAuthError("unsupported key type: expected RSA or Ed25519")
    return base64.b64encode(sig).decode("ascii")


def auth_headers(key_id: str, key_pem_path: str, method: str, path: str) -> dict[str, str]:
    """Build the three Kalshi auth headers for one GET request."""
    ts_ms = int(time.time() * 1000)
    return {
        HEADER_KEY: key_id,
        HEADER_TIMESTAMP: str(ts_ms),
        HEADER_SIGNATURE: sign_request(key_pem_path, ts_ms, method, path),
    }
