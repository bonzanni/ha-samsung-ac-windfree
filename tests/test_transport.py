from __future__ import annotations

import asyncio
import logging
import threading
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING

import cbor2
import pytest
from OpenSSL import SSL

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
        self.call_threads: list[int] = []
        self.sessions: list[FakeSession] = []

    def __call__(self, **kwargs: object) -> FakeSession:
        self.call_threads.append(threading.get_ident())
        self.calls.append(kwargs)
        session = self._build(int(kwargs["port"]))
        self.sessions.append(session)
        return session


class ThreadedCleanupSession(FakeSession):
    """Fake blocking session with a real reader thread and ordered cleanup."""

    def __init__(
        self,
        encoded_resources: dict[str, bytes],
        *,
        block_close: bool = True,
    ) -> None:
        super().__init__(encoded_resources)
        self.close_started = threading.Event()
        self.close_release = threading.Event()
        self.close_finished = threading.Event()
        self.reader_stop = threading.Event()
        self.reader_thread: threading.Thread | None = None
        self.join_before_close = False
        if not block_close:
            self.close_release.set()

    def start_reader(self) -> None:
        self._record("start_reader")
        self.reader_thread = threading.Thread(
            target=self.reader_stop.wait,
            daemon=True,
            name="synthetic-dtls-reader",
        )
        self.reader_thread.start()

    def close(self) -> None:
        self._record("close")
        self.close_started.set()
        self.close_release.wait()
        self.reader_stop.set()
        self.close_finished.set()

    def join(self) -> None:
        self._record("join")
        if not self.close_finished.is_set():
            self.join_before_close = True
        assert self.reader_thread is not None
        self.reader_thread.join()


def _structured_alert(reason: str) -> ConnectionError:
    alert = SSL.Error([("SSL routines", "", reason)])
    wrapped = ConnectionError("sensitive outer handshake failure")
    wrapped.__cause__ = alert
    return wrapped


def _assert_transport_traceback_redacted(
    error: BaseException,
    *forbidden: str,
) -> None:
    for frame, _line in traceback.walk_tb(error.__traceback__):
        if (
            frame.f_globals.get("__name__")
            != "custom_components.samsung_ac_windfree.transport"
        ):
            continue
        rendered = repr(frame.f_locals)
        for secret in forbidden:
            assert secret not in rendered


async def test_constructor_uses_only_in_memory_pem_and_exact_rate_limit(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    loop_thread = threading.get_ident()
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
    assert factory.call_threads
    assert all(thread != loop_thread for thread in factory.call_threads)


async def test_constructor_failure_is_sanitized_and_scrubs_traceback_material(
    hass: HomeAssistant,
    credentials: Credentials,
) -> None:
    factory_thread: int | None = None

    def fail_factory(**_kwargs: object) -> FakeSession:
        nonlocal factory_thread
        factory_thread = threading.get_ident()
        raise RuntimeError("192.0.2.10 secret-factory-payload secret-private-key")

    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=fail_factory,
    )

    with pytest.raises(TransportError) as err:
        await transport.async_connect()

    assert factory_thread is not None
    assert factory_thread != threading.get_ident()
    assert err.value.__context__ is None
    _assert_transport_traceback_redacted(
        err.value,
        "192.0.2.10",
        "secret-factory-payload",
        "secret-private-key",
        credentials.client_key_pem[:64],
    )


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


@pytest.mark.parametrize(
    "raw_payload",
    [
        cbor2.dumps(["not-a-map"]),
        cbor2.dumps({1: "non-string-key"}),
        b"\xffmalformed-secret-cbor",
    ],
    ids=["non-map", "non-string-key", "malformed"],
)
async def test_get_rejects_invalid_cbor_shapes_without_raw_traceback(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    raw_payload: bytes,
) -> None:
    resources = {**encoded_resources, "/oic/d": raw_payload}
    session = FakeSession(resources)
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

    assert err.value.__context__ is None
    _assert_transport_traceback_redacted(
        err.value,
        "malformed-secret-cbor",
        "non-string-key",
    )


async def test_get_rejects_oversize_payload_before_cbor_decode(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    raw_payload = b"x" * (transport_module._MAX_CBOR_PAYLOAD + 1)
    resources = {**encoded_resources, "/oic/d": raw_payload}
    session = FakeSession(resources)
    loads_called = False

    def loads(_payload: bytes) -> object:
        nonlocal loads_called
        loads_called = True
        return {}

    monkeypatch.setattr(transport_module.cbor2, "loads", loads)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    with pytest.raises(TransportError):
        await transport.async_get("/oic/d")

    assert not loads_called


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


@pytest.mark.parametrize(
    "payload",
    [
        {1: "non-string-key"},
        {"value": "x" * (128 * 1024)},
    ],
    ids=["non-string-key", "oversize"],
)
async def test_post_rejects_invalid_or_oversize_representation_before_session(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    payload: dict[object, object],
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

    with pytest.raises(TransportError) as err:
        await transport.async_post("/power/vs/0", payload)  # type: ignore[arg-type]

    assert not any(name == "post" for name, _args in session.calls)
    assert err.value.__context__ is None
    _assert_transport_traceback_redacted(err.value, "non-string-key", "x" * 64)


async def test_post_session_error_scrubs_payload_and_encoded_cbor_traceback(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    session = FakeSession(encoded_resources)
    session.post_error = RuntimeError("secret-dependency-post-error")
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    with pytest.raises(TransportError) as err:
        await transport.async_post(
            "/power/vs/0",
            {"value": "secret-post-payload"},
        )

    assert err.value.__context__ is None
    _assert_transport_traceback_redacted(
        err.value,
        "secret-dependency-post-error",
        "secret-post-payload",
    )


async def test_post_rejection_scrubs_dependency_response_bytes_from_traceback(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    session = FakeSession(encoded_resources)
    session.post_code = 128
    session.post_response = b"secret-post-response-cbor"
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    with pytest.raises(TransportError) as err:
        await transport.async_post("/power/vs/0", {"value": "On"})

    assert err.value.__context__ is None
    _assert_transport_traceback_redacted(
        err.value,
        "secret-post-response-cbor",
    )


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


async def test_observe_decodes_before_scheduling_and_no_raw_bytes_cross_loop(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    loop_thread = threading.get_ident()
    decode_threads: list[int] = []
    scheduled_args: list[tuple[object, ...]] = []
    real_loads = cbor2.loads

    def loads(payload: bytes) -> object:
        decode_threads.append(threading.get_ident())
        return real_loads(payload)

    class RecordingLoop:
        def call_soon_threadsafe(
            self,
            callback: Callable[..., None],
            *args: object,
        ) -> object:
            scheduled_args.append(args)
            return hass.loop.call_soon_threadsafe(callback, *args)

    monkeypatch.setattr(transport_module.cbor2, "loads", loads)
    session = FakeSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    transport._loop = RecordingLoop()  # type: ignore[assignment]
    received: list[dict[str, object]] = []
    await transport.async_connect()
    await transport.async_observe(
        ("/power/vs/0",),
        lambda _generation, _path, body: received.append(dict(body)),
    )

    await hass.async_add_executor_job(
        session.emit,
        "/power/vs/0",
        encoded_resources["/power/vs/0"],
    )
    await asyncio.sleep(0)

    assert decode_threads
    assert all(thread != loop_thread for thread in decode_threads)
    assert scheduled_args
    assert all(
        not isinstance(argument, bytes)
        for arguments in scheduled_args
        for argument in arguments
    )
    assert any(
        isinstance(argument, dict)
        for arguments in scheduled_args
        for argument in arguments
    )
    assert received == [{"x.com.samsung.da.power": "Off"}]


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
    "raw_payload",
    [
        cbor2.dumps(["not-a-map"]),
        cbor2.dumps({1: "non-string-key"}),
        b"\xffmalformed-secret",
    ],
    ids=["non-map", "non-string-key", "malformed"],
)
async def test_observe_rejects_invalid_cbor_before_loop_schedule(
    hass: HomeAssistant,
    credentials: Credentials,
    raw_payload: bytes,
) -> None:
    scheduled = False
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: pytest.fail("must not construct"),
    )

    class RejectingLoop:
        def call_soon_threadsafe(self, *_args: object) -> None:
            nonlocal scheduled
            scheduled = True

    transport._loop = RejectingLoop()  # type: ignore[assignment]
    callback = transport.threadsafe_callback(
        generation=0,
        target=lambda *_args: pytest.fail("must not deliver"),
    )

    await hass.async_add_executor_job(callback, "/secret/device-uuid", raw_payload)

    assert not scheduled


async def test_observe_rejects_oversize_before_cbor_decode_or_loop_schedule(
    hass: HomeAssistant,
    credentials: Credentials,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    loads_called = False
    scheduled = False

    def loads(_payload: bytes) -> object:
        nonlocal loads_called
        loads_called = True
        return {}

    class RejectingLoop:
        def call_soon_threadsafe(self, *_args: object) -> None:
            nonlocal scheduled
            scheduled = True

    monkeypatch.setattr(transport_module.cbor2, "loads", loads)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: pytest.fail("must not construct"),
    )
    transport._loop = RejectingLoop()  # type: ignore[assignment]
    callback = transport.threadsafe_callback(
        generation=0,
        target=lambda *_args: pytest.fail("must not deliver"),
    )
    payload = b"x" * (transport_module._MAX_CBOR_PAYLOAD + 1)

    await hass.async_add_executor_job(callback, "/secret/device-uuid", payload)

    assert not loads_called
    assert not scheduled


@pytest.mark.parametrize(
    ("alert_reason", "expected"),
    [
        ("tlsv1 alert bad certificate", "bad_certificate"),
        ("tlsv1 alert number 42", "bad_certificate"),
        ("tlsv1 alert unsupported certificate", "unsupported_certificate"),
        ("tlsv1 alert number 43", "unsupported_certificate"),
        ("tlsv1 alert certificate expired", "certificate_expired"),
        ("tlsv1 alert number 45", "certificate_expired"),
        ("tlsv1 alert certificate unknown", "certificate_unknown"),
        ("tlsv1 alert number 46", "certificate_unknown"),
        ("tlsv1 alert unknown ca", "unknown_ca"),
        ("tlsv1 alert number 48", "unknown_ca"),
        ("tlsv1 alert access denied", "access_denied"),
        ("tlsv1 alert number 49", "access_denied"),
    ],
)
async def test_structured_allowed_fatal_alerts_are_extracted_and_sanitized(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    alert_reason: str,
    expected: str,
) -> None:
    session = FakeSession(encoded_resources)
    session.connect_error = _structured_alert(alert_reason)
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


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("tlsv1 alert bad certificate and alert code 42"),
        SSL.Error([("SSL routines", "", "tlsv1 alert handshake failure")]),
        SSL.Error([("SSL routines", "", "tlsv1 alert bad certificate with suffix")]),
        SSL.Error([("not an alert record",)]),
    ],
    ids=[
        "arbitrary-text",
        "unknown-structured",
        "inexact-structured",
        "malformed-structured",
    ],
)
async def test_unstructured_unknown_or_inexact_alert_remains_transient(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    error: Exception,
) -> None:
    session = FakeSession(encoded_resources)
    session.connect_error = error
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


async def test_dependency_logger_is_exactly_and_idempotently_sanitized(
    hass: HomeAssistant,
    credentials: Credentials,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    dependency_logger = logging.getLogger("smartthings_local.protocol.dtls_session")
    unrelated_logger = logging.getLogger("smartthings_local.unrelated")
    transport_module._install_dependency_log_filter()
    transport_module._install_dependency_log_filter()
    matching_filters = [
        item
        for item in dependency_logger.filters
        if isinstance(item, transport_module._DependencyLogFilter)
    ]
    assert len(matching_filters) == 1

    def emit_logs() -> None:
        try:
            raise RuntimeError("secret-raw-exception")
        except RuntimeError:
            dependency_logger.warning(
                "GET %s /%s payload=%r",
                "192.0.2.10",
                "device-uuid/private-path",
                b"secret-payload",
                exc_info=True,
                stack_info=True,
            )
        unrelated_logger.warning("unrelated-visible-message")

    with caplog.at_level(logging.WARNING):
        await hass.async_add_executor_job(emit_logs)

    dependency_records = [
        record
        for record in caplog.records
        if record.name == "smartthings_local.protocol.dtls_session"
    ]
    assert len(dependency_records) == 1
    record = dependency_records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "WindFree DTLS dependency warning"
    assert record.args == ()
    assert record.exc_info is None
    assert record.stack_info is None
    assert "unrelated-visible-message" in caplog.text
    for secret in (
        "192.0.2.10",
        "device-uuid",
        "private-path",
        "secret-payload",
        "secret-raw-exception",
    ):
        assert secret not in caplog.text


async def test_close_timeout_retains_one_ordered_cleanup_until_reader_exits(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    monkeypatch.setattr(transport_module, "_CLOSE_TIMEOUT", 0.01)
    session = ThreadedCleanupSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    cleanup_task = None
    try:
        await transport.async_close()

        assert session.close_started.is_set()
        assert not any(name == "join" for name, _args in session.calls)
        assert transport._session is session
        cleanup_task = transport._cleanup_task
        assert cleanup_task is not None
        assert not cleanup_task.done()
        assert session.reader_thread is not None
        assert session.reader_thread.is_alive()
    finally:
        session.close_release.set()
        if cleanup_task is not None:
            await asyncio.wait_for(asyncio.shield(cleanup_task), 1.0)
        else:
            await hass.async_add_executor_job(session.close_finished.wait)
            assert session.reader_thread is not None
            await hass.async_add_executor_job(session.reader_thread.join)
        await asyncio.sleep(0)

    assert [name for name, _args in session.calls][-2:] == ["close", "join"]
    assert not session.join_before_close
    assert not session.reader_thread.is_alive()
    assert transport._session is None
    assert transport._cleanup_task is None


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


async def test_close_cancellation_retains_cleanup_and_exact_cancellation(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    monkeypatch.setattr(transport_module, "_CLOSE_TIMEOUT", 0.01)
    session = ThreadedCleanupSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()
    task = asyncio.create_task(transport.async_close())
    await hass.async_add_executor_job(session.close_started.wait)
    cleanup_task = getattr(transport, "_cleanup_task", None)
    try:
        task.cancel("exact-close-cancellation")

        with pytest.raises(asyncio.CancelledError) as err:
            await task
        assert err.value.args == ("exact-close-cancellation",)
        assert cleanup_task is not None
        assert not cleanup_task.done()
        assert transport._cleanup_task is cleanup_task
        assert transport._session is session
        assert session.reader_thread is not None
        assert session.reader_thread.is_alive()
    finally:
        session.close_release.set()
        if cleanup_task is not None:
            await asyncio.wait_for(asyncio.shield(cleanup_task), 1.0)
        else:
            await hass.async_add_executor_job(session.close_finished.wait)
            assert session.reader_thread is not None
            await hass.async_add_executor_job(session.reader_thread.join)
        await asyncio.sleep(0)

    assert [name for name, _args in session.calls][-2:] == ["close", "join"]
    assert not session.join_before_close
    assert not session.reader_thread.is_alive()
    assert transport._session is None


@pytest.mark.parametrize("lock_name", ["_lifecycle_lock", "_request_lock"])
async def test_close_is_bounded_while_cleanup_waits_for_coordination_lock(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    lock_name: str,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    monkeypatch.setattr(transport_module, "_CLOSE_TIMEOUT", 0.01)
    session = ThreadedCleanupSession(encoded_resources, block_close=False)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()
    lock = getattr(transport, lock_name)
    await lock.acquire()
    cleanup_task = None
    try:
        await asyncio.wait_for(transport.async_close(), 0.1)
        cleanup_task = transport._cleanup_task
        assert cleanup_task is not None
        assert not cleanup_task.done()
        assert transport._session is session
    finally:
        lock.release()
        cleanup_task = getattr(transport, "_cleanup_task", cleanup_task)
        if cleanup_task is not None:
            await asyncio.wait_for(asyncio.shield(cleanup_task), 1.0)
        else:
            await transport.async_close()
        await asyncio.sleep(0)
    assert [name for name, _args in session.calls][-2:] == ["close", "join"]
    assert transport._session is None


async def test_repeated_close_reuses_one_cleanup_task_and_is_idempotent(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    monkeypatch.setattr(transport_module, "_CLOSE_TIMEOUT", 0.01)
    session = ThreadedCleanupSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    first_cleanup = None
    try:
        await transport.async_close()
        first_cleanup = transport._cleanup_task
        assert first_cleanup is not None
        await transport.async_close()
        assert transport._cleanup_task is first_cleanup
        assert [name for name, _args in session.calls].count("close") == 1
        assert [name for name, _args in session.calls].count("join") == 0
    finally:
        session.close_release.set()
        if first_cleanup is not None:
            await asyncio.wait_for(asyncio.shield(first_cleanup), 1.0)
        else:
            await hass.async_add_executor_job(session.close_finished.wait)
            assert session.reader_thread is not None
            await hass.async_add_executor_job(session.reader_thread.join)
        await asyncio.sleep(0)

    await transport.async_close()

    assert [name for name, _args in session.calls].count("close") == 1
    assert [name for name, _args in session.calls].count("join") == 1


def _patch_discovery_to_use_fake_sessions(
    monkeypatch: pytest.MonkeyPatch,
    factory: RecordingSessionFactory,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    real_transport = WindFreeTransport

    def build(*args: object, **kwargs: object) -> WindFreeTransport:
        return real_transport(*args, **kwargs, session_factory=factory)

    monkeypatch.setattr(transport_module, "WindFreeTransport", build)


@pytest.mark.parametrize(
    "ports",
    [
        (49151,),
        (49161,),
        (True,),
        (49154.0,),
        ("49154",),
        (49152, 65000),
    ],
    ids=[
        "below-range",
        "above-range",
        "bool",
        "float",
        "string",
        "mixed-invalid",
    ],
)
async def test_discovery_rejects_invalid_port_overrides_before_construction(
    hass: HomeAssistant,
    credentials: Credentials,
    monkeypatch: pytest.MonkeyPatch,
    ports: tuple[object, ...],
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    constructed = False

    def reject_construction(*_args: object, **_kwargs: object) -> None:
        nonlocal constructed
        constructed = True

    monkeypatch.setattr(transport_module, "WindFreeTransport", reject_construction)

    with pytest.raises(ValueError, match="transport_discovery_invalid_ports"):
        await async_discover_transport(
            hass,
            "192.0.2.10",
            credentials,
            ports=ports,  # type: ignore[arg-type]
        )

    assert not constructed


async def test_discovery_preserves_structured_fatal_alert_after_cleanup(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def session_for(_port: int) -> FakeSession:
        session = FakeSession(encoded_resources)
        session.connect_error = _structured_alert("tlsv1 alert bad certificate")
        return session

    factory = RecordingSessionFactory(session_for)
    _patch_discovery_to_use_fake_sessions(monkeypatch, factory)

    with pytest.raises(TransportError) as err:
        await async_discover_transport(
            hass,
            "192.0.2.10",
            credentials,
            ports=(49153, 49154),
        )

    assert err.value.fatal_alert == "bad_certificate"
    assert [call["port"] for call in factory.calls] == [49153]
    assert [name for name, _args in factory.sessions[0].calls] == [
        "connect",
        "close",
        "join",
    ]


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


async def test_discovery_allows_valid_port_subset_in_caller_order(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def session_for(port: int) -> FakeSession:
        session = FakeSession(encoded_resources)
        if port != 49154:
            session.connect_error = TimeoutError("transient")
        return session

    factory = RecordingSessionFactory(session_for)
    _patch_discovery_to_use_fake_sessions(monkeypatch, factory)

    port, _transport = await async_discover_transport(
        hass,
        "192.0.2.10",
        credentials,
        ports=(49158, 49154),
    )

    assert port == 49154
    assert [call["port"] for call in factory.calls] == [49158, 49154]
