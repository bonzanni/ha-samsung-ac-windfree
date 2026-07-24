from __future__ import annotations

import asyncio
import logging
import threading
import traceback
from collections.abc import Callable, Mapping, Sequence, Set
from typing import TYPE_CHECKING

import cbor2
import pytest
from OpenSSL import SSL

from custom_components.samsung_ac_windfree.const import (
    PROBE_HANDSHAKE_TIMEOUT,
    PROBE_PORTS,
    RATE_LIMIT_RPS,
)
from custom_components.samsung_ac_windfree.device import (
    TEMPERATURE_PATH,
    CommandKind,
    build_command,
)
from custom_components.samsung_ac_windfree.models import (
    Credentials,
    FanMode,
    HvacMode,
    PresetMode,
    SwingMode,
)
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
        if self.reader_thread is not None:
            self.reader_thread.join()


def _structured_alert(reason: str) -> ConnectionError:
    alert = SSL.Error([("SSL routines", "", reason)])
    wrapped = ConnectionError("sensitive outer handshake failure")
    wrapped.__cause__ = alert
    return wrapped


def _plain_cbor_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_cbor_value(item) for key, item in value.items()}
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain_cbor_value(item) for item in value]
    if isinstance(value, Set):
        return frozenset(_plain_cbor_value(item) for item in value)
    return value


def _assert_transport_traceback_redacted(
    error: BaseException,
    *sensitive_values: object,
    forbidden_names: frozenset[str] = frozenset(),
) -> None:
    transport_frames = []
    for frame, _line in traceback.walk_tb(error.__traceback__):
        if (
            frame.f_globals.get("__name__")
            != "custom_components.samsung_ac_windfree.transport"
        ):
            continue
        transport_frames.append(frame)

    assert transport_frames
    for frame in transport_frames:
        assert forbidden_names.isdisjoint(frame.f_locals), (
            frame.f_code.co_name,
            forbidden_names.intersection(frame.f_locals),
        )
        for name, value in frame.f_locals.items():
            for sensitive in sensitive_values:
                assert value is not sensitive, (frame.f_code.co_name, name)
                if isinstance(sensitive, str) and isinstance(value, str):
                    assert sensitive not in value, (frame.f_code.co_name, name)
                if isinstance(sensitive, bytes) and isinstance(value, bytes):
                    assert sensitive not in value, (frame.f_code.co_name, name)
                assert repr(sensitive) not in repr(value), (
                    frame.f_code.co_name,
                    name,
                )


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
        credentials.client_key_pem,
        credentials.client_chain_pem,
        credentials,
        fail_factory,
        forbidden_names=frozenset(
            {
                "built",
                "cleanup",
                "connected",
                "credentials",
                "factory",
                "failure",
                "host",
                "port",
                "reader",
                "session",
            }
        ),
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


async def test_get_normalizes_live_ocf_device_directory(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    directory = [
        {"rt": ["x.com.samsung.da.device"], "if": ["oic.if.baseline"]},
        {
            "href": "/power/vs/0",
            "rep": {"x.com.samsung.da.power": "On"},
        },
        {
            "href": "/mode/vs/0",
            "rep": {"x.com.samsung.da.modes": ["Cool"]},
        },
    ]
    session = FakeSession(
        {
            **encoded_resources,
            "/device/0": cbor2.dumps(directory),
        }
    )
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    assert await transport.async_get("/device/0") == {
        "/power/vs/0": {"x.com.samsung.da.power": "On"},
        "/mode/vs/0": {"x.com.samsung.da.modes": ["Cool"]},
    }


@pytest.mark.parametrize(
    "directory",
    [
        [
            {"rt": ["device"], "if": ["baseline"]},
            {"href": "/power/vs/0", "rep": {}},
            {"href": "/power/vs/0", "rep": {}},
        ],
        [
            {"rt": ["device"], "if": ["baseline"]},
            {"href": "/power/vs/0"},
        ],
        [
            {"rt": ["device"], "if": ["baseline"]},
            {"href": "power/vs/0", "rep": {}},
        ],
        [
            {"rt": ["device"], "if": ["baseline"]},
            {"href": "/power/vs/0", "rep": {1: "invalid"}},
        ],
        [
            {"rt": ["device"], "if": ["baseline"]},
            {"href": "/power/vs/0", "rep": {}, "unexpected": True},
        ],
        [
            {"href": "/power/vs/0", "rep": {}},
            {"rt": ["device"], "if": ["baseline"]},
        ],
    ],
    ids=[
        "duplicate-href",
        "missing-representation",
        "relative-href",
        "non-string-representation-key",
        "unexpected-envelope-key",
        "descriptor-not-first",
    ],
)
async def test_get_rejects_malformed_ocf_device_directory(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    directory: list[dict[object, object]],
) -> None:
    session = FakeSession(
        {
            **encoded_resources,
            "/device/0": cbor2.dumps(directory),
        }
    )
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    with pytest.raises(TransportError, match="transport_get_invalid_response"):
        await transport.async_get("/device/0")


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
    ("kind", "value"),
    [
        (CommandKind.POWER, True),
        (CommandKind.HVAC_MODE, HvacMode.AUTO),
        (CommandKind.TEMPERATURE, 27),
        (CommandKind.FAN, FanMode.HIGH),
        (CommandKind.SWING, SwingMode.BOTH),
        (CommandKind.PRESET, PresetMode.QUIET),
        (CommandKind.DISPLAY_LIGHT, False),
        (CommandKind.AUTO_CLEAN, True),
    ],
)
async def test_post_thaws_and_canonically_encodes_every_immutable_command(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    kind: CommandKind,
    value: object,
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
    aggregate = None
    if kind is CommandKind.TEMPERATURE:
        aggregate = cbor2.loads(encoded_resources[TEMPERATURE_PATH])
        aggregate["unknownBytes"] = bytearray(b"\x00\x80\xff")
        aggregate["unknownSet"] = {"preserved", "values"}
        aggregate["unknownNestedSet"] = frozenset(
            {
                frozenset({"nested", "values"}),
                frozenset({"singleton"}),
            }
        )
    command = build_command(kind, value, fresh_aggregate=aggregate)

    await transport.async_post(command.path, command.payload)

    post_call = next(call for call in session.calls if call[0] == "post")
    encoded = post_call[1][1]
    plain = _plain_cbor_value(command.payload)
    assert cbor2.loads(encoded) == plain
    assert encoded == cbor2.dumps(plain, canonical=True)


@pytest.mark.parametrize(
    "payload",
    [
        {1: "non-string-key"},
        {"value": "x" * (128 * 1024)},
        {"value": {"unsupported": object()}},
    ],
    ids=["non-string-key", "oversize", "unsupported-nested-value"],
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


async def test_close_during_blocking_constructor_never_starts_session_and_retains_cleanup(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    monkeypatch.setattr(transport_module, "_CLOSE_TIMEOUT", 0.01)
    factory_started = threading.Event()
    factory_release = threading.Event()
    session = ThreadedCleanupSession(encoded_resources)

    def blocking_factory(**_kwargs: object) -> FakeSession:
        factory_started.set()
        factory_release.wait()
        return session

    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=blocking_factory,
    )
    connect_task = asyncio.create_task(transport.async_connect())
    cleanup_task = None
    try:
        await hass.async_add_executor_job(factory_started.wait)
        await asyncio.wait_for(transport.async_close(), 0.1)
        factory_release.set()

        with pytest.raises(TransportError) as err:
            await asyncio.wait_for(connect_task, 0.2)
        assert err.value.operation == "transport_closed"
        assert not any(
            name in {"connect", "start_reader"} for name, _args in session.calls
        )
        cleanup_task = transport._cleanup_task
        assert cleanup_task is not None
        assert transport._session is session
        assert session.close_started.is_set()
    finally:
        factory_release.set()
        session.close_release.set()
        if not connect_task.done():
            connect_task.cancel()
        try:
            await connect_task
        except BaseException:
            pass
        cleanup_task = transport._cleanup_task or cleanup_task
        if cleanup_task is not None:
            await asyncio.wait_for(asyncio.shield(cleanup_task), 1.0)
        await asyncio.sleep(0)

    assert [name for name, _args in session.calls] == ["close", "join"]
    assert transport._session is None


async def test_cancelled_connect_during_constructor_retains_constructed_session_cleanup(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    monkeypatch.setattr(transport_module, "_CLOSE_TIMEOUT", 0.01)
    factory_started = threading.Event()
    factory_release = threading.Event()
    session = ThreadedCleanupSession(encoded_resources)

    def blocking_factory(**_kwargs: object) -> FakeSession:
        factory_started.set()
        factory_release.wait()
        return session

    transport = WindFreeTransport(
        hass,
        host="secret-constructor-host",
        port=49154,
        credentials=credentials,
        session_factory=blocking_factory,
    )
    connect_task = asyncio.create_task(transport.async_connect())
    cleanup_task = None
    try:
        await hass.async_add_executor_job(factory_started.wait)
        connect_task.cancel("exact-constructor-cancellation")
        factory_release.set()

        with pytest.raises(asyncio.CancelledError) as err:
            await asyncio.wait_for(connect_task, 0.2)
        assert err.value.args == ("exact-constructor-cancellation",)
        assert not any(
            name in {"connect", "start_reader"} for name, _args in session.calls
        )
        cleanup_task = transport._cleanup_task
        assert cleanup_task is not None
        assert transport._session is session
        _assert_transport_traceback_redacted(
            err.value,
            "secret-constructor-host",
            credentials,
            credentials.client_key_pem,
            credentials.client_chain_pem,
            blocking_factory,
            session,
            forbidden_names=frozenset(
                {
                    "args",
                    "built",
                    "cleanup",
                    "connected",
                    "credentials",
                    "factory",
                    "host",
                    "job",
                    "port",
                    "reader",
                    "result",
                    "session",
                    "target",
                }
            ),
        )
    finally:
        factory_release.set()
        session.close_release.set()
        if not connect_task.done():
            connect_task.cancel()
        try:
            await connect_task
        except BaseException:
            pass
        cleanup_task = transport._cleanup_task or cleanup_task
        if cleanup_task is not None:
            await asyncio.wait_for(asyncio.shield(cleanup_task), 1.0)
        await asyncio.sleep(0)

    assert [name for name, _args in session.calls] == ["close", "join"]
    assert transport._session is None


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


@pytest.mark.parametrize("operation", ["get", "post", "observe"])
async def test_request_cancellation_scrubs_complete_production_traceback(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    operation: str,
) -> None:
    started = threading.Event()
    release = threading.Event()
    secret_path = f"/secret/{operation}/device-uuid"
    secret_segments = ("secret", operation, "device-uuid")
    secret_payload = {"value": f"secret-{operation}-payload"}
    secret_encoded = cbor2.dumps(secret_payload)

    def callback(*_args: object) -> None:
        return

    class BlockingSession(FakeSession):
        def get(self, path: tuple[str, ...]) -> tuple[int, bytes]:
            self._record("get", path)
            started.set()
            release.wait()
            return 69, cbor2.dumps({"value": "secret-get-response"})

        def post(self, path: tuple[str, ...], payload: bytes) -> tuple[int, bytes]:
            self._record("post", path, payload)
            started.set()
            release.wait()
            return 68, b"secret-post-response"

        def subscribe(self, path: tuple[str, ...]) -> bytes:
            self._record("subscribe", path)
            started.set()
            release.wait()
            return b"secret-observe-response"

    session = BlockingSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()
    if operation == "get":
        task = asyncio.create_task(transport.async_get(secret_path))
    elif operation == "post":
        task = asyncio.create_task(transport.async_post(secret_path, secret_payload))
    else:
        task = asyncio.create_task(transport.async_observe((secret_path,), callback))
    await hass.async_add_executor_job(started.wait)

    task.cancel(f"exact-{operation}-cancellation")
    release.set()

    with pytest.raises(asyncio.CancelledError) as err:
        await task
    assert err.value.args == (f"exact-{operation}-cancellation",)
    _assert_transport_traceback_redacted(
        err.value,
        secret_path,
        secret_segments,
        secret_payload,
        secret_encoded,
        callback,
        session,
        forbidden_names=frozenset(
            {
                "args",
                "callback",
                "decoded",
                "deliver",
                "encoded",
                "outcome",
                "path",
                "paths",
                "payload",
                "representation",
                "response",
                "segments",
                "session",
                "target",
            }
        ),
    )


async def test_get_error_scrubs_path_segments_raw_response_and_session(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    secret_path = "/secret/get/device-uuid"
    secret_segments = ("secret", "get", "device-uuid")
    decoded_response = {1: "secret-get-response"}
    raw_response = cbor2.dumps(decoded_response)
    session = FakeSession({**encoded_resources, secret_path: raw_response})
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    with pytest.raises(TransportError) as err:
        await transport.async_get(secret_path)

    _assert_transport_traceback_redacted(
        err.value,
        secret_path,
        secret_segments,
        decoded_response,
        raw_response,
        session,
        forbidden_names=frozenset(
            {
                "args",
                "decoded",
                "outcome",
                "path",
                "payload",
                "representation",
                "segments",
                "session",
                "target",
            }
        ),
    )


async def test_post_error_scrubs_path_payload_encoded_response_and_session(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    secret_path = "/secret/post/device-uuid"
    secret_segments = ("secret", "post", "device-uuid")
    secret_payload = {"value": "secret-post-payload"}
    encoded = cbor2.dumps(secret_payload)
    raw_response = b"secret-post-response"
    session = FakeSession(encoded_resources)
    session.post_code = 128
    session.post_response = raw_response
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    with pytest.raises(TransportError) as err:
        await transport.async_post(secret_path, secret_payload)

    _assert_transport_traceback_redacted(
        err.value,
        secret_path,
        secret_segments,
        secret_payload,
        encoded,
        raw_response,
        session,
        forbidden_names=frozenset(
            {
                "args",
                "encoded",
                "outcome",
                "path",
                "payload",
                "response",
                "segments",
                "session",
                "target",
            }
        ),
    )


async def test_observe_error_scrubs_paths_segments_callback_and_session(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
) -> None:
    secret_path = "/secret/observe/device-uuid"
    secret_paths = (secret_path,)
    secret_segments = ("secret", "observe", "device-uuid")

    def callback(*_args: object) -> None:
        return

    raw_error = RuntimeError("secret-observe-dependency-error")
    session = FakeSession(encoded_resources)
    session.subscribe_error = raw_error
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    with pytest.raises(TransportError) as err:
        await transport.async_observe(secret_paths, callback)

    _assert_transport_traceback_redacted(
        err.value,
        secret_path,
        secret_paths,
        secret_segments,
        callback,
        raw_error,
        session,
        forbidden_names=frozenset(
            {
                "args",
                "callback",
                "decoded",
                "deliver",
                "encoded",
                "outcome",
                "path",
                "paths",
                "segments",
                "session",
                "target",
            }
        ),
    )


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
        _assert_transport_traceback_redacted(
            err.value,
            session,
            cleanup_task,
            forbidden_names=frozenset({"cleanup", "session", "task"}),
        )
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


async def test_close_error_is_sanitized_and_scrubs_internal_outcome(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_error = RuntimeError("secret-close-internal-error")
    session = FakeSession(encoded_resources)
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_kwargs: session,
    )
    await transport.async_connect()

    def fail_cleanup() -> None:
        raise raw_error

    monkeypatch.setattr(transport, "_ensure_cleanup_task", fail_cleanup)

    with pytest.raises(TransportError) as err:
        await transport.async_close()

    _assert_transport_traceback_redacted(
        err.value,
        raw_error,
        session,
        forbidden_names=frozenset({"cleanup", "error", "outcome", "session", "task"}),
    )


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


async def test_discovery_error_scrubs_host_ports_credentials_and_candidates(
    hass: HomeAssistant,
    credentials: Credentials,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    candidates: list[object] = []
    raw_errors: list[BaseException] = []

    class FailingCandidate:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            candidates.append(self)

        async def async_connect(self) -> None:
            error = RuntimeError("secret-discovery-outcome")
            raw_errors.append(error)
            raise error

        async def async_close(self) -> None:
            return

    monkeypatch.setattr(transport_module, "WindFreeTransport", FailingCandidate)
    ports = (49152, 49153)

    with pytest.raises(ConnectionError) as err:
        await async_discover_transport(
            hass,
            "secret-discovery-host",
            credentials,
            ports=ports,
        )

    _assert_transport_traceback_redacted(
        err.value,
        "secret-discovery-host",
        ports,
        credentials,
        credentials.client_key_pem,
        credentials.client_chain_pem,
        *candidates,
        *raw_errors,
        forbidden_names=frozenset(
            {
                "candidate",
                "credentials",
                "error",
                "factory",
                "host",
                "outcome",
                "port",
                "ports",
            }
        ),
    )


async def test_discovery_cancellation_is_exact_and_scrubs_probe_state(
    hass: HomeAssistant,
    credentials: Credentials,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.samsung_ac_windfree import transport as transport_module

    started = asyncio.Event()
    closed = asyncio.Event()
    candidate: object | None = None

    class BlockingCandidate:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal candidate
            candidate = self

        async def async_connect(self) -> None:
            started.set()
            await asyncio.Event().wait()

        async def async_close(self) -> None:
            closed.set()

    monkeypatch.setattr(transport_module, "WindFreeTransport", BlockingCandidate)
    ports = (49152, 49153)
    task = asyncio.create_task(
        async_discover_transport(
            hass,
            "secret-discovery-host",
            credentials,
            ports=ports,
        )
    )
    await started.wait()
    task.cancel("exact-discovery-cancellation")

    with pytest.raises(asyncio.CancelledError) as err:
        await task

    assert err.value.args == ("exact-discovery-cancellation",)
    assert closed.is_set()
    assert candidate is not None
    _assert_transport_traceback_redacted(
        err.value,
        "secret-discovery-host",
        ports,
        credentials,
        credentials.client_key_pem,
        credentials.client_chain_pem,
        candidate,
        forbidden_names=frozenset(
            {
                "candidate",
                "credentials",
                "error",
                "factory",
                "host",
                "outcome",
                "port",
                "ports",
            }
        ),
    )


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


async def test_discovery_assigns_requested_supervisor_generation(
    hass: HomeAssistant,
    credentials: Credentials,
    encoded_resources: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = RecordingSessionFactory(lambda _port: FakeSession(encoded_resources))
    _patch_discovery_to_use_fake_sessions(monkeypatch, factory)

    _port, discovered = await async_discover_transport(
        hass,
        "192.0.2.10",
        credentials,
        ports=(49152,),
        generation=7,
    )

    assert discovered._generation == 7


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
