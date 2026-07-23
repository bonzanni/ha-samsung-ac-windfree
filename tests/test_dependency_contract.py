from __future__ import annotations

import ssl
import threading
from importlib.metadata import version
from typing import TYPE_CHECKING

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
from smartthings_local.protocol.dtls_session import (
    DtlsCoapSession,
    _load_pem_chain,
)

from custom_components.samsung_ac_windfree.const import RATE_LIMIT_RPS
from custom_components.samsung_ac_windfree.models import Credentials
from custom_components.samsung_ac_windfree.transport import WindFreeTransport

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant


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


async def test_adapter_default_real_session_contract_and_thread_lifecycle(
    hass: HomeAssistant,
    credentials: Credentials,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    executor_threads: list[int] = []
    lifecycle_calls: list[tuple[str, int]] = []
    reader_stop = threading.Event()
    real_add_executor_job = hass.async_add_executor_job

    def recording_executor_job(
        target: Callable[..., object],
        *args: object,
    ):
        def invoke() -> object:
            executor_threads.append(threading.get_ident())
            return target(*args)

        return real_add_executor_job(invoke)

    def connect(session: DtlsCoapSession) -> None:
        lifecycle_calls.append(("connect", threading.get_ident()))
        assert session.cert_path is None
        assert session.key_path is None
        assert session.cert_pem == credentials.client_chain_pem
        assert session.key_pem == credentials.client_key_pem
        assert session.port == 49154
        assert session._min_req_interval == 1.0 / RATE_LIMIT_RPS

    def start_reader(session: DtlsCoapSession) -> None:
        lifecycle_calls.append(("start_reader", threading.get_ident()))
        session._reader_thread = threading.Thread(
            target=reader_stop.wait,
            daemon=True,
            name="real-contract-reader",
        )
        session._reader_thread.start()

    def close(session: DtlsCoapSession) -> None:
        lifecycle_calls.append(("close", threading.get_ident()))
        reader_stop.set()

    def join(session: DtlsCoapSession) -> None:
        lifecycle_calls.append(("join", threading.get_ident()))
        assert session._reader_thread is not None
        session._reader_thread.join()

    monkeypatch.setattr(hass, "async_add_executor_job", recording_executor_job)
    monkeypatch.setattr(DtlsCoapSession, "connect", connect)
    monkeypatch.setattr(DtlsCoapSession, "start_reader", start_reader)
    monkeypatch.setattr(DtlsCoapSession, "close", close)
    monkeypatch.setattr(DtlsCoapSession, "join", join)

    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        handshake_timeout=3.25,
    )
    await transport.async_connect()
    session = transport._session

    assert isinstance(session, DtlsCoapSession)
    assert session.HANDSHAKE_TIMEOUT_S == 3.25
    assert executor_threads
    assert all(thread != loop_thread for thread in executor_threads)
    assert [name for name, _thread in lifecycle_calls] == [
        "connect",
        "start_reader",
    ]
    assert session._reader_thread is not None
    assert session._reader_thread.is_alive()

    await transport.async_close()

    assert [name for name, _thread in lifecycle_calls] == [
        "connect",
        "start_reader",
        "close",
        "join",
    ]
    assert all(thread != loop_thread for _name, thread in lifecycle_calls)
    assert not session._reader_thread.is_alive()
