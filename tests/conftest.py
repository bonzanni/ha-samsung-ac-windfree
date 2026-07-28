from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import cbor2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from OpenSSL import crypto

from custom_components.samsung_ac_windfree import device as device_module
from custom_components.samsung_ac_windfree.models import Credentials
from tests.resource_limits import apply_address_space_limit

# Protect every Linux test invocation, including local runs outside CI.
# Set PYTEST_RLIMIT_AS_GB=0 only for deliberate memory profiling.
apply_address_space_limit()

_NOT_BEFORE = datetime(2020, 1, 1, tzinfo=UTC)
_NOT_AFTER = datetime(2040, 1, 1, tzinfo=UTC)


def _self_signed_sha1(
    key: rsa.RSAPrivateKey,
    common_name: str,
) -> x509.Certificate:
    """Build a SHA-1 self-signed certificate.

    SHA-1 is deliberate, not an oversight: test_dependency_contract asserts that
    the DTLS dependency loads a legacy SHA-1 chain in memory without changing
    global TLS policy. A SHA-256 fixture would still pass that test while
    silently ending the coverage its name claims.
    """

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    # A fixed, wide window: several tests pin their own clock, and a validity
    # range derived from real "now" would land outside it.
    starts = datetime(2020, 1, 1, tzinfo=UTC)
    expires = datetime(2030, 1, 1, tzinfo=UTC)
    provisional = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(starts)
        .not_valid_after(expires)
        .sign(key, hashes.SHA256())
    )
    # cryptography refuses to sign with SHA-1, so re-sign through pyOpenSSL the
    # way the device's own chain is signed.
    legacy = crypto.load_certificate(
        crypto.FILETYPE_ASN1, provisional.public_bytes(serialization.Encoding.DER)
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    legacy.sign(crypto.load_privatekey(crypto.FILETYPE_PEM, key_pem), "sha1")
    return x509.load_der_x509_certificate(
        crypto.dump_certificate(crypto.FILETYPE_ASN1, legacy)
    )


@pytest.fixture(scope="session")
def credentials() -> Credentials:
    """A synthetic local client credential.

    Generated here rather than minted: the integration no longer contains the
    minting code, and nothing in this repository should reproduce it.
    """

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate = _self_signed_sha1(key, "windfree-test-client")
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    chain_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
    return Credentials(
        client_key_pem=key_pem,
        client_chain_pem=chain_pem,
        not_before=certificate.not_valid_before_utc.isoformat(),
        not_after=certificate.not_valid_after_utc.isoformat(),
    )


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
    encoded = {
        path: cbor2.dumps(representation)
        for path, representation in resource_representations.items()
        if path != "/device/0"
    }
    directory_resources = {
        **resource_representations["/device/0"],
        **{
            path: representation
            for path, representation in resource_representations.items()
            if path not in {"/oic/d", "/oic/p", "/device/0"}
        },
    }
    for index in range(39 - len(directory_resources)):
        directory_resources[f"/sanitized/optional/{index}"] = {}
    encoded["/device/0"] = cbor2.dumps(
        [
            {
                "rt": ["x.com.samsung.devcol", "oic.wk.col"],
                "if": ["oic.if.baseline", "oic.if.ll", "oic.if.b"],
            },
            *[
                {"href": path, "rep": representation}
                for path, representation in directory_resources.items()
            ],
        ]
    )
    return encoded


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


@pytest.fixture(autouse=True)
def use_sanitized_unit_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep synthetic identity fixtures distinct from the supported live unit."""

    identity = json.loads(
        (Path(__file__).parent / "fixtures" / "device_identity.json").read_text()
    )
    model_number = identity["device_0"]["/information/vs/0"][
        "x.com.samsung.da.modelNum"
    ]
    monkeypatch.setattr(
        device_module,
        "SUPPORTED_UNIT_FINGERPRINT_SHA256",
        hashlib.sha256(model_number.encode("utf-8")).hexdigest(),
    )
