"""FR-02: Kalshi GET signing with RSA-PSS and Ed25519 test keys generated in the test."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from pmr.http.kalshi_auth import (
    HEADER_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    KalshiAuthError,
    auth_headers,
    sign_request,
)

PATH = "/trade-api/v2/historical/markets"


def _write(tmp_path: Path, key: object, name: str) -> Path:
    pem = key.private_bytes(  # type: ignore[attr-defined]
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    p = tmp_path / name
    p.write_bytes(pem)
    return p


def test_rsa_pss_signature_verifies_and_excludes_query(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = _write(tmp_path, key, "rsa.pem")
    sig = sign_request(str(p), 1_700_000_000_123, "get", PATH + "?limit=5&cursor=abc")
    key.public_key().verify(
        base64.b64decode(sig),
        f"1700000000123GET{PATH}".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_ed25519_signature_verifies(tmp_path: Path) -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    p = _write(tmp_path, key, "ed.pem")
    sig = sign_request(str(p), 42, "GET", "https://external-api.kalshi.com" + PATH + "?x=1")
    key.public_key().verify(base64.b64decode(sig), f"42GET{PATH}".encode())


def test_ed25519_is_deterministic(tmp_path: Path) -> None:
    p = _write(tmp_path, ed25519.Ed25519PrivateKey.generate(), "ed.pem")
    assert sign_request(str(p), 1, "GET", PATH) == sign_request(str(p), 1, "GET", PATH)


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT"])
def test_non_get_is_never_signed(tmp_path: Path, method: str) -> None:
    p = _write(tmp_path, ed25519.Ed25519PrivateKey.generate(), "ed.pem")
    with pytest.raises(KalshiAuthError, match="only GET"):
        sign_request(str(p), 1, method, PATH)


def test_unsupported_and_garbage_keys(tmp_path: Path) -> None:
    p = _write(tmp_path, ec.generate_private_key(ec.SECP256R1()), "ec.pem")
    with pytest.raises(KalshiAuthError, match="unsupported"):
        sign_request(str(p), 1, "GET", PATH)
    bad = tmp_path / "bad.pem"
    bad.write_text("not a key")
    with pytest.raises(KalshiAuthError, match="usable PEM"):
        sign_request(str(bad), 1, "GET", PATH)


def test_auth_headers(tmp_path: Path) -> None:
    p = _write(tmp_path, ed25519.Ed25519PrivateKey.generate(), "ed.pem")
    h = auth_headers("key-id", str(p), "GET", PATH)
    assert h[HEADER_KEY] == "key-id"
    assert h[HEADER_TIMESTAMP].isdigit() and len(h[HEADER_TIMESTAMP]) == 13
    assert base64.b64decode(h[HEADER_SIGNATURE])
