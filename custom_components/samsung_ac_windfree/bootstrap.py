"""Pinned one-time certificate bootstrap for local WindFree authentication."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from uuid import UUID

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from OpenSSL import crypto

from .const import (
    REMOVED_SIGNING_DIGEST_NAME,
    BUNDLE_SHA256,
    BUNDLE_URL,
    HTTPS_TIMEOUT,
    SAMSUNG_IDENTITY_HOST,
    SAMSUNG_IDENTITY_LEAF_SHA256,
    SAMSUNG_IDENTITY_SPKI_SHA256,
)
from .models import BootstrapError, Credentials

_MAX_BUNDLE_SIZE = 64 * 1024
_PEM_BLOCK = re.compile(
    rb"-----BEGIN ([A-Z0-9 ]+)-----\r?\n"
    rb"[A-Za-z0-9+/=\r\n]+"
    rb"-----END \1-----\r?\n?"
)
_UUID_OU = re.compile(
    r"uuid:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

ERROR_UNAVAILABLE = "bootstrap_unavailable: bootstrap source unavailable"
ERROR_PIN_MISMATCH = "bootstrap_pin_mismatch: bootstrap material changed"
ERROR_INVALID_CLOCK = "invalid_clock: system clock is outside the allowed range"
ERROR_INVALID_MATERIAL = "bootstrap_invalid_material: bootstrap material is invalid"

_ROLE_OID = ObjectIdentifier("1.3.6.1.4.1.51414.1.3")
_OCF_CLIENT_OID = ObjectIdentifier("1.3.6.1.4.1.51414.0.1.2")
_ROLE_VALUE = b"\x0c\x10samsung.role.hub"


def _samsung_name(
    common_name: str,
    *,
    organizational_unit: str | None = None,
    email: str | None = None,
) -> x509.Name:
    attributes = [
        x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Samsung Electronics"),
    ]
    if organizational_unit is not None:
        attributes.append(
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, organizational_unit)
        )
    attributes.append(x509.NameAttribute(NameOID.COMMON_NAME, common_name))
    if email is not None:
        attributes.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email))
    return x509.Name(attributes)


_BUNDLE_SUBJECTS = (
    _samsung_name("REMOVED_IDENTITY", email="REMOVED_IDENTITY"),
    _samsung_name("RemoteAccessCA(CE)"),
    _samsung_name("CECA"),
    _samsung_name("ROOTCA"),
)
_IDENTITY_ISSUER = _samsung_name(
    "Samsung Electronics OCF Server SubCA",
    organizational_unit="OCF Server SubCA",
)


@dataclass(frozen=True, slots=True)
class BundleMaterial:
    signing_key: rsa.RSAPrivateKey
    signing_certificate: x509.Certificate
    public_chain: tuple[x509.Certificate, ...]


@dataclass(frozen=True, slots=True)
class BootstrapInputs:
    bundle_bytes: bytes
    identity_der: bytes
    server_date: datetime
    local_now: datetime


@dataclass(frozen=True, slots=True)
class BootstrapPins:
    bundle_sha256: str
    signing_sha256: str
    identity_leaf_sha256: str
    identity_spki_sha256: str

    @classmethod
    def from_constants(cls) -> BootstrapPins:
        return cls(
            bundle_sha256=BUNDLE_SHA256,
            signing_sha256=REMOVED_SIGNING_DIGEST_NAME,
            identity_leaf_sha256=SAMSUNG_IDENTITY_LEAF_SHA256,
            identity_spki_sha256=SAMSUNG_IDENTITY_SPKI_SHA256,
        )


PRODUCTION_PINS = BootstrapPins.from_constants()


def _digest_matches(data: bytes, expected_hex: str) -> bool:
    try:
        expected = bytes.fromhex(expected_hex)
    except ValueError:
        return False
    return hmac.compare_digest(hashlib.sha256(data).digest(), expected)


def _invalid_material(detail: str | None = None) -> BootstrapError:
    if detail is None:
        return BootstrapError(ERROR_INVALID_MATERIAL)
    return BootstrapError(f"{ERROR_INVALID_MATERIAL} ({detail})")


def _has_exact_name_attributes(actual: x509.Name, expected: x509.Name) -> bool:
    return sorted(
        (attribute.oid.dotted_string, attribute.value) for attribute in actual
    ) == sorted(
        (attribute.oid.dotted_string, attribute.value) for attribute in expected
    )


def _parse_exact_pem(data: bytes) -> tuple[bytes, tuple[bytes, ...]]:
    blocks: list[tuple[bytes, bytes]] = []
    position = 0
    for match in _PEM_BLOCK.finditer(data):
        if data[position : match.start()].strip():
            raise _invalid_material()
        blocks.append((match.group(1), match.group(0)))
        position = match.end()
    if data[position:].strip():
        raise _invalid_material()

    key_blocks = [
        block
        for label, block in blocks
        if label in (b"PRIVATE KEY", b"RSA PRIVATE KEY")
    ]
    cert_blocks = [block for label, block in blocks if label == b"CERTIFICATE"]
    if len(blocks) != 5 or len(key_blocks) != 1 or len(cert_blocks) != 4:
        raise _invalid_material()
    if blocks[0][0] not in (b"PRIVATE KEY", b"RSA PRIVATE KEY"):
        raise _invalid_material()
    if any(label != b"CERTIFICATE" for label, _block in blocks[1:]):
        raise _invalid_material()
    return key_blocks[0], tuple(cert_blocks)


def _verify_rsa_signature(
    certificate: x509.Certificate, issuer_key: rsa.RSAPublicKey
) -> None:
    algorithm = certificate.signature_hash_algorithm
    if algorithm is None:
        raise _invalid_material()
    issuer_key.verify(
        certificate.signature,
        certificate.tbs_certificate_bytes,
        padding.PKCS1v15(),
        algorithm,
    )


def validate_bundle(
    data: bytes,
    *,
    pins: BootstrapPins = PRODUCTION_PINS,
) -> BundleMaterial:
    """Validate exact bytes, one key, four certs, key pair, chain, and REMOVED_IDENTITY."""
    if not _digest_matches(data, pins.bundle_sha256):
        raise BootstrapError(ERROR_PIN_MISMATCH)

    try:
        key_pem, certificate_pems = _parse_exact_pem(data)
        key = serialization.load_pem_private_key(key_pem, password=None)
        certificates = tuple(
            x509.load_pem_x509_certificate(pem) for pem in certificate_pems
        )
    except BootstrapError:
        raise
    except Exception as err:
        raise _invalid_material() from err

    if not isinstance(key, rsa.RSAPrivateKey):
        raise _invalid_material()
    if any(
        not isinstance(certificate.public_key(), rsa.RSAPublicKey)
        for certificate in certificates
    ):
        raise _invalid_material()
    signing_certificate = certificates[0]
    if not _digest_matches(
        signing_certificate.public_bytes(serialization.Encoding.DER),
        pins.signing_sha256,
    ):
        raise BootstrapError(ERROR_PIN_MISMATCH)
    signing_public_key = signing_certificate.public_key()
    if not isinstance(signing_public_key, rsa.RSAPublicKey):
        raise _invalid_material()
    if key.public_key().public_numbers() != signing_public_key.public_numbers():
        raise _invalid_material()

    try:
        for index, certificate in enumerate(certificates):
            if not _has_exact_name_attributes(
                certificate.subject, _BUNDLE_SUBJECTS[index]
            ):
                raise _invalid_material()
            issuer = (
                certificates[index + 1]
                if index + 1 < len(certificates)
                else certificate
            )
            if not _has_exact_name_attributes(certificate.issuer, issuer.subject):
                raise _invalid_material()
            basic_constraints = certificate.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
            if not basic_constraints.ca:
                raise _invalid_material()
            issuer_key = issuer.public_key()
            if not isinstance(issuer_key, rsa.RSAPublicKey):
                raise _invalid_material()
            _verify_rsa_signature(certificate, issuer_key)
    except BootstrapError:
        raise
    except Exception as err:
        raise _invalid_material() from err

    return BundleMaterial(
        signing_key=key,
        signing_certificate=signing_certificate,
        public_chain=certificates,
    )


def validate_identity_certificate(
    der: bytes,
    *,
    pins: BootstrapPins = PRODUCTION_PINS,
) -> str:
    """Validate leaf/SPKI/issuer/CN and return UUID from the subject OU."""
    if not _digest_matches(der, pins.identity_leaf_sha256):
        raise BootstrapError(ERROR_PIN_MISMATCH)
    try:
        certificate = x509.load_der_x509_certificate(der)
        spki = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except Exception as err:
        raise _invalid_material() from err
    if not _digest_matches(spki, pins.identity_spki_sha256):
        raise BootstrapError(ERROR_PIN_MISMATCH)
    if not _has_exact_name_attributes(certificate.issuer, _IDENTITY_ISSUER):
        raise _invalid_material("unexpected issuer")

    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(common_names) != 1 or common_names[0].value != "*.REMOVED_HOST.com":
        raise _invalid_material("unexpected common name")
    organizational_units = certificate.subject.get_attributes_for_oid(
        NameOID.ORGANIZATIONAL_UNIT_NAME
    )
    if len(organizational_units) != 1:
        raise _invalid_material("unexpected organizational unit")
    match = _UUID_OU.fullmatch(organizational_units[0].value)
    if match is None:
        raise _invalid_material("unexpected organizational unit")
    try:
        identity_uuid = str(UUID(match.group(1)))
    except ValueError as err:
        raise _invalid_material("unexpected organizational unit") from err
    expected_subject = _samsung_name(
        "*.REMOVED_HOST.com",
        organizational_unit=f"uuid:{identity_uuid}",
    )
    if not _has_exact_name_attributes(certificate.subject, expected_subject):
        raise _invalid_material("unexpected subject")
    return identity_uuid


def _validate_clock(server_date: datetime, local_now: datetime) -> None:
    if (
        server_date.tzinfo is None
        or server_date.utcoffset() is None
        or local_now.tzinfo is None
        or local_now.utcoffset() is None
    ):
        raise BootstrapError(ERROR_INVALID_CLOCK)
    server_utc = server_date.astimezone(UTC)
    local_utc = local_now.astimezone(UTC)
    if (
        abs((local_utc - server_utc).total_seconds())
        > timedelta(days=1).total_seconds()
    ):
        raise BootstrapError(ERROR_INVALID_CLOCK)


def _calendar_expiry(anchor: datetime) -> datetime:
    try:
        return anchor.replace(year=anchor.year + 10)
    except ValueError:
        return anchor.replace(year=anchor.year + 10, day=28)


def _sign_sha1(
    builder: x509.CertificateBuilder, signing_key: rsa.RSAPrivateKey
) -> x509.Certificate:
    """Apply the device-required legacy SHA-1 signature to a built certificate."""
    provisional = builder.sign(signing_key, hashes.SHA256())
    openssl_certificate = crypto.load_certificate(
        crypto.FILETYPE_ASN1,
        provisional.public_bytes(serialization.Encoding.DER),
    )
    openssl_key = crypto.load_privatekey(
        crypto.FILETYPE_PEM,
        signing_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    openssl_certificate.sign(openssl_key, "sha1")
    certificate = x509.load_der_x509_certificate(
        crypto.dump_certificate(crypto.FILETYPE_ASN1, openssl_certificate)
    )
    if not isinstance(certificate.signature_hash_algorithm, hashes.SHA1):
        raise _invalid_material()
    signing_key.public_key().verify(
        certificate.signature,
        certificate.tbs_certificate_bytes,
        padding.PKCS1v15(),
        hashes.SHA1(),
    )
    return certificate


def _mint_credentials(
    material: BundleMaterial, identity_uuid: str, server_date: datetime
) -> Credentials:
    anchor = server_date.astimezone(UTC)
    not_before = anchor - timedelta(minutes=5)
    not_after = _calendar_expiry(anchor)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    uuid_ou = f"uuid:{identity_uuid}"
    uuid_cn = f"urn:uuid:{identity_uuid}"
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Samsung Electronics"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, uuid_ou),
            x509.NameAttribute(NameOID.COMMON_NAME, uuid_cn),
        ]
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(material.signing_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage(
                [
                    ExtendedKeyUsageOID.CLIENT_AUTH,
                    ExtendedKeyUsageOID.SERVER_AUTH,
                    _OCF_CLIENT_OID,
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(f"urn:uuid:{identity_uuid}"),
                    x509.UniformResourceIdentifier(f"uri:uuid:{identity_uuid}"),
                    x509.UniformResourceIdentifier(f"uuid:{identity_uuid}"),
                    x509.DNSName(identity_uuid),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.UnrecognizedExtension(_ROLE_OID, _ROLE_VALUE), critical=False
        )
    )
    certificate = _sign_sha1(builder, material.signing_key)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    chain_pem = certificate.public_bytes(serialization.Encoding.PEM) + b"".join(
        chain_certificate.public_bytes(serialization.Encoding.PEM)
        for chain_certificate in material.public_chain
    )
    return Credentials(
        client_key_pem=key_pem,
        client_chain_pem=chain_pem.decode("ascii"),
        not_before=not_before.isoformat(),
        not_after=not_after.isoformat(),
    )


def create_credentials(
    inputs: BootstrapInputs,
    *,
    pins: BootstrapPins = PRODUCTION_PINS,
) -> Credentials:
    """Mint RSA-2048/SHA-1 leaf anchored to authenticated server Date."""
    material = validate_bundle(inputs.bundle_bytes, pins=pins)
    identity_uuid = validate_identity_certificate(inputs.identity_der, pins=pins)
    _validate_clock(inputs.server_date, inputs.local_now)
    try:
        return _mint_credentials(material, identity_uuid, inputs.server_date)
    except Exception as err:
        raise _invalid_material() from err


def _parse_http_date(value: str | None) -> datetime:
    if value is None:
        raise _invalid_material()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as err:
        raise _invalid_material() from err
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid_material()
    return parsed.astimezone(UTC)


async def async_fetch_bundle(
    session: aiohttp.ClientSession,
) -> tuple[bytes, datetime]:
    """Fetch via normal PKI, require HTTP Date, size <= 64 KiB, and status 200."""
    try:
        async with asyncio.timeout(HTTPS_TIMEOUT):
            async with session.get(BUNDLE_URL) as response:
                if response.status != 200:
                    raise BootstrapError(ERROR_UNAVAILABLE)
                server_date = _parse_http_date(response.headers.get("Date"))
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.content.iter_chunked(16 * 1024):
                    size += len(chunk)
                    if size > _MAX_BUNDLE_SIZE:
                        raise _invalid_material()
                    chunks.append(chunk)
                return b"".join(chunks), server_date
    except BootstrapError:
        raise
    except Exception as err:
        raise BootstrapError(ERROR_UNAVAILABLE) from err


def _fetch_identity_der() -> bytes:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection(
        (SAMSUNG_IDENTITY_HOST, 443), timeout=HTTPS_TIMEOUT
    ) as raw_socket:
        with context.wrap_socket(
            raw_socket, server_hostname=SAMSUNG_IDENTITY_HOST
        ) as tls_socket:
            tls_socket.settimeout(HTTPS_TIMEOUT)
            der = tls_socket.getpeercert(binary_form=True)
    if not isinstance(der, bytes) or not der:
        raise ValueError
    return der


async def async_fetch_identity_der(hass: HomeAssistant) -> bytes:
    """Fetch untrusted TLS leaf in executor; validation happens before use."""
    try:
        async with asyncio.timeout(HTTPS_TIMEOUT):
            return await hass.async_add_executor_job(_fetch_identity_der)
    except Exception as err:
        raise BootstrapError(ERROR_UNAVAILABLE) from err


async def async_bootstrap_credentials(hass: HomeAssistant) -> Credentials:
    """Run both bounded fetches and CPU certificate work in the executor."""
    session = async_get_clientsession(hass)
    bundle_bytes, server_date = await async_fetch_bundle(session)
    identity_der = await async_fetch_identity_der(hass)
    inputs = BootstrapInputs(
        bundle_bytes=bundle_bytes,
        identity_der=identity_der,
        server_date=server_date,
        local_now=dt_util.utcnow(),
    )
    try:
        return await hass.async_add_executor_job(create_credentials, inputs)
    except BootstrapError:
        raise
    except Exception as err:
        raise _invalid_material() from err
