from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

import cbor2
import pytest

from custom_components.samsung_ac_windfree.const import (
    PROBE_HANDSHAKE_TIMEOUT,
    PROBE_PORTS,
    RATE_LIMIT_RPS,
)
from custom_components.samsung_ac_windfree.models import Credentials
from custom_components.samsung_ac_windfree.transport import (
    TransportError,
    WindFreeTransport,
    async_discover_transport,
)
from tests.conftest import FakeSession

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class RecordingSessionFactory:
    def __init__(self, build: Callable[[int], FakeSession]) -> None:
        self._build = build
        self.calls: list[dict[str, object]] = []
        self.sessions: list[FakeSession] = []

    def __call__(self, **kwargs: object) -> FakeSession:
        self.calls.append(kwargs)
        session = self._build(int(kwargs["port"]))
        self.sessions.append(session)
        return session


async def test_constructor_uses_only_in_memory_pem_and_exact_rate_limit(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    factory = RecordingSessionFactory(lambda _port: FakeSession(encoded_resources))
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        handshake_timeout=3.25,
        session_factory=factory,
    )

    await transport.async_connect()

    assert factory.calls == [
        {
            "host": "192.0.2.10",
            "port": 49154,
            "cert_pem": credentials.client_chain_pem,
            "key_pem": credentials.client_key_pem,
            "rate_limit_rps": RATE_LIMIT_RPS,
        }
    ]
    assert factory.sessions[0].HANDSHAKE_TIMEOUT_S == 3.25


async def test_all_blocking_session_methods_run_only_in_executor(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    loop_thread = threading.get_ident()
    session = FakeSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )

    await transport.async_connect()
    await transport.async_get("/oic/d")
    await transport.async_post("/power/vs/0", {"value": "On"})
    await transport.async_observe(("/power/vs/0",), lambda *_args: None)
    await transport.async_close()

    for method in (
        "connect",
        "start_reader",
        "get",
        "post",
        "subscribe",
        "close",
        "join",
    ):
        assert session.call_threads[method]
        assert all(thread != loop_thread for thread in session.call_threads[method])


async def test_get_decodes_complete_block2_result_and_normalizes_path(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    resource_representations: dict[str, dict[str, object]],
) -> None:
    session = FakeSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    result = await transport.async_get("//device//0/")

    assert result == resource_representations["/device/0"]
    assert [call for call in session.calls if call[0] == "get"] == [
        ("get", (("device", "0"),))
    ]


async def test_post_encodes_cbor_and_requires_changed_acknowledgement(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    session = FakeSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()
    payload = {"x.com.samsung.da.power": "On"}

    await transport.async_post("/power/vs/0", payload)

    post_call = next(call for call in session.calls if call[0] == "post")
    assert post_call[1][0] == ("power", "vs", "0")
    assert cbor2.loads(post_call[1][1]) == payload


@pytest.mark.parametrize(("method", "code"), [("get", 68), ("post", 69)])
async def test_only_exact_coap_success_codes_are_accepted(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    method: str,
    code: int,
) -> None:
    session = FakeSession(encoded_resources)
    setattr(session, f"{method}_code", code)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    with pytest.raises(TransportError) as err:
        if method == "get":
            await transport.async_get("/oic/d")
        else:
            await transport.async_post("/power/vs/0", {"value": "On"})

    assert err.value.coap_code == code


async def test_requests_are_serialized_and_paced_at_two_rps(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    now = 10.0
    delays: list[float] = []
    real_sleep = asyncio.sleep

    def monotonic() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay
        await real_sleep(0)

    monkeypatch.setattr(transport_module.time, "monotonic", monotonic)
    monkeypatch.setattr(transport_module.asyncio, "sleep", sleep)
    session = FakeSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    await transport.async_get("/oic/d")
    await transport.async_post("/power/vs/0", {"value": "On"})

    assert delays == [0.5]


async def test_observe_registers_and_close_deregisters_all_paths(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    session = FakeSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    await transport.async_observe(
        ("/power/vs/0", "/mode/vs/0"),
        lambda *_args: None,
    )

    assert session.active_observations == {
        ("power", "vs", "0"),
        ("mode", "vs", "0"),
    }
    await transport.async_close()
    assert session.active_observations == set()
    assert [name for name, _args in session.calls][-2:] == ["close", "join"]


async def test_notification_crosses_to_loop_decodes_and_adds_generation(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    session = FakeSession(encoded_resources)
    received: list[tuple[int, str, dict[str, object], int]] = []
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        generation=7,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()
    await transport.async_observe(
        ("/power/vs/0",),
        lambda generation, path, body: received.append(
            (generation, path, dict(body), threading.get_ident())
        ),
    )

    await hass.async_add_executor_job(
        session.emit,
        "/power/vs/0",
        encoded_resources["/power/vs/0"],
    )
    await asyncio.sleep(0)

    assert received == [
        (
            7,
            "/power/vs/0",
            {"x.com.samsung.da.power": "Off"},
            threading.get_ident(),
        )
    ]


async def test_old_generation_notification_is_ignored(
    hass: HomeAssistant,
    credentials: Credentials,
) -> None:
    received: list[tuple[str, dict[str, object]]] = []
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        generation=2,
        session_factory=lambda **_kwargs: pytest.fail("must not construct"),
    )
    callback = transport.threadsafe_callback(
        generation=1,
        target=lambda path, body: received.append((path, dict(body))),
    )

    callback("/power/vs/0", cbor2.dumps({"value": "old"}))
    await asyncio.sleep(0)

    assert received == []


async def test_scheduled_notification_is_dropped_after_close(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    session = FakeSession(encoded_resources)
    received: list[object] = []
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()
    callback = transport.threadsafe_callback(
        generation=0,
        target=lambda path, body: received.append((path, body)),
    )

    callback("/power/vs/0", encoded_resources["/power/vs/0"])
    await transport.async_close()
    await asyncio.sleep(0)

    assert received == []


async def test_invalid_notification_is_safely_dropped_without_payload_log(
    hass: HomeAssistant,
    credentials: Credentials,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: pytest.fail("must not construct"),
    )
    callback = transport.threadsafe_callback(
        generation=0,
        target=lambda *_args: pytest.fail("must not deliver"),
    )

    with caplog.at_level(logging.WARNING):
        callback("/contains/device-uuid", b"secret-callback-payload")
        await asyncio.sleep(0)

    assert "secret-callback-payload" not in caplog.text
    assert "device-uuid" not in caplog.text


@pytest.mark.parametrize(
    ("alert_message", "expected"),
    [
        ("tlsv1 alert bad certificate", "bad_certificate"),
        ("fatal alert code 42", "bad_certificate"),
        ("tlsv1 alert unsupported certificate", "unsupported_certificate"),
        ("fatal alert code 43", "unsupported_certificate"),
        ("tlsv1 alert certificate expired", "certificate_expired"),
        ("fatal alert code 45", "certificate_expired"),
        ("tlsv1 alert certificate unknown", "certificate_unknown"),
        ("fatal alert code 46", "certificate_unknown"),
        ("tlsv1 alert unknown ca", "unknown_ca"),
        ("fatal alert code 48", "unknown_ca"),
        ("tlsv1 alert access denied", "access_denied"),
        ("fatal alert code 49", "access_denied"),
    ],
)
async def test_allowed_fatal_alerts_are_extracted_and_sanitized(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    alert_message: str,
    expected: str,
) -> None:
    session = FakeSession(encoded_resources)
    session.connect_error = ConnectionError(
        f"failed at 192.0.2.10 with {alert_message} and secret-key"
    )
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )

    with pytest.raises(TransportError) as err:
        await transport.async_connect()

    assert err.value.fatal_alert == expected
    assert "192.0.2.10" not in str(err.value)
    assert "secret-key" not in str(err.value)
    assert err.value.__context__ is None


async def test_unknown_alert_remains_transient(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    session = FakeSession(encoded_resources)
    session.connect_error = ConnectionError("tlsv1 alert handshake failure")
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )

    with pytest.raises(TransportError) as err:
        await transport.async_connect()

    assert err.value.fatal_alert is None


async def test_partial_connect_and_reader_failures_are_fully_cleaned_up(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    connect_failure = FakeSession(encoded_resources)
    connect_failure.connect_error = OSError("sensitive host")
    reader_failure = FakeSession(encoded_resources)
    reader_failure.start_reader_error = RuntimeError("sensitive key")

    for session in (connect_failure, reader_failure):
        transport = WindFreeTransport(
            hass,
            host="192.0.2.10",
            port=49154,
            credentials=credentials,
            session_factory=lambda session=session, **_kwargs: session,
        )
        with pytest.raises(TransportError):
            await transport.async_connect()
        names = [name for name, _args in session.calls]
        assert names[-2:] == ["close", "join"]


async def test_error_does_not_expose_host_payload_uuid_or_key(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    session = FakeSession(encoded_resources)
    session.get_error = RuntimeError(
        "failed at 192.0.2.10 with secret-payload device-uuid private-key"
    )
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    with pytest.raises(TransportError) as err:
        await transport.async_get("/oic/d")

    rendered = str(err.value)
    for secret in ("192.0.2.10", "secret-payload", "device-uuid", "private-key"):
        assert secret not in rendered
    assert err.value.__context__ is None


async def test_close_and_join_are_independently_bounded(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    blocker = threading.Event()

    class BlockingCloseSession(FakeSession):
        def close(self) -> None:
            self._record("close")
            blocker.wait()

        def join(self) -> None:
            self._record("join")
            blocker.wait()

    monkeypatch.setattr(transport_module, "_CLOSE_TIMEOUT", 0.01)
    session = BlockingCloseSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    await transport.async_close()

    assert [name for name, _args in session.calls][-2:] == ["close", "join"]
    blocker.set()


async def test_cancellation_propagates_after_blocking_call_settles(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingGetSession(FakeSession):
        def get(self, path: tuple[str, ...]) -> tuple[int, bytes]:
            self._record("get", path)
            started.set()
            release.wait()
            return self.get_code, self.encoded_resources["/" + "/".join(path)]

    session = BlockingGetSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()
    task = asyncio.create_task(transport.async_get("/oic/d"))
    await hass.async_add_executor_job(started.wait)

    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_close_cancellation_still_joins_after_close_settles(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    close_started = threading.Event()
    close_release = threading.Event()

    class BlockingCloseSession(FakeSession):
        def close(self) -> None:
            self._record("close")
            close_started.set()
            close_release.wait()

    session = BlockingCloseSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()
    task = asyncio.create_task(transport.async_close())
    await hass.async_add_executor_job(close_started.wait)

    task.cancel()
    close_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert [name for name, _args in session.calls][-2:] == ["close", "join"]


def _patch_discovery_to_use_fake_sessions(
    monkeypatch: pytest.MonkeyPatch,
    factory: RecordingSessionFactory,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    real_transport = WindFreeTransport

    def build(*args: object, **kwargs: object) -> WindFreeTransport:
        return real_transport(*args, **kwargs, session_factory=factory)

    monkeypatch.setattr(transport_module, "WindFreeTransport", build)


async def test_discovery_sweeps_only_all_nine_ports_sequentially_and_cleans_up(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_session(_port: int) -> FakeSession:
        session = FakeSession(encoded_resources)
        session.connect_error = ConnectionError("host and payload")
        return session

    factory = RecordingSessionFactory(failing_session)
    _patch_discovery_to_use_fake_sessions(monkeypatch, factory)

    with pytest.raises(ConnectionError) as err:
        await async_discover_transport(hass, "192.0.2.10", credentials)

    assert [call["port"] for call in factory.calls] == list(PROBE_PORTS)
    for session in factory.sessions:
        names = [name for name, _args in session.calls]
        assert names == ["connect", "close", "join"]
    assert "192.0.2.10" not in str(err.value)
    assert all(str(port) not in str(err.value) for port in PROBE_PORTS)


async def test_discovery_reuses_first_successful_authenticated_session(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def session_for(port: int) -> FakeSession:
        session = FakeSession(encoded_resources)
        if port != 49154:
            session.connect_error = TimeoutError("probe timeout")
        return session

    factory = RecordingSessionFactory(session_for)
    _patch_discovery_to_use_fake_sessions(monkeypatch, factory)

    port, discovered = await async_discover_transport(
        hass,
        "192.0.2.10",
        credentials,
    )

    assert port == 49154
    assert [call["port"] for call in factory.calls] == [49152, 49153, 49154]
    assert all(call["rate_limit_rps"] == RATE_LIMIT_RPS for call in factory.calls)
    assert factory.sessions[-1].HANDSHAKE_TIMEOUT_S == PROBE_HANDSHAKE_TIMEOUT
    assert [name for name, _args in factory.sessions[-1].calls] == [
        "connect",
        "start_reader",
    ]
    assert discovered._session is factory.sessions[-1]
