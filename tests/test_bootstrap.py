from __future__ import annotations

import asyncio
import hashlib
from dataclasses import fields, replace
from datetime import UTC, datetime
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

from custom_components.samsung_ac_windfree import bootstrap
from custom_components.samsung_ac_windfree.bootstrap import (
    PRODUCTION_PINS,
    BootstrapInputs,
    BootstrapPins,
    async_bootstrap_credentials,
    async_fetch_bundle,
    async_fetch_identity_der,
    create_credentials,
    validate_bundle,
    validate_identity_certificate,
)
from custom_components.samsung_ac_windfree.models import BootstrapError

ROLE_OID = ObjectIdentifier("1.3.6.1.4.1.51414.1.3")
OCF_CLIENT_OID = ObjectIdentifier("1.3.6.1.4.1.51414.0.1.2")
ROLE_VALUE = b"\x0c\x10samsung.role.hub"


def test_wrong_bundle_digest_fails_before_parsing(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
) -> None:
    with pytest.raises(
        BootstrapError, match=r"bootstrap_pin_mismatch.*bootstrap material changed"
    ):
        create_credentials(
            replace(
                bootstrap_inputs,
                bundle_bytes=b"not-the-pinned-bundle",
            ),
            pins=replace(bootstrap_pins, bundle_sha256="00" * 32),
        )


def test_clock_is_checked_but_server_date_anchors_validity(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
) -> None:
    inputs = replace(
        bootstrap_inputs,
        server_date=datetime(2026, 7, 23, tzinfo=UTC),
        local_now=datetime(2026, 7, 24, tzinfo=UTC),
    )
    credentials = create_credentials(inputs, pins=bootstrap_pins)
    assert credentials.not_before == "2026-07-22T23:55:00+00:00"
    assert credentials.not_after == "2036-07-23T00:00:00+00:00"


def test_clock_outside_24_hours_is_rejected(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
) -> None:
    inputs = replace(
        bootstrap_inputs,
        server_date=datetime(2026, 7, 23, tzinfo=UTC),
        local_now=datetime(2026, 7, 25, tzinfo=UTC),
    )
    with pytest.raises(BootstrapError, match=r"invalid_clock.*system clock"):
        create_credentials(inputs, pins=bootstrap_pins)


def test_february_29_expiry_clamps_to_february_28(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
) -> None:
    inputs = replace(
        bootstrap_inputs,
        server_date=datetime(2024, 2, 29, 8, 30, tzinfo=UTC),
        local_now=datetime(2024, 2, 29, 8, 30, tzinfo=UTC),
    )
    credentials = create_credentials(inputs, pins=bootstrap_pins)
    assert credentials.not_after == "2034-02-28T08:30:00+00:00"


@pytest.mark.parametrize(
    ("server_date", "local_now"),
    [
        (datetime(2026, 7, 23), datetime(2026, 7, 23, tzinfo=UTC)),
        (datetime(2026, 7, 23, tzinfo=UTC), datetime(2026, 7, 23)),
    ],
)
def test_naive_clock_values_are_rejected(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
    server_date: datetime,
    local_now: datetime,
) -> None:
    with pytest.raises(BootstrapError, match="invalid_clock"):
        create_credentials(
            replace(
                bootstrap_inputs,
                server_date=server_date,
                local_now=local_now,
            ),
            pins=bootstrap_pins,
        )


def test_result_contains_no_universal_private_key(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
    synthetic_bootstrap_material,
) -> None:
    credentials = create_credentials(bootstrap_inputs, pins=bootstrap_pins)
    serialized_values = [
        getattr(credentials, field.name) for field in fields(credentials)
    ]
    universal_key_pem = synthetic_bootstrap_material.signing_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    assert all("UNIVERSAL TEST KEY" not in value for value in serialized_values)
    assert all(universal_key_pem not in value for value in serialized_values)
    assert bootstrap_inputs.bundle_bytes.decode() not in serialized_values
    assert not hasattr(credentials, "universal_key_pem")


def test_production_pins_are_the_approved_constants() -> None:
    assert PRODUCTION_PINS == BootstrapPins.from_constants()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: b"not pem",
        lambda data: data.replace(b"-----END PRIVATE KEY-----", b""),
        lambda data: (
            data + b"-----BEGIN PUBLIC KEY-----\nAA==\n-----END PUBLIC KEY-----\n"
        ),
        lambda data: data + data[data.index(b"-----BEGIN CERTIFICATE-----") :],
        lambda data: data[: data.rindex(b"-----BEGIN CERTIFICATE-----")],
    ],
    ids=["not-pem", "malformed-key", "extra-pem", "extra-cert", "missing-cert"],
)
def test_bundle_rejects_malformed_extra_or_missing_pem_blocks(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
    mutate,
) -> None:
    data = mutate(bootstrap_inputs.bundle_bytes)
    pins = replace(bootstrap_pins, bundle_sha256=hashlib.sha256(data).hexdigest())
    with pytest.raises(BootstrapError, match="bootstrap_invalid_material"):
        validate_bundle(data, pins=pins)


def test_bundle_rejects_wrong_rsa_modulus(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
    synthetic_bootstrap_material,
) -> None:
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    data = synthetic_bootstrap_material.bundle(key=wrong_key)
    pins = replace(bootstrap_pins, bundle_sha256=hashlib.sha256(data).hexdigest())
    with pytest.raises(BootstrapError, match="bootstrap_invalid_material"):
        validate_bundle(data, pins=pins)


def test_bundle_rejects_invalid_chain_signature(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
    synthetic_bootstrap_material,
) -> None:
    invalid_leaf = synthetic_bootstrap_material.invalid_signing_certificate()
    certificates = (invalid_leaf, *synthetic_bootstrap_material.certificates[1:])
    data = synthetic_bootstrap_material.bundle(certificates=certificates)
    pins = replace(
        bootstrap_pins,
        bundle_sha256=hashlib.sha256(data).hexdigest(),
        signing_sha256=invalid_leaf.fingerprint(hashes.SHA256()).hex(),
    )
    with pytest.raises(BootstrapError, match="bootstrap_invalid_material"):
        validate_bundle(data, pins=pins)


@pytest.mark.parametrize(
    ("replacement", "field"),
    [
        (
            x509.Name(
                [
                    x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
                    x509.NameAttribute(
                        NameOID.ORGANIZATION_NAME, "Samsung Electronics"
                    ),
                    x509.NameAttribute(NameOID.COMMON_NAME, "Unexpected CA"),
                    x509.NameAttribute(NameOID.EMAIL_ADDRESS, "REMOVED_IDENTITY"),
                ]
            ),
            "subject",
        ),
        (
            x509.Name(
                [
                    x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
                    x509.NameAttribute(
                        NameOID.ORGANIZATION_NAME, "Samsung Electronics"
                    ),
                    x509.NameAttribute(NameOID.COMMON_NAME, "Unexpected Issuer"),
                ]
            ),
            "issuer",
        ),
    ],
)
def test_bundle_rejects_exact_subject_or_issuer_chain_mismatch(
    bootstrap_pins: BootstrapPins,
    synthetic_bootstrap_material,
    replacement: x509.Name,
    field: str,
) -> None:
    certificate = synthetic_bootstrap_material.replacement_signing_certificate(
        **{field: replacement}
    )
    data = synthetic_bootstrap_material.bundle(
        certificates=(certificate, *synthetic_bootstrap_material.certificates[1:])
    )
    pins = replace(
        bootstrap_pins,
        bundle_sha256=hashlib.sha256(data).hexdigest(),
        signing_sha256=certificate.fingerprint(hashes.SHA256()).hex(),
    )
    with pytest.raises(BootstrapError, match="bootstrap_invalid_material"):
        validate_bundle(data, pins=pins)


def test_bundle_rejects_wrong_signing_fingerprint(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
) -> None:
    with pytest.raises(BootstrapError, match="bootstrap_pin_mismatch"):
        validate_bundle(
            bootstrap_inputs.bundle_bytes,
            pins=replace(bootstrap_pins, signing_sha256="00" * 32),
        )


@pytest.mark.parametrize(
    "pin_field",
    ["identity_leaf_sha256", "identity_spki_sha256"],
)
def test_identity_rejects_wrong_leaf_or_spki_pin(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
    pin_field: str,
) -> None:
    pins = replace(bootstrap_pins, **{pin_field: "00" * 32})
    with pytest.raises(BootstrapError, match="bootstrap_pin_mismatch"):
        validate_identity_certificate(bootstrap_inputs.identity_der, pins=pins)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"common_name": "REMOVED_API_HOST"}, "common name"),
        ({"organizational_unit": "service:not-a-uuid"}, "organizational unit"),
        ({"country": "NL"}, "subject"),
        ({"organization": "Not Samsung"}, "subject"),
        (
            {
                "issuer": x509.Name(
                    [x509.NameAttribute(NameOID.COMMON_NAME, "Unexpected Issuer")]
                )
            },
            "issuer",
        ),
    ],
)
def test_identity_rejects_issuer_cn_or_ou_mismatch(
    synthetic_bootstrap_material,
    pins_for,
    kwargs: dict[str, Any],
    expected: str,
) -> None:
    identity_der = synthetic_bootstrap_material.identity_der(**kwargs)
    bundle = synthetic_bootstrap_material.bundle()
    pins = pins_for(bundle, identity_der)
    with pytest.raises(BootstrapError, match=f"bootstrap_invalid_material.*{expected}"):
        validate_identity_certificate(identity_der, pins=pins)


def test_identity_returns_only_canonical_uuid(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
    synthetic_bootstrap_material,
) -> None:
    assert (
        validate_identity_certificate(
            bootstrap_inputs.identity_der, pins=bootstrap_pins
        )
        == synthetic_bootstrap_material.identity_uuid
    )


def test_generated_leaf_has_exact_key_signature_subject_and_extensions(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
    synthetic_bootstrap_material,
) -> None:
    credentials = create_credentials(bootstrap_inputs, pins=bootstrap_pins)
    key = serialization.load_pem_private_key(
        credentials.client_key_pem.encode(), password=None
    )
    certificates = x509.load_pem_x509_certificates(
        credentials.client_chain_pem.encode()
    )
    leaf = certificates[0]

    assert isinstance(key, rsa.RSAPrivateKey)
    assert key.key_size == 2048
    assert (
        key.public_key().public_numbers()
        != synthetic_bootstrap_material.signing_key.public_key().public_numbers()
    )
    leaf_public_key = leaf.public_key()
    assert isinstance(leaf_public_key, rsa.RSAPublicKey)
    assert leaf_public_key.public_numbers() == key.public_key().public_numbers()
    assert leaf.version is x509.Version.v3
    assert leaf.signature_hash_algorithm.name == "sha1"
    assert leaf.issuer == synthetic_bootstrap_material.signing_certificate.subject
    assert leaf.subject == x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Samsung Electronics"),
            x509.NameAttribute(
                NameOID.ORGANIZATIONAL_UNIT_NAME,
                f"uuid:{synthetic_bootstrap_material.identity_uuid}",
            ),
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"urn:uuid:{synthetic_bootstrap_material.identity_uuid}",
            ),
        ]
    )
    assert leaf.not_valid_before_utc == datetime(2026, 7, 22, 23, 55, tzinfo=UTC)
    assert leaf.not_valid_after_utc == datetime(2036, 7, 23, tzinfo=UTC)
    assert {extension.oid for extension in leaf.extensions} == {
        x509.ExtensionOID.BASIC_CONSTRAINTS,
        x509.ExtensionOID.KEY_USAGE,
        x509.ExtensionOID.EXTENDED_KEY_USAGE,
        x509.ExtensionOID.SUBJECT_ALTERNATIVE_NAME,
        ROLE_OID,
    }
    assert all(extension.critical is False for extension in leaf.extensions)
    assert leaf.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value == x509.BasicConstraints(ca=False, path_length=None)
    key_usage_extension = leaf.extensions.get_extension_for_class(x509.KeyUsage)
    assert key_usage_extension.value.digital_signature
    assert key_usage_extension.value.key_encipherment
    assert not key_usage_extension.value.content_commitment
    assert not key_usage_extension.value.data_encipherment
    assert not key_usage_extension.value.key_agreement
    assert not key_usage_extension.value.key_cert_sign
    assert not key_usage_extension.value.crl_sign
    eku_extension = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    eku = eku_extension.value
    assert set(eku) == {
        ExtendedKeyUsageOID.CLIENT_AUTH,
        ExtendedKeyUsageOID.SERVER_AUTH,
        OCF_CLIENT_OID,
    }
    role_extension = leaf.extensions.get_extension_for_oid(ROLE_OID)
    assert role_extension.value.value == ROLE_VALUE
    san_extension = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    sans = san_extension.value
    assert set(sans.get_values_for_type(x509.UniformResourceIdentifier)) == {
        f"urn:uuid:{synthetic_bootstrap_material.identity_uuid}",
        f"uri:uuid:{synthetic_bootstrap_material.identity_uuid}",
        f"uuid:{synthetic_bootstrap_material.identity_uuid}",
    }
    assert sans.get_values_for_type(x509.DNSName) == [
        synthetic_bootstrap_material.identity_uuid
    ]
    assert len(certificates) == 5
    signing_public_key = synthetic_bootstrap_material.signing_key.public_key()
    signing_public_key.verify(
        leaf.signature,
        leaf.tbs_certificate_bytes,
        padding.PKCS1v15(),
        hashes.SHA1(),
    )


def test_credential_minting_uses_no_files_or_subprocesses(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    before = tuple(tmp_path.rglob("*"))
    create_credentials(bootstrap_inputs, pins=bootstrap_pins)
    assert tuple(tmp_path.rglob("*")) == before
    assert "tempfile" not in bootstrap.__dict__
    assert "subprocess" not in bootstrap.__dict__


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _size: int):
        yield self._body


class _FakeResponse:
    def __init__(
        self,
        body: bytes = b"bundle",
        *,
        status: int = 200,
        date: str | None = None,
    ) -> None:
        self.status = status
        self.headers = {} if date is None else {"Date": date}
        self.content = _FakeContent(body)


class _ResponseContext:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeResponse:
        return self.response

    async def __aexit__(self, *_args) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    def get(self, _url: str) -> _ResponseContext:
        return _ResponseContext(self.response)


@pytest.mark.parametrize("date", [None, "definitely-not-an-http-date"])
async def test_bundle_fetch_rejects_missing_or_malformed_http_date(date) -> None:
    session = _FakeSession(_FakeResponse(date=date))
    with pytest.raises(BootstrapError, match="bootstrap_invalid_material"):
        await async_fetch_bundle(session)


async def test_bundle_fetch_returns_authenticated_date_and_exact_bytes() -> None:
    server_date = datetime(2026, 7, 23, 12, 30, tzinfo=UTC)
    session = _FakeSession(
        _FakeResponse(body=b"synthetic bundle", date=format_datetime(server_date))
    )
    assert await async_fetch_bundle(session) == (b"synthetic bundle", server_date)


async def test_bundle_fetch_rejects_non_200_and_oversize_response() -> None:
    valid_date = format_datetime(datetime(2026, 7, 23, tzinfo=UTC))
    with pytest.raises(BootstrapError, match="bootstrap_unavailable"):
        await async_fetch_bundle(
            _FakeSession(_FakeResponse(status=503, date=valid_date))
        )
    with pytest.raises(BootstrapError, match="bootstrap_invalid_material"):
        await async_fetch_bundle(
            _FakeSession(_FakeResponse(body=b"x" * 65537, date=valid_date))
        )


async def test_bundle_fetch_timeout_is_sanitized(monkeypatch) -> None:
    class SlowContext:
        async def __aenter__(self):
            await asyncio.sleep(1)

        async def __aexit__(self, *_args):
            return None

    session = SimpleNamespace(get=lambda _url: SlowContext())
    monkeypatch.setattr(bootstrap, "HTTPS_TIMEOUT", 0.001)
    with pytest.raises(BootstrapError, match="bootstrap_unavailable"):
        await async_fetch_bundle(session)


def test_identity_socket_fetch_uses_timeout_sni_and_no_ca_trust(
    monkeypatch,
) -> None:
    calls: dict[str, Any] = {}

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class FakeTlsSocket(FakeSocket):
        def settimeout(self, timeout):
            calls["tls_timeout"] = timeout

        def getpeercert(self, *, binary_form):
            calls["binary_form"] = binary_form
            return b"synthetic der"

    class FakeContext:
        check_hostname = True
        verify_mode = bootstrap.ssl.CERT_REQUIRED

        def __init__(self, protocol):
            calls["protocol"] = protocol

        def wrap_socket(self, raw_socket, *, server_hostname):
            calls["raw_socket"] = raw_socket
            calls["server_hostname"] = server_hostname
            calls["check_hostname"] = self.check_hostname
            calls["verify_mode"] = self.verify_mode
            return FakeTlsSocket()

    raw_socket = FakeSocket()
    monkeypatch.setattr(bootstrap.ssl, "SSLContext", FakeContext)
    monkeypatch.setattr(
        bootstrap.socket,
        "create_connection",
        lambda address, *, timeout: (
            calls.update(address=address, socket_timeout=timeout) or raw_socket
        ),
    )

    assert bootstrap._fetch_identity_der() == b"synthetic der"
    assert calls == {
        "protocol": bootstrap.ssl.PROTOCOL_TLS_CLIENT,
        "address": (bootstrap.SAMSUNG_IDENTITY_HOST, 443),
        "socket_timeout": bootstrap.HTTPS_TIMEOUT,
        "raw_socket": raw_socket,
        "server_hostname": bootstrap.SAMSUNG_IDENTITY_HOST,
        "check_hostname": False,
        "verify_mode": bootstrap.ssl.CERT_NONE,
        "tls_timeout": bootstrap.HTTPS_TIMEOUT,
        "binary_form": True,
    }


async def test_identity_fetch_runs_in_executor_and_sanitizes_all_failures() -> None:
    class FakeHass:
        async def async_add_executor_job(self, target, *_args):
            assert target is bootstrap._fetch_identity_der
            raise RuntimeError("must not escape")

    with pytest.raises(BootstrapError, match="bootstrap_unavailable") as err:
        await async_fetch_identity_der(FakeHass())
    assert "must not escape" not in str(err.value)


async def test_bootstrap_runs_cpu_work_in_executor_and_sanitizes_failure(
    monkeypatch,
    bootstrap_inputs: BootstrapInputs,
) -> None:
    calls: list[Any] = []

    async def fake_bundle(_session):
        return bootstrap_inputs.bundle_bytes, bootstrap_inputs.server_date

    async def fake_identity(_hass):
        return bootstrap_inputs.identity_der

    class FakeHass:
        async def async_add_executor_job(self, target, *args):
            calls.append((target, args))
            raise RuntimeError("must not escape")

    monkeypatch.setattr(bootstrap, "async_fetch_bundle", fake_bundle)
    monkeypatch.setattr(bootstrap, "async_fetch_identity_der", fake_identity)
    monkeypatch.setattr(bootstrap, "async_get_clientsession", lambda _hass: object())

    with pytest.raises(BootstrapError, match="bootstrap_invalid_material") as err:
        await async_bootstrap_credentials(FakeHass())
    assert "must not escape" not in str(err.value)
    assert calls[0][0] is create_credentials
    assert isinstance(calls[0][1][0], BootstrapInputs)


def test_all_bootstrap_errors_are_sanitized_categories() -> None:
    assert bootstrap.ERROR_UNAVAILABLE.startswith("bootstrap_unavailable:")
    assert bootstrap.ERROR_PIN_MISMATCH.startswith("bootstrap_pin_mismatch:")
    assert bootstrap.ERROR_INVALID_CLOCK.startswith("invalid_clock:")
    assert bootstrap.ERROR_INVALID_MATERIAL.startswith("bootstrap_invalid_material:")
