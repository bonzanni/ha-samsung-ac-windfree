"""Report whether Samsung's public bootstrap materials still match their pins."""

from __future__ import annotations

import ast
import hashlib
import socket
import ssl
import urllib.request
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).parents[2]
MAX_BUNDLE_SIZE = 64 * 1024
_REQUIRED_CONSTANTS = frozenset(
    {
        "BUNDLE_SHA256",
        "BUNDLE_URL",
        "HTTPS_TIMEOUT",
        "SAMSUNG_IDENTITY_HOST",
        "SAMSUNG_IDENTITY_LEAF_SHA256",
        "SAMSUNG_IDENTITY_SPKI_SHA256",
    }
)


def _load_release_constants() -> dict[str, str | float]:
    source = (
        ROOT / "custom_components" / "samsung_ac_windfree" / "const.py"
    ).read_text()
    values: dict[str, str | float] = {}
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in _REQUIRED_CONSTANTS
        ):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    missing = _REQUIRED_CONSTANTS - values.keys()
    if missing:
        raise RuntimeError("release pin constants are incomplete")
    return values


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_bundle(url: str, timeout: float) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(url, timeout=timeout) as response:
        if response.status != 200 or response.url != url:
            raise RuntimeError("public bundle endpoint changed")
        bundle = response.read(MAX_BUNDLE_SIZE + 1)
    if len(bundle) > MAX_BUNDLE_SIZE:
        raise RuntimeError("public bundle exceeded size limit")
    return bundle


def _fetch_identity_leaf(host: str, timeout: float) -> bytes:
    # This endpoint intentionally presents Samsung's private trust chain.
    # Authenticity is established by the exact leaf and SPKI pins below.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, 443), timeout=timeout) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=host) as tls_socket:
            leaf = tls_socket.getpeercert(binary_form=True)
    if not leaf:
        raise RuntimeError("Samsung identity endpoint returned no certificate")
    return leaf


def main() -> int:
    constants = _load_release_constants()
    timeout = float(constants["HTTPS_TIMEOUT"])
    bundle = _fetch_bundle(str(constants["BUNDLE_URL"]), timeout)
    leaf = _fetch_identity_leaf(str(constants["SAMSUNG_IDENTITY_HOST"]), timeout)
    certificate = x509.load_der_x509_certificate(leaf)
    spki = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    actual = {
        "bundle": hashlib.sha256(bundle).hexdigest(),
        "identity leaf": hashlib.sha256(leaf).hexdigest(),
        "identity SPKI": hashlib.sha256(spki).hexdigest(),
    }
    expected = {
        "bundle": constants["BUNDLE_SHA256"],
        "identity leaf": constants["SAMSUNG_IDENTITY_LEAF_SHA256"],
        "identity SPKI": constants["SAMSUNG_IDENTITY_SPKI_SHA256"],
    }
    mismatches = [label for label in expected if actual[label] != expected[label]]
    if mismatches:
        print("Samsung public pin mismatch:", ", ".join(mismatches))
        return 1
    print("Samsung public pins match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
