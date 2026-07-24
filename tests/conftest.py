from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import cbor2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

from custom_components.samsung_ac_windfree.bootstrap import (
    BootstrapInputs,
    BootstrapPins,
    create_credentials,
)
from custom_components.samsung_ac_windfree.models import Credentials
from tests.resource_limits import apply_address_space_limit

# Protect every Linux test invocation, including local runs outside CI.
# Set PYTEST_RLIMIT_AS_GB=0 only for deliberate memory profiling.
apply_address_space_limit()

_NOT_BEFORE = datetime(2020, 1, 1, tzinfo=UTC)
_NOT_AFTER = datetime(2040, 1, 1, tzinfo=UTC)


def _bundle_name(common_name: str, *, email: str | None = None) -> x509.Name:
    attributes = [
        x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Samsung Electronics"),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ]
    if email is not None:
        attributes.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email))
    return x509.Name(attributes)


def _ca_certificate(
    subject: x509.Name,
    issuer: x509.Name,
    public_key: rsa.RSAPublicKey,
    signer_key: rsa.RSAPrivateKey,
    *,
    rsa_padding: padding.PSS | padding.PKCS1v15 | None = None,
) -> x509.Certificate:
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOT_BEFORE)
        .not_valid_after(_NOT_AFTER)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(signer_key, hashes.SHA256(), rsa_padding=rsa_padding)
    )


@dataclass(frozen=True, slots=True)
class SyntheticBootstrapMaterial:
    signing_key: rsa.RSAPrivateKey
    signing_certificate: x509.Certificate
    remote_key: rsa.RSAPrivateKey
    remote_certificate: x509.Certificate
    ceca_certificate: x509.Certificate
    root_certificate: x509.Certificate
    identity_key: ec.EllipticCurvePrivateKey
    identity_issuer_key: ec.EllipticCurvePrivateKey
    identity_issuer: x509.Name
    identity_uuid: str

    @property
    def certificates(self) -> tuple[x509.Certificate, ...]:
        return (
            self.signing_certificate,
            self.remote_certificate,
            self.ceca_certificate,
            self.root_certificate,
        )

    def bundle(
        self,
        *,
        key: rsa.RSAPrivateKey | None = None,
        certificates: tuple[x509.Certificate, ...] | None = None,
    ) -> bytes:
        private_key = key or self.signing_key
        certs = certificates or self.certificates
        return private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ) + b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in certs)

    def identity_der(
        self,
        *,
        common_name: str = "*.REMOVED_HOST.com",
        organizational_unit: str | None = None,
        issuer: x509.Name | None = None,
        public_key: ec.EllipticCurvePublicKey | None = None,
        country: str = "KR",
        organization: str = "Samsung Electronics",
        additional_organizational_unit: str | None = None,
        extra_subject_attribute: x509.NameAttribute | None = None,
    ) -> bytes:
        ou = organizational_unit or f"uuid:{self.identity_uuid}"
        subject_attributes = [
            x509.NameAttribute(NameOID.COUNTRY_NAME, country),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, ou),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
        if additional_organizational_unit is not None:
            subject_attributes.append(
                x509.NameAttribute(
                    NameOID.ORGANIZATIONAL_UNIT_NAME,
                    additional_organizational_unit,
                )
            )
        if extra_subject_attribute is not None:
            subject_attributes.append(extra_subject_attribute)
        subject = x509.Name(subject_attributes)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer or self.identity_issuer)
            .public_key(public_key or self.identity_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_NOT_BEFORE)
            .not_valid_after(_NOT_AFTER)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .sign(self.identity_issuer_key, hashes.SHA256())
        )
        return certificate.public_bytes(serialization.Encoding.DER)

    def invalid_signing_certificate(self) -> x509.Certificate:
        unrelated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return _ca_certificate(
            self.signing_certificate.subject,
            self.remote_certificate.subject,
            self.signing_key.public_key(),
            unrelated_key,
        )

    def replacement_signing_certificate(
        self,
        *,
        subject: x509.Name | None = None,
        issuer: x509.Name | None = None,
        rsa_padding: padding.PSS | padding.PKCS1v15 | None = None,
    ) -> x509.Certificate:
        return _ca_certificate(
            subject or self.signing_certificate.subject,
            issuer or self.remote_certificate.subject,
            self.signing_key.public_key(),
            self.remote_key,
            rsa_padding=rsa_padding,
        )


@pytest.fixture(scope="session")
def synthetic_bootstrap_material() -> SyntheticBootstrapMaterial:
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ceca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    remote_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    root_name = _bundle_name("ROOTCA")
    ceca_name = _bundle_name("CECA")
    remote_name = _bundle_name("RemoteAccessCA(CE)")
    signing_name = _bundle_name("REMOVED_IDENTITY", email="REMOVED_IDENTITY")

    root_certificate = _ca_certificate(
        root_name, root_name, root_key.public_key(), root_key
    )
    ceca_certificate = _ca_certificate(
        ceca_name, root_name, ceca_key.public_key(), root_key
    )
    remote_certificate = _ca_certificate(
        remote_name, ceca_name, remote_key.public_key(), ceca_key
    )
    signing_certificate = _ca_certificate(
        signing_name, remote_name, signing_key.public_key(), remote_key
    )

    identity_key = ec.generate_private_key(ec.SECP256R1())
    identity_issuer_key = ec.generate_private_key(ec.SECP256R1())
    identity_issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Samsung Electronics"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "OCF Server SubCA"),
            x509.NameAttribute(
                NameOID.COMMON_NAME, "Samsung Electronics OCF Server SubCA"
            ),
        ]
    )
    return SyntheticBootstrapMaterial(
        signing_key=signing_key,
        signing_certificate=signing_certificate,
        remote_key=remote_key,
        remote_certificate=remote_certificate,
        ceca_certificate=ceca_certificate,
        root_certificate=root_certificate,
        identity_key=identity_key,
        identity_issuer_key=identity_issuer_key,
        identity_issuer=identity_issuer,
        identity_uuid=str(uuid4()),
    )


def _pins_for(
    material: SyntheticBootstrapMaterial,
    bundle: bytes,
    identity_der: bytes,
) -> BootstrapPins:
    identity_certificate = x509.load_der_x509_certificate(identity_der)
    identity_spki = identity_certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return BootstrapPins(
        bundle_sha256=hashlib.sha256(bundle).hexdigest(),
        signing_sha256=material.signing_certificate.fingerprint(hashes.SHA256()).hex(),
        identity_leaf_sha256=hashlib.sha256(identity_der).hexdigest(),
        identity_spki_sha256=hashlib.sha256(identity_spki).hexdigest(),
    )


@pytest.fixture(scope="session")
def bootstrap_inputs(
    synthetic_bootstrap_material: SyntheticBootstrapMaterial,
) -> BootstrapInputs:
    bundle = synthetic_bootstrap_material.bundle()
    identity_der = synthetic_bootstrap_material.identity_der()
    return BootstrapInputs(
        bundle_bytes=bundle,
        identity_der=identity_der,
        server_date=datetime(2026, 7, 23, tzinfo=UTC),
        local_now=datetime(2026, 7, 23, 12, tzinfo=UTC),
    )


@pytest.fixture(scope="session")
def bootstrap_pins(
    synthetic_bootstrap_material: SyntheticBootstrapMaterial,
    bootstrap_inputs: BootstrapInputs,
) -> BootstrapPins:
    return _pins_for(
        synthetic_bootstrap_material,
        bootstrap_inputs.bundle_bytes,
        bootstrap_inputs.identity_der,
    )


@pytest.fixture
def pins_for(synthetic_bootstrap_material: SyntheticBootstrapMaterial):
    def build(bundle: bytes, identity_der: bytes) -> BootstrapPins:
        return _pins_for(synthetic_bootstrap_material, bundle, identity_der)

    return build


@pytest.fixture(scope="session")
def credentials(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
) -> Credentials:
    return create_credentials(bootstrap_inputs, pins=bootstrap_pins)


@pytest.fixture
def resource_representations() -> dict[str, dict[str, object]]:
    fixtures = Path(__file__).parent / "fixtures"
    identity = json.loads((fixtures / "device_identity.json").read_text())
    state = json.loads((fixtures / "device_state.json").read_text())
    return {
        "/oic/d": identity["oic_d"],
        "/oic/p": identity["oic_p"],
        "/device/0": identity["device_0"],
        **state,
    }


@pytest.fixture
def encoded_resources(
    resource_representations: dict[str, dict[str, object]],
) -> dict[str, bytes]:
    return {
        path: cbor2.dumps(representation)
        for path, representation in resource_representations.items()
    }


class FakeSession:
    """Synthetic implementation of the pinned blocking session contract."""

    def __init__(self, encoded_resources: dict[str, bytes]) -> None:
        self.encoded_resources = encoded_resources
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.call_threads: dict[str, list[int]] = {}
        self.on_notification = None
        self.handshake_timeout: float | None = None
        self.connect_error: Exception | None = None
        self.start_reader_error: Exception | None = None
        self.get_error: Exception | None = None
        self.post_error: Exception | None = None
        self.subscribe_error: Exception | None = None
        self.close_error: Exception | None = None
        self.join_error: Exception | None = None
        self.get_code = 69
        self.post_code = 68
        self.post_response = b""
        self.active_observations: set[tuple[str, ...]] = set()

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        self.call_threads.setdefault(name, []).append(threading.get_ident())

    def connect(self) -> None:
        self._record("connect")
        if self.connect_error is not None:
            raise self.connect_error

    def start_reader(self) -> None:
        self._record("start_reader")
        if self.start_reader_error is not None:
            raise self.start_reader_error

    def get(self, path: tuple[str, ...]) -> tuple[int, bytes]:
        self._record("get", path)
        if self.get_error is not None:
            raise self.get_error
        href = "/" + "/".join(path)
        return self.get_code, self.encoded_resources[href]

    def post(self, path: tuple[str, ...], payload: bytes) -> tuple[int, bytes]:
        self._record("post", path, payload)
        if self.post_error is not None:
            raise self.post_error
        return self.post_code, self.post_response

    def subscribe(self, path: tuple[str, ...]) -> bytes:
        self._record("subscribe", path)
        if self.subscribe_error is not None:
            raise self.subscribe_error
        self.active_observations.add(path)
        return b"\x41"

    def close(self) -> None:
        self._record("close")
        self.active_observations.clear()
        if self.close_error is not None:
            raise self.close_error

    def join(self) -> None:
        self._record("join")
        if self.join_error is not None:
            raise self.join_error

    def emit(self, path: str, payload: bytes) -> None:
        assert self.on_notification is not None
        self.on_notification(path, payload)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
