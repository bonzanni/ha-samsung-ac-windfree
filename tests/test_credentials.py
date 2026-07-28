"""Behavioural tests for the uploaded-credential security boundary.

The config-flow tests patch the upload reader, so without this module nothing
exercises the parser or the upload consumption path.
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

from custom_components.samsung_ac_windfree.config_flow import (
    _async_read_uploaded_credential,
)
from custom_components.samsung_ac_windfree.credentials import (
    MAX_CREDENTIAL_BYTES,
    CredentialError,
    parse_uploaded_credential,
)

NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _cert(
    subject: str,
    key,
    *,
    issuer: str | None = None,
    signing_key=None,
    starts: datetime | None = None,
    expires: datetime | None = None,
    algorithm=None,
    padding_mode=None,
) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer or subject))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(starts or NOW - timedelta(days=1))
        .not_valid_after(expires or NOW + timedelta(days=365))
    )
    kwargs = {}
    if padding_mode is not None:
        kwargs["rsa_padding"] = padding_mode
    return builder.sign(signing_key or key, algorithm or hashes.SHA256(), **kwargs)


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def leaf_pair(rsa_key):
    return rsa_key, _cert("leaf", rsa_key)


def test_accepts_a_matching_key_and_leaf(leaf_pair) -> None:
    key, leaf = leaf_pair
    result = parse_uploaded_credential(_key_pem(key), _pem(leaf), now=NOW)
    assert result.not_before == leaf.not_valid_before_utc.isoformat()
    assert result.not_after == leaf.not_valid_after_utc.isoformat()


def test_rejects_key_not_matching_leaf(leaf_pair) -> None:
    _key, leaf = leaf_pair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(CredentialError) as err:
        parse_uploaded_credential(_key_pem(other), _pem(leaf), now=NOW)
    assert str(err.value) == "credentials_key_mismatch"


def test_rejects_encrypted_key(leaf_pair) -> None:
    key, leaf = leaf_pair
    encrypted = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"secret"),
    )
    with pytest.raises(CredentialError) as err:
        parse_uploaded_credential(encrypted, _pem(leaf), now=NOW)
    assert str(err.value) == "credentials_invalid_key"


def test_rejects_trailing_bytes_after_pem(leaf_pair) -> None:
    key, leaf = leaf_pair
    with pytest.raises(CredentialError) as err:
        parse_uploaded_credential(_key_pem(key), _pem(leaf) + b"trailing", now=NOW)
    assert str(err.value) == "credentials_invalid_chain"


def test_rejects_certificate_supplied_as_key(leaf_pair) -> None:
    _key, leaf = leaf_pair
    with pytest.raises(CredentialError) as err:
        parse_uploaded_credential(_pem(leaf), _pem(leaf), now=NOW)
    # The category must name the key, not the chain.
    assert str(err.value) == "credentials_invalid_key"


@pytest.mark.parametrize(
    ("starts", "expires", "expected"),
    [
        (NOW + timedelta(days=1), NOW + timedelta(days=2), "credentials_not_yet_valid"),
        (NOW - timedelta(days=2), NOW - timedelta(days=1), "credentials_expired"),
    ],
)
def test_rejects_leaf_outside_its_validity(rsa_key, starts, expires, expected) -> None:
    leaf = _cert("leaf", rsa_key, starts=starts, expires=expires)
    with pytest.raises(CredentialError) as err:
        parse_uploaded_credential(_key_pem(rsa_key), _pem(leaf), now=NOW)
    assert str(err.value) == expected


def test_rejects_oversized_input(leaf_pair) -> None:
    key, leaf = leaf_pair
    with pytest.raises(CredentialError) as err:
        parse_uploaded_credential(
            _key_pem(key), _pem(leaf) + b"\n" * (MAX_CREDENTIAL_BYTES + 1), now=NOW
        )
    assert str(err.value) == "credentials_too_large"


def test_rejects_unrelated_certificate_after_the_leaf(rsa_key) -> None:
    """A second certificate must be the issuer, not merely present."""

    leaf = _cert("leaf", rsa_key)
    stranger = _cert(
        "stranger", rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    with pytest.raises(CredentialError) as err:
        parse_uploaded_credential(
            _key_pem(rsa_key), _pem(leaf) + _pem(stranger), now=NOW
        )
    assert str(err.value) == "credentials_invalid_chain"


def test_rejects_expired_intermediate(rsa_key) -> None:
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = _cert(
        "ca", ca_key, starts=NOW - timedelta(days=10), expires=NOW - timedelta(days=1)
    )
    leaf = _cert("leaf", rsa_key, issuer="ca", signing_key=ca_key)
    with pytest.raises(CredentialError) as err:
        parse_uploaded_credential(_key_pem(rsa_key), _pem(leaf) + _pem(ca), now=NOW)
    assert str(err.value) == "credentials_invalid_chain"


def test_accepts_a_correctly_signed_chain(rsa_key) -> None:
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = _cert("ca", ca_key)
    leaf = _cert("leaf", rsa_key, issuer="ca", signing_key=ca_key)
    result = parse_uploaded_credential(
        _key_pem(rsa_key), _pem(leaf) + _pem(ca), now=NOW
    )
    assert result.client_chain_pem.count("BEGIN CERTIFICATE") == 2


def test_rejects_ec_chain_with_a_forged_signature() -> None:
    """A bad EC signature must not pass as 'unsupported algorithm'."""

    ca_key = ec.generate_private_key(ec.SECP256R1())
    impostor = ec.generate_private_key(ec.SECP256R1())
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = _cert("ca", ca_key)
    # Names line up, but the signature is from a different key entirely.
    forged = _cert("leaf", leaf_key, issuer="ca", signing_key=impostor)
    with pytest.raises(CredentialError) as err:
        parse_uploaded_credential(_key_pem(leaf_key), _pem(forged) + _pem(ca), now=NOW)
    assert str(err.value) == "credentials_invalid_chain"


def test_accepts_a_valid_rsa_pss_chain(rsa_key) -> None:
    """A correctly signed RSA-PSS chain must not be rejected."""

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = _cert("ca", ca_key)
    pss = padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
    )
    leaf = _cert("leaf", rsa_key, issuer="ca", signing_key=ca_key, padding_mode=pss)
    result = parse_uploaded_credential(
        _key_pem(rsa_key), _pem(leaf) + _pem(ca), now=NOW
    )
    assert result.client_chain_pem.count("BEGIN CERTIFICATE") == 2


def test_errors_never_carry_uploaded_material(leaf_pair) -> None:
    _key, leaf = leaf_pair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(CredentialError) as err:
        parse_uploaded_credential(_key_pem(other), _pem(leaf), now=NOW)
    message = str(err.value)
    assert "BEGIN" not in message
    assert "\n" not in message
    formatted = "".join(traceback.format_exception_only(err.value))
    assert "BEGIN" not in formatted


# --- upload consumption -------------------------------------------------------


class _FakeUpload:
    """Stand-in for process_uploaded_file that records consumption."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.consumed: list[str] = []

    def __call__(self, _hass, upload_id: str):
        return self._ctx(upload_id)

    def _ctx(self, upload_id: str):
        outer = self

        class _Ctx:
            def __enter__(self):
                if upload_id not in outer.files:
                    raise ValueError("File does not exist")
                return _FakePath(outer.files[upload_id])

            def __exit__(self, *_exc):
                outer.consumed.append(upload_id)
                outer.files.pop(upload_id, None)
                return False

        return _Ctx()


class _FakePath:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def stat(self):
        class _Stat:
            st_size = len(self._data)

        stat = _Stat()
        stat.st_size = len(self._data)
        return stat

    def read_bytes(self) -> bytes:
        return self._data


async def _read(hass, uploader, key_id, chain_id):
    with patch(
        "custom_components.samsung_ac_windfree.config_flow.process_uploaded_file",
        new=uploader,
    ):
        return await _async_read_uploaded_credential(hass, key_id, chain_id)


async def test_both_uploads_are_consumed_on_success(hass, leaf_pair) -> None:
    key, leaf = leaf_pair
    uploader = _FakeUpload({"k": _key_pem(key), "c": _pem(leaf)})
    await _read(hass, uploader, "k", "c")
    assert sorted(uploader.consumed) == ["c", "k"]
    assert uploader.files == {}


async def test_chain_upload_is_consumed_when_the_key_is_unreadable(hass) -> None:
    """A failure on the first handle must not strand the second."""

    uploader = _FakeUpload({"c": b"chain"})  # "k" is missing
    with pytest.raises(CredentialError):
        await _read(hass, uploader, "k", "c")
    assert uploader.consumed == ["c"]
    assert uploader.files == {}


async def test_duplicate_upload_id_is_consumed_once_then_rejected(
    hass, leaf_pair
) -> None:
    key, _leaf = leaf_pair
    uploader = _FakeUpload({"same": _key_pem(key)})
    with pytest.raises(CredentialError) as err:
        await _read(hass, uploader, "same", "same")
    assert str(err.value) == "credentials_duplicate_file"
    assert uploader.consumed == ["same"], "the single handle must still be consumed"
    assert uploader.files == {}


async def test_oversized_upload_keeps_its_own_category(hass, leaf_pair) -> None:
    _key, leaf = leaf_pair
    uploader = _FakeUpload({"k": b"x" * (MAX_CREDENTIAL_BYTES + 1), "c": _pem(leaf)})
    with pytest.raises(CredentialError) as err:
        await _read(hass, uploader, "k", "c")
    assert str(err.value) == "credentials_too_large"
    # Oversized input is still deleted, and never read into memory.
    assert sorted(uploader.consumed) == ["c", "k"]
