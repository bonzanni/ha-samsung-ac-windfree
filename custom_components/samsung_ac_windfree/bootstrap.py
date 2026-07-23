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
from cryptography.x509.oid import (
    ExtendedKeyUsageOID,
    NameOID,
    ObjectIdentifier,
    SignatureAlgorithmOID,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from OpenSSL import crypto
from yarl import URL

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
_BUNDLE_ENDPOINT = URL(BUNDLE_URL)
_CANONICAL_SHA1_RSA_ALGORITHM = bytes.fromhex("300d06092a864886f70d0101050500")
_SAFE_IDENTITY_ERRORS = frozenset(
    {
        f"{ERROR_INVALID_MATERIAL} (unexpected issuer)",
        f"{ERROR_INVALID_MATERIAL} (unexpected common name)",
        f"{ERROR_INVALID_MATERIAL} (unexpected organizational unit)",
        f"{ERROR_INVALID_MATERIAL} (unexpected subject)",
    }
)


class _DerDecodeError(ValueError):
    """Generated certificate DER was not minimally and safely encoded."""


def _read_der_tlv(
    data: bytes,
    offset: int,
    limit: int,
) -> tuple[int, bytes, int, int]:
    if offset < 0 or offset >= limit or limit > len(data):
        raise _DerDecodeError
    start = offset
    tag = data[offset]
    if tag & 0x1F == 0x1F:
        raise _DerDecodeError
    offset += 1
    if offset >= limit:
        raise _DerDecodeError
    first_length = data[offset]
    offset += 1
    if first_length < 0x80:
        length = first_length
    else:
        length_octets = first_length & 0x7F
        if (
            length_octets == 0
            or length_octets > 4
            or offset + length_octets > limit
            or data[offset] == 0
        ):
            raise _DerDecodeError
        length = int.from_bytes(data[offset : offset + length_octets], "big")
        if length < 0x80:
            raise _DerDecodeError
        offset += length_octets
    end = offset + length
    if end > limit:
        raise _DerDecodeError
    return tag, data[start:end], offset, end


def _parse_certificate_algorithm_identifiers(
    der: bytes,
) -> tuple[bytes, bytes]:
    outer_tag, _outer_raw, outer_content, outer_end = _read_der_tlv(der, 0, len(der))
    if outer_tag != 0x30 or outer_end != len(der):
        raise _DerDecodeError

    tbs_tag, _tbs_raw, tbs_content, tbs_end = _read_der_tlv(
        der, outer_content, outer_end
    )
    if tbs_tag != 0x30:
        raise _DerDecodeError
    outer_algorithm_tag, outer_algorithm, _outer_alg_content, algorithm_end = (
        _read_der_tlv(der, tbs_end, outer_end)
    )
    signature_tag, _signature_raw, _signature_content, signature_end = _read_der_tlv(
        der, algorithm_end, outer_end
    )
    if (
        outer_algorithm_tag != 0x30
        or signature_tag != 0x03
        or signature_end != outer_end
    ):
        raise _DerDecodeError

    cursor = tbs_content
    first_tag, _first_raw, _first_content, first_end = _read_der_tlv(
        der, cursor, tbs_end
    )
    if first_tag == 0xA0:
        cursor = first_end
        serial_tag, _serial_raw, _serial_content, cursor = _read_der_tlv(
            der, cursor, tbs_end
        )
    else:
        serial_tag = first_tag
        cursor = first_end
    if serial_tag != 0x02:
        raise _DerDecodeError
    inner_algorithm_tag, inner_algorithm, _inner_content, _inner_end = _read_der_tlv(
        der, cursor, tbs_end
    )
    if inner_algorithm_tag != 0x30:
        raise _DerDecodeError
    return inner_algorithm, outer_algorithm


def _certificate_algorithm_identifiers(
    der: bytes,
) -> tuple[bytes, bytes]:
    parse_failed = False
    try:
        result = _parse_certificate_algorithm_identifiers(der)
    except IndexError, _DerDecodeError:
        parse_failed = True
    if parse_failed:
        raise _invalid_material()
    return result


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


def _fixed_error_category(
    error: BootstrapError,
    *,
    fallback: str,
) -> str:
    message = str(error)
    if message.startswith("bootstrap_unavailable:"):
        return ERROR_UNAVAILABLE
    if message.startswith("bootstrap_pin_mismatch:"):
        return ERROR_PIN_MISMATCH
    if message.startswith("invalid_clock:"):
        return ERROR_INVALID_CLOCK
    if message.startswith("bootstrap_invalid_material:"):
        return ERROR_INVALID_MATERIAL
    return fallback


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
    parameters = certificate.signature_algorithm_parameters
    if algorithm is None or not isinstance(parameters, (padding.PKCS1v15, padding.PSS)):
        raise _invalid_material()
    issuer_key.verify(
        certificate.signature,
        certificate.tbs_certificate_bytes,
        parameters,
        algorithm,
    )


def _validate_bundle_impl(
    data: bytes,
    *,
    pins: BootstrapPins,
) -> BundleMaterial:
    """Validate exact bytes, one key, four certs, key pair, chain, and REMOVED_IDENTITY."""
    if not _digest_matches(data, pins.bundle_sha256):
        raise BootstrapError(ERROR_PIN_MISMATCH)

    parse_failed = False
    try:
        key_pem, certificate_pems = _parse_exact_pem(data)
        key = serialization.load_pem_private_key(key_pem, password=None)
        certificates = tuple(
            x509.load_pem_x509_certificate(pem) for pem in certificate_pems
        )
    except BootstrapError:
        raise
    except Exception:
        parse_failed = True
    if parse_failed:
        raise _invalid_material()

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

    chain_failed = False
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
    except Exception:
        chain_failed = True
    if chain_failed:
        raise _invalid_material()

    return BundleMaterial(
        signing_key=key,
        signing_certificate=signing_certificate,
        public_chain=certificates,
    )


def _validate_bundle_outcome(
    data: bytes,
    pins: BootstrapPins,
) -> BundleMaterial | str:
    failure_category: str | None = None
    try:
        return _validate_bundle_impl(data, pins=pins)
    except BootstrapError as error:
        failure_category = _fixed_error_category(error, fallback=ERROR_INVALID_MATERIAL)
    except Exception:
        failure_category = ERROR_INVALID_MATERIAL
    return failure_category


def validate_bundle(
    data: bytes,
    *,
    pins: BootstrapPins = PRODUCTION_PINS,
) -> BundleMaterial:
    """Validate exact bytes, one key, four certs, key pair, chain, and REMOVED_IDENTITY."""
    outcome = _validate_bundle_outcome(data, pins)
    data = b""
    pins = PRODUCTION_PINS
    if isinstance(outcome, str):
        category = outcome
        outcome = None
        raise BootstrapError(category)
    return outcome


def _validate_identity_certificate_impl(
    der: bytes,
    *,
    pins: BootstrapPins,
) -> str:
    """Validate leaf/SPKI/issuer/CN and return UUID from the subject OU."""
    if not _digest_matches(der, pins.identity_leaf_sha256):
        raise BootstrapError(ERROR_PIN_MISMATCH)
    parse_failed = False
    try:
        certificate = x509.load_der_x509_certificate(der)
        spki = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except Exception:
        parse_failed = True
    if parse_failed:
        raise _invalid_material()
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
    invalid_uuid = False
    try:
        identity_uuid = str(UUID(match.group(1)))
    except ValueError:
        invalid_uuid = True
    if invalid_uuid:
        raise _invalid_material("unexpected organizational unit")
    expected_subject = _samsung_name(
        "*.REMOVED_HOST.com",
        organizational_unit=organizational_units[0].value,
    )
    if not _has_exact_name_attributes(certificate.subject, expected_subject):
        raise _invalid_material("unexpected subject")
    return identity_uuid


def _identity_outcome(
    der: bytes,
    pins: BootstrapPins,
) -> str:
    failure_category: str | None = None
    try:
        identity_uuid = _validate_identity_certificate_impl(der, pins=pins)
    except BootstrapError as error:
        message = str(error)
        failure_category = (
            message
            if message in _SAFE_IDENTITY_ERRORS
            else _fixed_error_category(error, fallback=ERROR_INVALID_MATERIAL)
        )
    except Exception:
        failure_category = ERROR_INVALID_MATERIAL
    if failure_category is not None:
        return failure_category
    return identity_uuid


def validate_identity_certificate(
    der: bytes,
    *,
    pins: BootstrapPins = PRODUCTION_PINS,
) -> str:
    """Validate leaf/SPKI/issuer/CN and return UUID from the subject OU."""
    outcome = _identity_outcome(der, pins)
    der = b""
    pins = PRODUCTION_PINS
    if outcome.startswith(
        (
            "bootstrap_unavailable:",
            "bootstrap_pin_mismatch:",
            "invalid_clock:",
            "bootstrap_invalid_material:",
        )
    ):
        category = outcome
        outcome = ""
        raise BootstrapError(category)
    return outcome


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


def _resign_with_pyopenssl(
    provisional_der: bytes,
    signing_key_pem: bytes,
) -> bytes:
    """Replace a provisional signature using the in-memory legacy boundary."""
    openssl_certificate = crypto.load_certificate(
        crypto.FILETYPE_ASN1,
        provisional_der,
    )
    openssl_key = crypto.load_privatekey(
        crypto.FILETYPE_PEM,
        signing_key_pem,
    )
    openssl_certificate.sign(openssl_key, "sha1")
    return crypto.dump_certificate(crypto.FILETYPE_ASN1, openssl_certificate)


def _extension_profile(
    certificate: x509.Certificate,
) -> tuple[tuple[str, bool, bytes], ...]:
    return tuple(
        (
            extension.oid.dotted_string,
            extension.critical,
            extension.value.public_bytes(),
        )
        for extension in certificate.extensions
    )


def _has_same_tbs_profile(
    provisional: x509.Certificate,
    certificate: x509.Certificate,
) -> bool:
    provisional_spki = provisional.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    certificate_spki = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return (
        certificate.version == provisional.version
        and certificate.serial_number == provisional.serial_number
        and certificate.subject == provisional.subject
        and certificate.issuer == provisional.issuer
        and certificate.not_valid_before_utc == provisional.not_valid_before_utc
        and certificate.not_valid_after_utc == provisional.not_valid_after_utc
        and hmac.compare_digest(certificate_spki, provisional_spki)
        and _extension_profile(certificate) == _extension_profile(provisional)
    )


def _sign_sha1(
    builder: x509.CertificateBuilder, signing_key: rsa.RSAPrivateKey
) -> x509.Certificate:
    """Apply the device-required legacy SHA-1 signature to a built certificate."""
    provisional = builder.sign(signing_key, hashes.SHA256())
    signing_key_pem = signing_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    certificate_der = _resign_with_pyopenssl(
        provisional.public_bytes(serialization.Encoding.DER),
        signing_key_pem,
    )
    inner_algorithm, outer_algorithm = _certificate_algorithm_identifiers(
        certificate_der
    )
    certificate = x509.load_der_x509_certificate(certificate_der)
    provisional_parameters = provisional.signature_algorithm_parameters
    certificate_parameters = certificate.signature_algorithm_parameters
    if (
        provisional.signature_algorithm_oid != SignatureAlgorithmOID.RSA_WITH_SHA256
        or not isinstance(provisional.signature_hash_algorithm, hashes.SHA256)
        or not isinstance(provisional_parameters, padding.PKCS1v15)
        or not _has_same_tbs_profile(provisional, certificate)
        or inner_algorithm != _CANONICAL_SHA1_RSA_ALGORITHM
        or outer_algorithm != _CANONICAL_SHA1_RSA_ALGORITHM
        or certificate.signature_algorithm_oid != SignatureAlgorithmOID.RSA_WITH_SHA1
        or not isinstance(certificate.signature_hash_algorithm, hashes.SHA1)
        or not isinstance(certificate_parameters, padding.PKCS1v15)
    ):
        raise _invalid_material()
    signing_key.public_key().verify(
        certificate.signature,
        certificate.tbs_certificate_bytes,
        certificate_parameters,
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


def _create_credentials_outcome(
    inputs: BootstrapInputs,
    pins: BootstrapPins,
) -> Credentials | str:
    failure_category: str | None = None
    try:
        material = validate_bundle(inputs.bundle_bytes, pins=pins)
        identity_uuid = validate_identity_certificate(inputs.identity_der, pins=pins)
        _validate_clock(inputs.server_date, inputs.local_now)
        return _mint_credentials(material, identity_uuid, inputs.server_date)
    except BootstrapError as error:
        failure_category = _fixed_error_category(error, fallback=ERROR_INVALID_MATERIAL)
    except Exception:
        failure_category = ERROR_INVALID_MATERIAL
    return failure_category


def create_credentials(
    inputs: BootstrapInputs,
    *,
    pins: BootstrapPins = PRODUCTION_PINS,
) -> Credentials:
    """Mint RSA-2048/SHA-1 leaf anchored to authenticated server Date."""
    outcome = _create_credentials_outcome(inputs, pins)
    del inputs
    pins = PRODUCTION_PINS
    if isinstance(outcome, str):
        category = outcome
        outcome = None
        raise BootstrapError(category)
    return outcome


def _parse_http_date(value: str | None) -> datetime:
    if value is None:
        raise _invalid_material()
    parse_failed = False
    try:
        parsed = parsedate_to_datetime(value)
    except TypeError, ValueError:
        parse_failed = True
    if parse_failed:
        raise _invalid_material()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid_material()
    return parsed.astimezone(UTC)


async def _async_fetch_bundle_outcome(
    session: aiohttp.ClientSession,
) -> tuple[bytes, datetime] | str:
    failure_category: str | None = None
    result: tuple[bytes, datetime] | None = None
    response: aiohttp.ClientResponse | None = None
    response_url: URL | None = None
    server_date: datetime | None = None
    chunks: list[bytes] = []
    chunk = b""
    size = 0
    try:
        async with asyncio.timeout(HTTPS_TIMEOUT):
            async with session.get(BUNDLE_URL, allow_redirects=False) as response:
                response_url = URL(response.url)
                if (
                    response_url != _BUNDLE_ENDPOINT
                    or response_url.scheme != "https"
                    or response_url.host != _BUNDLE_ENDPOINT.host
                    or response.status != 200
                ):
                    raise BootstrapError(ERROR_UNAVAILABLE)
                server_date = _parse_http_date(response.headers.get("Date"))
                async for chunk in response.content.iter_chunked(16 * 1024):
                    size += len(chunk)
                    if size > _MAX_BUNDLE_SIZE:
                        raise _invalid_material()
                    chunks.append(chunk)
                result = b"".join(chunks), server_date
    except asyncio.CancelledError:
        chunks.clear()
        chunk = b""
        result = None
        del session
        del response
        del response_url
        del server_date
        del chunks
        del chunk
        del result
        raise
    except BootstrapError as error:
        failure_category = _fixed_error_category(error, fallback=ERROR_UNAVAILABLE)
    except Exception:
        failure_category = ERROR_UNAVAILABLE
    del session
    del response
    del response_url
    del server_date
    chunks.clear()
    del chunks
    chunk = b""
    del chunk
    result_or_category: tuple[bytes, datetime] | str
    if failure_category is not None:
        result_or_category = failure_category
    elif result is None:
        result_or_category = ERROR_UNAVAILABLE
    else:
        result_or_category = result
    result = None
    return result_or_category


async def async_fetch_bundle(
    session: aiohttp.ClientSession,
) -> tuple[bytes, datetime]:
    """Fetch via normal PKI, require HTTP Date, size <= 64 KiB, and status 200."""
    try:
        outcome = await _async_fetch_bundle_outcome(session)
    except asyncio.CancelledError:
        del session
        raise
    del session
    if isinstance(outcome, str):
        category = outcome
        outcome = None
        raise BootstrapError(category)
    return outcome


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


async def _async_fetch_identity_der_outcome(
    hass: HomeAssistant,
) -> bytes | str:
    failure_category: str | None = None
    result: bytes | None = None
    job = None
    try:
        async with asyncio.timeout(HTTPS_TIMEOUT):
            job = hass.async_add_executor_job(_fetch_identity_der)
            result = await job
    except asyncio.CancelledError:
        del hass
        job = None
        result = None
        del job
        del result
        raise
    except BootstrapError as error:
        failure_category = _fixed_error_category(error, fallback=ERROR_UNAVAILABLE)
    except Exception:
        failure_category = ERROR_UNAVAILABLE
    del hass
    job = None
    del job
    result_or_category: bytes | str
    if failure_category is not None:
        result_or_category = failure_category
    elif result is None:
        result_or_category = ERROR_UNAVAILABLE
    else:
        result_or_category = result
    result = None
    return result_or_category


async def async_fetch_identity_der(hass: HomeAssistant) -> bytes:
    """Fetch untrusted TLS leaf in executor; validation happens before use."""
    try:
        outcome = await _async_fetch_identity_der_outcome(hass)
    except asyncio.CancelledError:
        del hass
        raise
    del hass
    if isinstance(outcome, str):
        category = outcome
        outcome = b""
        raise BootstrapError(category)
    return outcome


async def _async_bootstrap_outcome(
    hass: HomeAssistant,
) -> Credentials | str:
    failure_category: str | None = None
    failure_fallback = ERROR_UNAVAILABLE
    result: Credentials | None = None
    session: aiohttp.ClientSession | None = None
    bundle_bytes = b""
    identity_der = b""
    inputs: BootstrapInputs | None = None
    try:
        session = async_get_clientsession(hass)
        bundle_bytes, server_date = await async_fetch_bundle(session)
        identity_der = await async_fetch_identity_der(hass)
        failure_fallback = ERROR_INVALID_MATERIAL
        inputs = BootstrapInputs(
            bundle_bytes=bundle_bytes,
            identity_der=identity_der,
            server_date=server_date,
            local_now=dt_util.utcnow(),
        )
        result = await hass.async_add_executor_job(create_credentials, inputs)
    except asyncio.CancelledError:
        del hass
        session = None
        bundle_bytes = b""
        identity_der = b""
        inputs = None
        result = None
        raise
    except BootstrapError as error:
        failure_category = _fixed_error_category(error, fallback=failure_fallback)
    except Exception:
        failure_category = failure_fallback
    del hass
    session = None
    bundle_bytes = b""
    identity_der = b""
    inputs = None
    result_or_category: Credentials | str
    if failure_category is not None:
        result_or_category = failure_category
    elif result is None:
        result_or_category = ERROR_INVALID_MATERIAL
    else:
        result_or_category = result
    result = None
    return result_or_category


async def async_bootstrap_credentials(hass: HomeAssistant) -> Credentials:
    """Run both bounded fetches and CPU certificate work in the executor."""
    try:
        outcome = await _async_bootstrap_outcome(hass)
    except asyncio.CancelledError:
        del hass
        raise
    del hass
    if isinstance(outcome, str):
        category = outcome
        outcome = None
        raise BootstrapError(category)
    return outcome
