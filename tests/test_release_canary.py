from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "check_samsung_pins.py"


@pytest.fixture
def canary() -> ModuleType:
    spec = importlib.util.spec_from_file_location("windfree_pin_canary", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_canary_loads_exact_release_constants_without_home_assistant(canary) -> None:
    constants = canary._load_release_constants()
    assert constants.keys() == canary._REQUIRED_CONSTANTS
    assert constants["BUNDLE_URL"].startswith("https://")
    assert constants["HTTPS_TIMEOUT"] > 0


def test_canary_accepts_matching_mocked_bundle_leaf_and_spki(
    canary, monkeypatch, capsys
) -> None:
    bundle = b"reviewed bundle"
    leaf = b"reviewed leaf"
    spki = b"reviewed spki"
    constants = {
        "BUNDLE_URL": "https://public.example/bundle",
        "HTTPS_TIMEOUT": 1.0,
        "SAMSUNG_IDENTITY_HOST": "identity.example",
        "BUNDLE_SHA256": hashlib.sha256(bundle).hexdigest(),
        "SAMSUNG_IDENTITY_LEAF_SHA256": hashlib.sha256(leaf).hexdigest(),
        "SAMSUNG_IDENTITY_SPKI_SHA256": hashlib.sha256(spki).hexdigest(),
    }
    certificate = MagicMock()
    certificate.public_key.return_value.public_bytes.return_value = spki
    monkeypatch.setattr(canary, "_load_release_constants", lambda: constants)
    monkeypatch.setattr(canary, "_fetch_bundle", lambda _url, _timeout: bundle)
    monkeypatch.setattr(canary, "_fetch_identity_leaf", lambda _host, _timeout: leaf)
    monkeypatch.setattr(
        canary.x509, "load_der_x509_certificate", lambda _leaf: certificate
    )

    assert canary.main() == 0
    assert capsys.readouterr().out == "Samsung public pins match\n"


def test_canary_reports_mismatch_without_printing_hashes(
    canary, monkeypatch, capsys
) -> None:
    bundle = b"changed bundle"
    leaf = b"reviewed leaf"
    spki = b"reviewed spki"
    constants = {
        "BUNDLE_URL": "https://public.example/bundle",
        "HTTPS_TIMEOUT": 1.0,
        "SAMSUNG_IDENTITY_HOST": "identity.example",
        "BUNDLE_SHA256": "0" * 64,
        "SAMSUNG_IDENTITY_LEAF_SHA256": hashlib.sha256(leaf).hexdigest(),
        "SAMSUNG_IDENTITY_SPKI_SHA256": hashlib.sha256(spki).hexdigest(),
    }
    certificate = MagicMock()
    certificate.public_key.return_value.public_bytes.return_value = spki
    monkeypatch.setattr(canary, "_load_release_constants", lambda: constants)
    monkeypatch.setattr(canary, "_fetch_bundle", lambda _url, _timeout: bundle)
    monkeypatch.setattr(canary, "_fetch_identity_leaf", lambda _host, _timeout: leaf)
    monkeypatch.setattr(
        canary.x509, "load_der_x509_certificate", lambda _leaf: certificate
    )

    assert canary.main() == 1
    output = capsys.readouterr().out
    assert output == "Samsung public pin mismatch: bundle\n"
    assert constants["BUNDLE_SHA256"] not in output
    assert hashlib.sha256(bundle).hexdigest() not in output


def test_canary_rejects_oversized_bundle(canary, monkeypatch) -> None:
    response = MagicMock()
    response.__enter__.return_value = SimpleNamespace(
        status=200,
        url="https://public.example/bundle",
        read=lambda _size: b"x" * (canary.MAX_BUNDLE_SIZE + 1),
    )
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr(canary.urllib.request, "build_opener", lambda *_args: opener)

    with pytest.raises(RuntimeError, match="exceeded size limit"):
        canary._fetch_bundle("https://public.example/bundle", 1.0)
