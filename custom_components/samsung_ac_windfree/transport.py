"""Executor-safe adapter for the pinned local DTLS/CoAP dependency."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
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


def _encode_representation(
    representation: Representation,
) -> bytes | _CallFailure:
    if not _is_representation(representation):
        return _CallFailure(None)
    try:
        encoded = cbor2.dumps(representation)
    except Exception:
        return _CallFailure(None)
    if len(encoded) > _MAX_CBOR_PAYLOAD:
        return _CallFailure(None)
    return encoded


def _redacted_target() -> None:
    """Replace sensitive callable locals before cancellation escapes."""


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
        target = _redacted_target
        args = ()
        raise


async def _executor_outcome[T](
    hass: HomeAssistant,
    target: Callable[..., T],
    *args: object,
) -> T | _CallFailure:
    try:
        return await _await_executor(hass, target, *args)
    except asyncio.CancelledError:
        target = _redacted_target
        args = ()
        raise
    except Exception as error:
        fatal_alert = _extract_fatal_alert(error)
        error = None
        return _CallFailure(fatal_alert)


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
        try:
            return await _executor_outcome(
                self._hass,
                _build_session,
                self._session_factory,
                self._host,
                self._port,
                credentials,
                self._handshake_timeout,
            )
        finally:
            credentials = None

    async def async_connect(self) -> None:
        """Construct, connect, and start exactly one blocking session."""
        failure: TransportError | None = None
        try:
            async with self._lifecycle_lock:
                if self._closed:
                    raise TransportError("transport_closed")
                if self._connected:
                    return
                built = await self._async_build_session()
                if isinstance(built, _CallFailure):
                    failure = TransportError(
                        "transport_connect_failed",
                        fatal_alert=built.fatal_alert,
                    )
                else:
                    session = built
                    self._session = session
                    connected = await _executor_outcome(
                        self._hass,
                        session.connect,
                    )
                    if isinstance(connected, _CallFailure):
                        failure = TransportError(
                            "transport_connect_failed",
                            fatal_alert=connected.fatal_alert,
                        )
                    elif self._closed:
                        failure = TransportError("transport_closed")
                    else:
                        reader = await _executor_outcome(
                            self._hass,
                            session.start_reader,
                        )
                        if isinstance(reader, _CallFailure):
                            failure = TransportError(
                                "transport_connect_failed",
                                fatal_alert=reader.fatal_alert,
                            )
                        else:
                            self._connected = True
                            return
        except asyncio.CancelledError:
            self._closed = True
            cleanup = self._ensure_cleanup_task()
            if cleanup is not None:
                await self._wait_for_cleanup(cleanup)
            raise

        if failure is None:
            failure = TransportError("transport_connect_failed")
        self._closed = True
        cleanup = self._ensure_cleanup_task()
        if cleanup is not None:
            await self._wait_for_cleanup(cleanup)
        raise failure

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
        async with self._request_lock:
            try:
                self._require_session()
                await self._pace_request()
                return await _executor_outcome(self._hass, target, *args)
            finally:
                self._last_request_at = time.monotonic()

    async def async_get(self, path: str) -> Representation:
        """Read and decode one complete dependency-owned Block2 response."""
        segments = _path_segments(path)
        path = ""
        session = self._require_session()
        outcome = await self._request_outcome(session.get, segments)
        if isinstance(outcome, _CallFailure):
            raise TransportError(
                "transport_get_failed",
                fatal_alert=outcome.fatal_alert,
            )
        code, payload = outcome
        outcome = None
        if code != _GET_CONTENT:
            payload = b""
            raise TransportError(
                "transport_get_rejected",
                coap_code=code,
            )
        representation = _decode_representation(payload)
        payload = b""
        if isinstance(representation, _CallFailure):
            raise TransportError("transport_get_invalid_response")
        return representation

    async def async_post(
        self,
        path: str,
        payload: Representation,
    ) -> None:
        """Encode and post one representation, requiring CoAP 2.04."""
        segments = _path_segments(path)
        path = ""
        encoded = _encode_representation(payload)
        payload = {}
        if isinstance(encoded, _CallFailure):
            raise TransportError("transport_post_invalid_payload")
        session = self._require_session()
        outcome = await self._request_outcome(
            session.post,
            segments,
            encoded,
        )
        encoded = b""
        if isinstance(outcome, _CallFailure):
            raise TransportError(
                "transport_post_failed",
                fatal_alert=outcome.fatal_alert,
            )
        code, response = outcome
        outcome = None
        del response
        if code != _POST_CHANGED:
            raise TransportError(
                "transport_post_rejected",
                coap_code=code,
            )

    async def async_observe(
        self,
        paths: tuple[str, ...],
        callback: NotificationCallback,
    ) -> None:
        """Register dependency OBSERVE callbacks for the supplied paths."""
        session = self._require_session()

        def deliver(path: str, body: Representation) -> None:
            callback(self._generation, path, body)

        session.on_notification = self.threadsafe_callback(
            generation=self._generation,
            target=deliver,
        )
        for path in paths:
            segments = _path_segments(path)
            path = ""
            outcome = await self._request_outcome(session.subscribe, segments)
            if isinstance(outcome, _CallFailure):
                raise TransportError(
                    "transport_observe_failed",
                    fatal_alert=outcome.fatal_alert,
                )

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

    async def async_close(self) -> None:
        """Start or observe one retained, bounded session cleanup."""
        self._closed = True
        cleanup = self._ensure_cleanup_task()
        if cleanup is not None:
            await self._wait_for_cleanup(cleanup)


def _path_segments(path: str) -> tuple[str, ...]:
    return tuple(segment for segment in path.split("/") if segment)


def _validate_probe_ports(ports: tuple[int, ...]) -> None:
    if any(type(port) is not int or port not in PROBE_PORTS for port in ports):
        raise ValueError(
            "transport_discovery_invalid_ports: ports are outside the safe range"
        )


async def async_discover_transport(
    hass: HomeAssistant,
    host: str,
    credentials: Credentials,
    *,
    ports: tuple[int, ...] = PROBE_PORTS,
) -> tuple[int, WindFreeTransport]:
    """Sequentially probe a safe-range subset and return the first session."""
    _validate_probe_ports(ports)
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
        except TransportError as error:
            await candidate.async_close()
            if error.fatal_alert is not None:
                raise
            continue
        except Exception:
            await candidate.async_close()
            continue
        return port, candidate
    raise ConnectionError("transport_discovery_failed: no authenticated local endpoint")
