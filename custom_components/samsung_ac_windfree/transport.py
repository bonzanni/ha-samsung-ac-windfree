"""Executor-safe adapter for the pinned local DTLS/CoAP dependency."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass

import cbor2
from homeassistant.core import HomeAssistant
from OpenSSL import SSL
from smartthings_local.protocol.dtls_session import DtlsCoapSession

from .const import (
    COAP_READ_TIMEOUT,
    PROBE_HANDSHAKE_TIMEOUT,
    PROBE_PORTS,
    RATE_LIMIT_RPS,
    RUNTIME_HANDSHAKE_TIMEOUT,
)
from .models import Credentials

_LOGGER = logging.getLogger(__name__)
_DEPENDENCY_LOGGER = "smartthings_local.protocol.dtls_session"

_GET_CONTENT = 69
_POST_CHANGED = 68
_CLOSE_TIMEOUT = COAP_READ_TIMEOUT
_REQUEST_INTERVAL = 1.0 / RATE_LIMIT_RPS
_MAX_CBOR_PAYLOAD = 64 * 1024
_FATAL_ALERTS_BY_CODE = {
    42: "bad_certificate",
    43: "unsupported_certificate",
    45: "certificate_expired",
    46: "certificate_unknown",
    48: "unknown_ca",
    49: "access_denied",
}
_FATAL_ALERT_REASONS = {
    reason: alert
    for code, alert in _FATAL_ALERTS_BY_CODE.items()
    for reason in (
        f"tlsv1 alert {alert.replace('_', ' ')}",
        f"sslv3 alert {alert.replace('_', ' ')}",
        f"tlsv1 alert number {code}",
        f"sslv3 alert number {code}",
    )
}

Representation = Mapping[str, object]
NotificationCallback = Callable[[int, str, Representation], None]
SessionFactory = Callable[..., DtlsCoapSession]


class TransportError(ConnectionError):
    """Sanitized transport failure with safe classification metadata."""

    def __init__(
        self,
        operation: str,
        *,
        fatal_alert: str | None = None,
        coap_code: int | None = None,
    ) -> None:
        self.operation = operation
        self.fatal_alert = fatal_alert
        self.coap_code = coap_code
        super().__init__(f"{operation}: local transport operation failed")


class _DependencyLogFilter(logging.Filter):
    """Replace exact dependency records before manifest-enabled propagation."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = f"WindFree DTLS dependency {record.levelname.lower()}"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def _install_dependency_log_filter() -> None:
    logger = logging.getLogger(_DEPENDENCY_LOGGER)
    if not any(isinstance(item, _DependencyLogFilter) for item in logger.filters):
        logger.addFilter(_DependencyLogFilter())


_install_dependency_log_filter()


@dataclass(frozen=True, slots=True)
class _CallFailure:
    fatal_alert: str | None


@dataclass(frozen=True, slots=True)
class _PublicFailure:
    operation: str
    fatal_alert: str | None = None
    coap_code: int | None = None
    error_kind: str = "transport"


def _raise_public_failure(result: _PublicFailure) -> None:
    operation = result.operation
    fatal_alert = result.fatal_alert
    coap_code = result.coap_code
    error_kind = result.error_kind
    del result
    if error_kind == "value":
        raise ValueError(
            "transport_discovery_invalid_ports: ports are outside the safe range"
        )
    if error_kind == "connection":
        raise ConnectionError(
            "transport_discovery_failed: no authenticated local endpoint"
        )
    raise TransportError(
        operation,
        fatal_alert=fatal_alert,
        coap_code=coap_code,
    )


def _extract_ssl_alert(error: SSL.Error) -> str | None:
    if len(error.args) != 1 or not isinstance(error.args[0], list):
        return None
    for record in error.args[0]:
        if (
            not isinstance(record, tuple)
            or len(record) != 3
            or not all(isinstance(item, str) for item in record)
        ):
            continue
        reason = record[2].lower()
        alert = _FATAL_ALERT_REASONS.get(reason)
        if alert is not None:
            return alert
    return None


def _extract_fatal_alert(error: BaseException) -> str | None:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, SSL.Error):
            alert = _extract_ssl_alert(current)
            if alert is not None:
                return alert
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return None


def _build_session(
    factory: SessionFactory,
    host: str,
    port: int,
    credentials: Credentials,
    handshake_timeout: float,
) -> DtlsCoapSession | _CallFailure:
    try:
        session = factory(
            host=host,
            port=port,
            cert_pem=credentials.client_chain_pem,
            key_pem=credentials.client_key_pem,
            rate_limit_rps=RATE_LIMIT_RPS,
        )
        session.HANDSHAKE_TIMEOUT_S = handshake_timeout
        return session
    except Exception as error:
        fatal_alert = _extract_fatal_alert(error)
        error = None
        return _CallFailure(fatal_alert)


def _is_representation(value: object) -> bool:
    return isinstance(value, Mapping) and all(isinstance(key, str) for key in value)


def _decode_representation(payload: bytes) -> Representation | _CallFailure:
    if len(payload) > _MAX_CBOR_PAYLOAD:
        return _CallFailure(None)
    try:
        decoded = cbor2.loads(payload)
    except Exception:
        return _CallFailure(None)
    if not _is_representation(decoded):
        return _CallFailure(None)
    return decoded


def _to_cbor_native(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            _to_cbor_native(key): _to_cbor_native(item) for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_to_cbor_native(item) for item in value]
    if isinstance(value, Set):
        return frozenset(_to_cbor_native(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    raise TypeError("unsupported CBOR value")


def _encode_representation(
    representation: Representation,
) -> bytes | _CallFailure:
    if not _is_representation(representation):
        return _CallFailure(None)
    try:
        encoded = cbor2.dumps(
            _to_cbor_native(representation),
            canonical=True,
        )
    except Exception:
        return _CallFailure(None)
    if len(encoded) > _MAX_CBOR_PAYLOAD:
        return _CallFailure(None)
    return encoded


async def _await_executor[T](
    hass: HomeAssistant,
    target: Callable[..., T],
    *args: object,
) -> T:
    job = asyncio.ensure_future(hass.async_add_executor_job(target, *args))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(job)
        except Exception:
            pass
        raise
    finally:
        del target, args, job


async def _executor_outcome[T](
    hass: HomeAssistant,
    target: Callable[..., T],
    *args: object,
) -> T | _CallFailure:
    try:
        return await _await_executor(hass, target, *args)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        fatal_alert = _extract_fatal_alert(error)
        error = None
        return _CallFailure(fatal_alert)
    finally:
        del target, args


def _close_and_join(session: DtlsCoapSession) -> _CallFailure | None:
    failed = False
    try:
        session.close()
    except Exception:
        failed = True
    try:
        session.join()
    except Exception:
        failed = True
    if failed:
        return _CallFailure(None)
    return None


class WindFreeTransport:
    """Own one sustained blocking DTLS session from Home Assistant's loop."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        credentials: Credentials,
        *,
        generation: int = 0,
        handshake_timeout: float = RUNTIME_HANDSHAKE_TIMEOUT,
        session_factory: SessionFactory = DtlsCoapSession,
    ) -> None:
        self._hass = hass
        self._loop = hass.loop
        self._host = host
        self._port = port
        self._credentials: Credentials | None = credentials
        self._generation = generation
        self._handshake_timeout = handshake_timeout
        self._session_factory = session_factory
        self._session: DtlsCoapSession | None = None
        self._cleanup_task: asyncio.Task[_CallFailure | None] | None = None
        self._connected = False
        self._closed = False
        self._last_request_at: float | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()

    async def _async_build_session(self) -> DtlsCoapSession | _CallFailure:
        credentials = self._credentials
        self._credentials = None
        if credentials is None:
            return _CallFailure(None)
        job = asyncio.ensure_future(
            self._hass.async_add_executor_job(
                _build_session,
                self._session_factory,
                self._host,
                self._port,
                credentials,
                self._handshake_timeout,
            )
        )
        built: DtlsCoapSession | _CallFailure = _CallFailure(None)
        try:
            built = await asyncio.shield(job)
            return built
        except asyncio.CancelledError:
            try:
                built = await asyncio.shield(job)
            except Exception:
                built = _CallFailure(None)
            if not isinstance(built, _CallFailure):
                self._session = built
            raise
        finally:
            del credentials, job, built

    async def _async_cleanup_bounded(self) -> None:
        cleanup = self._ensure_cleanup_task()
        try:
            if cleanup is not None:
                await self._wait_for_cleanup(cleanup)
        finally:
            del cleanup

    async def _async_connect_result(self) -> _PublicFailure | None:
        built: DtlsCoapSession | _CallFailure | None = None
        session: DtlsCoapSession | None = None
        connected: object = None
        reader: object = None
        try:
            async with self._lifecycle_lock:
                if self._closed:
                    return _PublicFailure("transport_closed")
                if self._connected:
                    return None
                built = await self._async_build_session()
                if isinstance(built, _CallFailure):
                    return _PublicFailure(
                        "transport_connect_failed",
                        fatal_alert=built.fatal_alert,
                    )
                session = built
                self._session = session
                if self._closed:
                    return _PublicFailure("transport_closed")
                connected = await _executor_outcome(
                    self._hass,
                    session.connect,
                )
                if isinstance(connected, _CallFailure):
                    return _PublicFailure(
                        "transport_connect_failed",
                        fatal_alert=connected.fatal_alert,
                    )
                if self._closed:
                    return _PublicFailure("transport_closed")
                reader = await _executor_outcome(
                    self._hass,
                    session.start_reader,
                )
                if isinstance(reader, _CallFailure):
                    return _PublicFailure(
                        "transport_connect_failed",
                        fatal_alert=reader.fatal_alert,
                    )
                if self._closed:
                    return _PublicFailure("transport_closed")
                self._connected = True
                return None
        except asyncio.CancelledError:
            self._closed = True
            await self._async_cleanup_bounded()
            raise
        finally:
            del built, session, connected, reader

    async def async_connect(self) -> None:
        """Construct, connect, and start exactly one blocking session."""
        result = await self._async_connect_result()
        if result is None:
            return
        self._closed = True
        await self._async_cleanup_bounded()
        _raise_public_failure(result)

    async def _pace_request(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            delay = _REQUEST_INTERVAL - (now - self._last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)

    def _require_session(self) -> DtlsCoapSession:
        if self._closed or not self._connected or self._session is None:
            raise TransportError("transport_not_connected")
        return self._session

    async def _request_outcome[T](
        self,
        target: Callable[..., T],
        *args: object,
    ) -> T | _CallFailure:
        try:
            async with self._request_lock:
                self._require_session()
                await self._pace_request()
                return await _executor_outcome(self._hass, target, *args)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            fatal_alert = _extract_fatal_alert(error)
            error = None
            return _CallFailure(fatal_alert)
        finally:
            del target, args
            self._last_request_at = time.monotonic()

    async def _async_get_result(
        self,
        path: str,
    ) -> Representation | _PublicFailure:
        segments: tuple[str, ...] = ()
        session: DtlsCoapSession | None = None
        outcome: object = None
        payload = b""
        representation: Representation | _CallFailure | None = None
        try:
            segments = _path_segments(path)
            session = self._require_session()
            outcome = await self._request_outcome(session.get, segments)
            if isinstance(outcome, _CallFailure):
                return _PublicFailure(
                    "transport_get_failed",
                    fatal_alert=outcome.fatal_alert,
                )
            code, payload = outcome
            if code != _GET_CONTENT:
                return _PublicFailure(
                    "transport_get_rejected",
                    coap_code=code,
                )
            representation = _decode_representation(payload)
            if isinstance(representation, _CallFailure):
                return _PublicFailure("transport_get_invalid_response")
            return representation
        except asyncio.CancelledError:
            raise
        except Exception:
            return _PublicFailure("transport_get_failed")
        finally:
            del path, segments, session, outcome, payload, representation

    async def async_get(self, path: str) -> Representation:
        """Read and decode one complete dependency-owned Block2 response."""
        try:
            result = await self._async_get_result(path)
        finally:
            del path
        if isinstance(result, _PublicFailure):
            _raise_public_failure(result)
        return result

    async def _async_post_result(
        self,
        path: str,
        payload: Representation,
    ) -> _PublicFailure | None:
        segments: tuple[str, ...] = ()
        encoded: bytes | _CallFailure = b""
        session: DtlsCoapSession | None = None
        outcome: object = None
        response = b""
        try:
            segments = _path_segments(path)
            encoded = _encode_representation(payload)
            if isinstance(encoded, _CallFailure):
                return _PublicFailure("transport_post_invalid_payload")
            session = self._require_session()
            outcome = await self._request_outcome(
                session.post,
                segments,
                encoded,
            )
            if isinstance(outcome, _CallFailure):
                return _PublicFailure(
                    "transport_post_failed",
                    fatal_alert=outcome.fatal_alert,
                )
            code, response = outcome
            if code != _POST_CHANGED:
                return _PublicFailure(
                    "transport_post_rejected",
                    coap_code=code,
                )
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            return _PublicFailure("transport_post_failed")
        finally:
            del path, payload, segments, encoded, session, outcome, response

    async def async_post(
        self,
        path: str,
        payload: Representation,
    ) -> None:
        """Encode and post one representation, requiring CoAP 2.04."""
        try:
            result = await self._async_post_result(path, payload)
        finally:
            del path, payload
        if result is not None:
            _raise_public_failure(result)

    async def _async_observe_result(
        self,
        paths: tuple[str, ...],
        callback: NotificationCallback,
    ) -> _PublicFailure | None:
        session: DtlsCoapSession | None = None
        path = ""
        segments: tuple[str, ...] = ()
        outcome: object = None
        deliver: Callable[[str, Representation], None] | None = None
        try:
            session = self._require_session()
            deliver = _generation_target(self._generation, callback)
            session.on_notification = self.threadsafe_callback(
                generation=self._generation,
                target=deliver,
            )
            for path in paths:
                segments = _path_segments(path)
                outcome = await self._request_outcome(
                    session.subscribe,
                    segments,
                )
                if isinstance(outcome, _CallFailure):
                    return _PublicFailure(
                        "transport_observe_failed",
                        fatal_alert=outcome.fatal_alert,
                    )
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            return _PublicFailure("transport_observe_failed")
        finally:
            del paths, callback, session, path, segments, outcome, deliver

    async def async_observe(
        self,
        paths: tuple[str, ...],
        callback: NotificationCallback,
    ) -> None:
        """Register dependency OBSERVE callbacks for the supplied paths."""
        try:
            result = await self._async_observe_result(paths, callback)
        finally:
            del paths, callback
        if result is not None:
            _raise_public_failure(result)

    def threadsafe_callback(
        self,
        *,
        generation: int,
        target: Callable[[str, Representation], None],
    ) -> Callable[[str, bytes], None]:
        """Wrap a reader-thread callback for safe event-loop delivery."""

        def callback(path: str, payload: bytes) -> None:
            if self._closed or generation != self._generation:
                return
            representation = _decode_representation(payload)
            payload = b""
            if isinstance(representation, _CallFailure):
                _LOGGER.warning("Dropped invalid WindFree notification")
                return
            try:
                self._loop.call_soon_threadsafe(
                    self._deliver_notification,
                    generation,
                    target,
                    path,
                    representation,
                )
            except RuntimeError:
                return

        return callback

    def _deliver_notification(
        self,
        generation: int,
        target: Callable[[str, Representation], None],
        path: str,
        representation: Representation,
    ) -> None:
        if self._closed or generation != self._generation:
            return
        try:
            target(path, representation)
        except Exception:
            _LOGGER.warning("Dropped failed WindFree notification callback")

    async def _run_cleanup(
        self,
        session: DtlsCoapSession,
    ) -> _CallFailure | None:
        async with self._lifecycle_lock:
            async with self._request_lock:
                outcome = await _executor_outcome(
                    self._hass,
                    _close_and_join,
                    session,
                )
                if isinstance(outcome, _CallFailure):
                    _LOGGER.warning(
                        "WindFree transport cleanup did not complete cleanly"
                    )
                    return outcome
                return None

    def _cleanup_done(
        self,
        task: asyncio.Task[_CallFailure | None],
        session: DtlsCoapSession,
    ) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            _LOGGER.warning("WindFree transport cleanup was interrupted")
        except Exception:
            _LOGGER.warning("WindFree transport cleanup did not complete")
        if self._session is session:
            self._session = None
        self._connected = False
        if self._cleanup_task is task:
            self._cleanup_task = None

    def _ensure_cleanup_task(
        self,
    ) -> asyncio.Task[_CallFailure | None] | None:
        if self._cleanup_task is not None:
            return self._cleanup_task
        session = self._session
        if session is None:
            return None
        session.on_notification = None
        task = self._loop.create_task(self._run_cleanup(session))
        self._cleanup_task = task
        task.add_done_callback(lambda completed: self._cleanup_done(completed, session))
        return task

    async def _wait_for_cleanup(
        self,
        task: asyncio.Task[_CallFailure | None],
    ) -> None:
        deadline = self._loop.time() + _CLOSE_TIMEOUT
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.shield(task)
        except TimeoutError:
            return
        except asyncio.CancelledError:
            try:
                async with asyncio.timeout_at(deadline):
                    await asyncio.shield(task)
            except TimeoutError:
                pass
            raise
        finally:
            del task

    async def _async_close_result(self) -> _PublicFailure | None:
        cleanup: asyncio.Task[_CallFailure | None] | None = None
        self._closed = True
        try:
            cleanup = self._ensure_cleanup_task()
            if cleanup is not None:
                await self._wait_for_cleanup(cleanup)
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            return _PublicFailure("transport_close_failed")
        finally:
            del cleanup

    async def async_close(self) -> None:
        """Start or observe one retained, bounded session cleanup."""
        result = await self._async_close_result()
        if result is not None:
            _raise_public_failure(result)


def _path_segments(path: str) -> tuple[str, ...]:
    return tuple(segment for segment in path.split("/") if segment)


def _probe_ports_are_valid(ports: tuple[int, ...]) -> bool:
    return not any(type(port) is not int or port not in PROBE_PORTS for port in ports)


def _generation_target(
    generation: int,
    callback: NotificationCallback,
) -> Callable[[str, Representation], None]:
    def deliver(path: str, body: Representation) -> None:
        callback(generation, path, body)

    return deliver


async def _async_discover_result(
    hass: HomeAssistant,
    host: str,
    credentials: Credentials,
    ports: tuple[int, ...],
) -> tuple[int, WindFreeTransport] | _PublicFailure:
    port = 0
    candidate: WindFreeTransport | None = None
    try:
        if not _probe_ports_are_valid(ports):
            return _PublicFailure(
                "transport_discovery_invalid_ports",
                error_kind="value",
            )
        for port in ports:
            candidate = WindFreeTransport(
                hass,
                host=host,
                port=port,
                credentials=credentials,
                handshake_timeout=PROBE_HANDSHAKE_TIMEOUT,
            )
            try:
                await candidate.async_connect()
            except asyncio.CancelledError:
                await candidate.async_close()
                raise
            except TransportError as probe_error:
                await candidate.async_close()
                if probe_error.fatal_alert is not None:
                    return _PublicFailure(
                        probe_error.operation,
                        fatal_alert=probe_error.fatal_alert,
                        coap_code=probe_error.coap_code,
                    )
                candidate = None
                continue
            except Exception:
                await candidate.async_close()
                candidate = None
                continue
            return port, candidate
        return _PublicFailure(
            "transport_discovery_failed",
            error_kind="connection",
        )
    finally:
        del host, credentials, ports, port, candidate


async def async_discover_transport(
    hass: HomeAssistant,
    host: str,
    credentials: Credentials,
    *,
    ports: tuple[int, ...] = PROBE_PORTS,
) -> tuple[int, WindFreeTransport]:
    """Sequentially probe a safe-range subset and return the first session."""
    try:
        result = await _async_discover_result(hass, host, credentials, ports)
    finally:
        del host, credentials, ports
    if isinstance(result, _PublicFailure):
        _raise_public_failure(result)
    return result
