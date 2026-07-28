"""Parse and validate an uploaded local client credential.

The integration no longer mints certificates. The owner supplies a client key
and certificate chain produced out of band, and this module decides whether they
are usable before anything is persisted or a connection is attempted.

Every failure is a fixed category, and no uploaded byte is ever interpolated
into a message or a log record. Frame locals still hold the uploaded bytes
while an error propagates, so config-flow callers clear the traceback before
the error is surfaced.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .models import Credentials

# The upload endpoint permits far larger files; a client credential is a few KiB.
MAX_CREDENTIAL_BYTES = 256 * 1024

ERROR_INVALID_KEY = "credentials_invalid_key"
ERROR_INVALID_CHAIN = "credentials_invalid_chain"
ERROR_KEY_MISMATCH = "credentials_key_mismatch"
ERROR_NOT_YET_VALID = "credentials_not_yet_valid"
ERROR_EXPIRED = "credentials_expired"
ERROR_TOO_LARGE = "credentials_too_large"

_PEM_BLOCK = re.compile(
    rb"-----BEGIN ([A-Z0-9 ]+)-----\r?\n"
    rb"[A-Za-z0-9+/=\r\n]+"
    rb"-----END \1-----\r?\n?"
)


class CredentialError(ValueError):
    """Uploaded credential rejected. Carries only a fixed category."""


def _blocks(
    data: bytes, category: str = ERROR_INVALID_CHAIN
) -> list[tuple[bytes, bytes]]:
    """Return (label, block) pairs, rejecting anything outside a PEM block.

    Trailing or interleaved bytes are refused rather than ignored: a file that
    is not exactly a sequence of PEM blocks is not a credential we understand.
    """

    found: list[tuple[bytes, bytes]] = []
    cursor = 0
    for match in _PEM_BLOCK.finditer(data):
        if data[cursor : match.start()].strip():
            raise CredentialError(category)
        found.append((match.group(1), match.group(0)))
        cursor = match.end()
    if data[cursor:].strip():
        raise CredentialError(category)
    return found


def _load_key(data: bytes):
    blocks = _blocks(data, ERROR_INVALID_KEY)
    if len(blocks) != 1:
        raise CredentialError(ERROR_INVALID_KEY)
    label = blocks[0][0]
    if b"ENCRYPTED" in label:
        # No passphrase field exists; refuse rather than fail later in the flow.
        raise CredentialError(ERROR_INVALID_KEY)
    if not label.endswith(b"PRIVATE KEY"):
        raise CredentialError(ERROR_INVALID_KEY)
    try:
        return serialization.load_pem_private_key(data, password=None)
    except Exception:
        raise CredentialError(ERROR_INVALID_KEY) from None


def _load_chain(data: bytes) -> list[x509.Certificate]:
    blocks = _blocks(data)
    if not blocks or any(label != b"CERTIFICATE" for label, _ in blocks):
        raise CredentialError(ERROR_INVALID_CHAIN)
    chain: list[x509.Certificate] = []
    for _label, block in blocks:
        try:
            chain.append(x509.load_pem_x509_certificate(block))
        except Exception:
            raise CredentialError(ERROR_INVALID_CHAIN) from None
    return chain


def _spki(public_key) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def parse_uploaded_credential(
    key_bytes: bytes,
    chain_bytes: bytes,
    *,
    now: datetime | None = None,
) -> Credentials:
    """Validate an uploaded key and chain and derive its validity window.

    Raises CredentialError with a fixed category. The leaf is the first
    certificate in the chain; validity dates come from it rather than from any
    value supplied alongside it.
    """

    if len(key_bytes) > MAX_CREDENTIAL_BYTES or len(chain_bytes) > MAX_CREDENTIAL_BYTES:
        raise CredentialError(ERROR_TOO_LARGE)

    private_key = _load_key(key_bytes)
    chain = _load_chain(chain_bytes)
    leaf = chain[0]

    # Compare DER SubjectPublicKeyInfo so this holds for RSA and EC alike.
    if _spki(private_key.public_key()) != _spki(leaf.public_key()):
        raise CredentialError(ERROR_KEY_MISMATCH)

    not_before = leaf.not_valid_before_utc
    not_after = leaf.not_valid_after_utc
    if not_after <= not_before:
        raise CredentialError(ERROR_INVALID_CHAIN)

    moment = now if now is not None else datetime.now(UTC)
    if moment < not_before:
        raise CredentialError(ERROR_NOT_YET_VALID)
    if moment >= not_after:
        raise CredentialError(ERROR_EXPIRED)

    # Anything after the leaf must actually be a chain. Without this, an
    # unrelated, duplicated, misordered or expired certificate placed after the
    # leaf is accepted and stored, and only fails much later at the handshake.
    for child, parent in pairwise(chain):
        if not (parent.not_valid_before_utc <= moment < parent.not_valid_after_utc):
            raise CredentialError(ERROR_INVALID_CHAIN)
        try:
            # Dispatches on the certificate's own algorithm and key type, and
            # checks issuer/subject too. Verifying by hand would mean assuming a
            # signature scheme: assuming RSA PKCS#1 v1.5 both accepts a bad EC
            # signature (the TypeError looks like "unsupported") and rejects a
            # valid RSA-PSS one.
            child.verify_directly_issued_by(parent)
        except Exception:
            # Fail closed: anything we cannot positively verify is not a chain.
            raise CredentialError(ERROR_INVALID_CHAIN) from None

    return Credentials(
        client_key_pem=key_bytes.decode("ascii", "strict"),
        client_chain_pem=chain_bytes.decode("ascii", "strict"),
        not_before=not_before.isoformat(),
        not_after=not_after.isoformat(),
    )


def stored_credentials(data: Mapping[str, Any]) -> Credentials | None:
    """Rebuild a Credentials from persisted config-entry data.

    Shared by entry setup and the reconfigure flow so both agree on what counts
    as a usable stored credential. Returns None rather than raising: callers
    decide whether that is a setup error or a flow error.
    """

    try:
        credentials = Credentials(
            client_key_pem=data["client_key_pem"],
            client_chain_pem=data["client_chain_pem"],
            not_before=data["not_before"],
            not_after=data["not_after"],
        )
        if not all(
            isinstance(value, str)
            for value in (
                credentials.client_key_pem,
                credentials.client_chain_pem,
                credentials.not_before,
                credentials.not_after,
            )
        ):
            raise ValueError
        not_before = datetime.fromisoformat(credentials.not_before)
        not_after = datetime.fromisoformat(credentials.not_after)
        if (
            not_before.tzinfo is None
            or not_after.tzinfo is None
            or not_before >= not_after
        ):
            raise ValueError
    except KeyError, TypeError, ValueError:
        return None
    return credentials
