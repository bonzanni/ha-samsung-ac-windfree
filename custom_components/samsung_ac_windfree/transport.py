"""Executor-safe adapter for the pinned local DTLS/CoAP dependency."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import cbor2
from homeassistant.core import HomeAssistant
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

_GET_CONTENT = 69
_POST_CHANGED = 68
_CLOSE_TIMEOUT = COAP_READ_TIMEOUT
_REQUEST_INTERVAL = 1.0 / RATE_LIMIT_RPS
_ALERT_CODE_PATTERN = re.compile(
    r"\balert(?:\s+(?:number|code|description))?[\s:=_-]+(\d{1,3})\b"
)
_FATAL_ALERTS_BY_CODE = {
    42: "bad_certificate",
    43: "unsupported_certificate",
    45: "certificate_expired",
    46: "certificate_unknown",
    48: "unknown_ca",
    49: "access_denied",
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


@dataclass(frozen=True, slots=True)
class _CallFailure:
    fatal_alert: str | None


def _error_texts(error: BaseException) -> tuple[str, ...]:
    texts: list[str] = []
    pending: list[object] = [error]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(item, BaseException):
            pending.extend(item.args)
            if item.__cause__ is not None:
                pending.append(item.__cause__)
            if item.__context__ is not None:
                pending.append(item.__context__)
        elif isinstance(item, tuple | list):
            pending.extend(item)
        elif isinstance(item, str):
            texts.append(item.lower())
    return tuple(texts)


def _extract_fatal_alert(error: BaseException) -> str | None:
    for text in _error_texts(error):
        match = _ALERT_CODE_PATTERN.search(text)
        if match is not None:
            alert = _FATAL_ALERTS_BY_CODE.get(int(match.group(1)))
            if alert is not None:
                return alert
        for alert in _FATAL_ALERTS_BY_CODE.values():
            phrase = alert.replace("_", r"[\s_-]+")
            if re.search(rf"\balert\b[^,\])]*\b{phrase}\b", text):
                return alert
    return None


def _build_session(
    factory: SessionFactory,
    *,
    host: str,
    port: int,
    credentials: Credentials,
) -> DtlsCoapSession | _CallFailure:
    try:
        return factory(
            host=host,
            port=port,
            cert_pem=credentials.client_chain_pem,
            key_pem=credentials.client_key_pem,
            rate_limit_rps=RATE_LIMIT_RPS,
        )
    except Exception as error:
        fatal_alert = _extract_fatal_alert(error)
        error = None
        return _CallFailure(fatal_alert)


def _decode_representation(payload: bytes) -> Representation | _CallFailure:
    try:
        decoded = cbor2.loads(payload)
    except Exception:
        return _CallFailure(None)
    if not isinstance(decoded, Mapping):
        return _CallFailure(None)
    return decoded


def _encode_representation(
    representation: Representation,
) -> bytes | _CallFailure:
    try:
        return cbor2.dumps(representation)
    except Exception:
        return _CallFailure(None)


def _consume_executor_result(future: asyncio.Future[object]) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except asyncio.CancelledError:
        return


async def _await_executor[T](
    hass: HomeAssistant,
    target: Callable[..., T],
    *args: object,
    deadline: float | None = None,
) -> T:
    job = asyncio.ensure_future(hass.async_add_executor_job(target, *args))
    try:
        if deadline is None:
            return await asyncio.shield(job)
        return await asyncio.wait_for(asyncio.shield(job), deadline)
    except asyncio.CancelledError:
        try:
            if deadline is None:
                await asyncio.shield(job)
            else:
                async with asyncio.timeout(deadline):
                    await asyncio.shield(job)
        except Exception:
            pass
        raise
    finally:
        if not job.done():
            job.add_done_callback(_consume_executor_result)


async def _executor_outcome[T](
    hass: HomeAssistant,
    target: Callable[..., T],
    *args: object,
    deadline: float | None = None,
) -> T | _CallFailure:
    try:
        return await _await_executor(hass, target, *args, deadline=deadline)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        fatal_alert = _extract_fatal_alert(error)
        error = None
        return _CallFailure(fatal_alert)


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
        self._connected = False
        self._closed = False
        self._last_request_at: float | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()

    async def async_connect(self) -> None:
        """Construct, connect, and start exactly one blocking session."""
        async with self._lifecycle_lock:
            if self._closed:
                raise TransportError("transport_closed")
            if self._connected:
                return
            credentials = self._credentials
            if credentials is None:
                raise TransportError("transport_connect_failed")
            built = _build_session(
                self._session_factory,
                host=self._host,
                port=self._port,
                credentials=credentials,
            )
            self._credentials = None
            if isinstance(built, _CallFailure):
                raise TransportError(
                    "transport_connect_failed",
                    fatal_alert=built.fatal_alert,
                )
            session = built
            self._session = session
            session.HANDSHAKE_TIMEOUT_S = self._handshake_timeout
            try:
                connected = await _executor_outcome(self._hass, session.connect)
                if isinstance(connected, _CallFailure):
                    await self._cleanup_session(session)
                    self._session = None
                    raise TransportError(
                        "transport_connect_failed",
                        fatal_alert=connected.fatal_alert,
                    )
                if self._closed:
                    await self._cleanup_session(session)
                    self._session = None
                    raise TransportError("transport_closed")
                reader = await _executor_outcome(self._hass, session.start_reader)
                if isinstance(reader, _CallFailure):
                    await self._cleanup_session(session)
                    self._session = None
                    raise TransportError(
                        "transport_connect_failed",
                        fatal_alert=reader.fatal_alert,
                    )
            except asyncio.CancelledError:
                await self._cleanup_session(session)
                self._session = None
                raise
            self._connected = True

    async def _pace_request(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            delay = _REQUEST_INTERVAL - (now - self._last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
        self._last_request_at = now

    def _require_session(self) -> DtlsCoapSession:
        if self._closed or not self._connected or self._session is None:
            raise TransportError("transport_not_connected")
        return self._session

    async def _request[T](
        self,
        operation: str,
        target: Callable[..., T],
        *args: object,
    ) -> T:
        async with self._request_lock:
            self._require_session()
            await self._pace_request()
            outcome = await _executor_outcome(self._hass, target, *args)
        if isinstance(outcome, _CallFailure):
            raise TransportError(
                operation,
                fatal_alert=outcome.fatal_alert,
            )
        return outcome

    async def async_get(self, path: str) -> Representation:
        """Read and decode one complete dependency-owned Block2 response."""
        session = self._require_session()
        code, payload = await self._request(
            "transport_get_failed",
            session.get,
            _path_segments(path),
        )
        if code != _GET_CONTENT:
            raise TransportError(
                "transport_get_rejected",
                coap_code=code,
            )
        representation = _decode_representation(payload)
        if isinstance(representation, _CallFailure):
            raise TransportError("transport_get_invalid_response")
        return representation

    async def async_post(
        self,
        path: str,
        payload: Representation,
    ) -> None:
        """Encode and post one representation, requiring CoAP 2.04."""
        encoded = _encode_representation(payload)
        if isinstance(encoded, _CallFailure):
            raise TransportError("transport_post_invalid_payload")
        session = self._require_session()
        code, _response = await self._request(
            "transport_post_failed",
            session.post,
            _path_segments(path),
            encoded,
        )
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
            await self._request(
                "transport_observe_failed",
                session.subscribe,
                _path_segments(path),
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
            try:
                self._loop.call_soon_threadsafe(
                    self._deliver_notification,
                    generation,
                    target,
                    path,
                    payload,
                )
            except RuntimeError:
                return

        return callback

    def _deliver_notification(
        self,
        generation: int,
        target: Callable[[str, Representation], None],
        path: str,
        payload: bytes,
    ) -> None:
        if self._closed or generation != self._generation:
            return
        representation = _decode_representation(payload)
        if isinstance(representation, _CallFailure):
            _LOGGER.warning("Dropped invalid WindFree notification")
            return
        try:
            target(path, representation)
        except Exception:
            _LOGGER.warning("Dropped failed WindFree notification callback")

    async def _cleanup_session(self, session: DtlsCoapSession) -> None:
        try:
            close = await _executor_outcome(
                self._hass,
                session.close,
                deadline=_CLOSE_TIMEOUT,
            )
        except asyncio.CancelledError:
            joined = await _executor_outcome(
                self._hass,
                session.join,
                deadline=_CLOSE_TIMEOUT,
            )
            if isinstance(joined, _CallFailure):
                _LOGGER.warning("WindFree transport reader did not stop cleanly")
            raise
        if isinstance(close, _CallFailure):
            _LOGGER.warning("WindFree transport close did not complete cleanly")
        joined = await _executor_outcome(
            self._hass,
            session.join,
            deadline=_CLOSE_TIMEOUT,
        )
        if isinstance(joined, _CallFailure):
            _LOGGER.warning("WindFree transport reader did not stop cleanly")

    async def async_close(self) -> None:
        """Deregister observations and tear down the blocking session."""
        self._closed = True
        async with self._lifecycle_lock:
            async with self._request_lock:
                session = self._session
                self._session = None
                self._connected = False
                if session is None:
                    return
                session.on_notification = None
                await self._cleanup_session(session)


def _path_segments(path: str) -> tuple[str, ...]:
    return tuple(segment for segment in path.split("/") if segment)


async def async_discover_transport(
    hass: HomeAssistant,
    host: str,
    credentials: Credentials,
    *,
    ports: tuple[int, ...] = PROBE_PORTS,
) -> tuple[int, WindFreeTransport]:
    """Sequentially probe nine ports and return the first authenticated one."""
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
        except Exception:
            await candidate.async_close()
            continue
        return port, candidate
    raise ConnectionError("transport_discovery_failed: no authenticated local endpoint")
