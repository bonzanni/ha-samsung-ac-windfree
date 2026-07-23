from __future__ import annotations

import ssl
from importlib.metadata import version

import pytest
from OpenSSL import SSL
from smartthings_local.protocol.coap import (
    ACCEPT,
    BLOCK2,
    CF_CBOR,
    OBSERVE,
    OBSERVE_REGISTER,
    build_coap,
    parse_coap,
)
from smartthings_local.protocol.dtls_session import _load_pem_chain

from custom_components.samsung_ac_windfree.models import Credentials


# smartthings-local 0.1.0 passes legacy pyOpenSSL objects in this private loader.
# Keep the waiver on this real-dependency contract test; production stays unchanged.
@pytest.mark.filterwarnings(
    "ignore:Passing pyOpenSSL X509 objects is deprecated.*:"
    "DeprecationWarning:OpenSSL\\.SSL"
)
@pytest.mark.filterwarnings(
    "ignore:Passing pyOpenSSL PKey objects is deprecated.*:"
    "DeprecationWarning:OpenSSL\\.SSL"
)
def test_real_dependency_loads_sha1_chain_without_global_tls_change(
    credentials: Credentials,
) -> None:
    assert version("smartthings-local") == "0.1.0"
    before = ssl.create_default_context().security_level
    ctx = SSL.Context(SSL.DTLS_METHOD)
    ctx.set_cipher_list(b"ECDHE-ECDSA-AES128-GCM-SHA256:@SECLEVEL=0")
    _load_pem_chain(
        ctx,
        credentials.client_chain_pem,
        credentials.client_key_pem,
    )
    assert ssl.create_default_context().security_level == before


def test_real_codec_preserves_observe_and_block2_options() -> None:
    packet = build_coap(
        0,
        1,
        0x1234,
        b"\x41",
        [
            (OBSERVE, OBSERVE_REGISTER),
            (ACCEPT, CF_CBOR),
            (BLOCK2, b""),
        ],
    )
    _, _, _, token, options, _ = parse_coap(packet)
    assert token == b"\x41"
    assert (OBSERVE, OBSERVE_REGISTER) in options
    assert (BLOCK2, b"") in options
