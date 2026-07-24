"""Host-only setup, reconfigure, and reauthentication flows."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant

from .bootstrap import async_bootstrap_credentials
from .const import DOMAIN, HOST_RESOLVE_TIMEOUT, SETUP_TIMEOUT
from .device import parse_identity, validate_contract
from .models import (
    BootstrapError,
    CapabilityMismatch,
    Credentials,
    DeviceIdentity,
    UnsupportedDevice,
    WindFreeError,
)
from .repairs import async_sync_bootstrap_issue
from .transport import TransportError, WindFreeTransport, async_discover_transport

BOOTSTRAP_TIMEOUT = 30.0
SWEEP_TIMEOUT = 54.0
IDENTITY_READ_TIMEOUT = 24.0

_HOST_SCHEMA = vol.Schema({vol.Required("host"): str})
_CONFIRM_SCHEMA = vol.Schema({})
_COMPATIBILITY: Mapping[str, object] = {
    "always_allowed": ["power", "hvac_mode", "display_light", "auto_clean"],
    "by_mode": {
        "Auto": [],
        "Cool": ["temperature", "fan", "swing", "preset"],
        "Dry": [],
        "Fan": [],
        "Heat": [],
    },
}
_BOOTSTRAP_ERRORS = frozenset(
    {
        "bootstrap_unavailable",
        "bootstrap_pin_mismatch",
        "invalid_clock",
        "bootstrap_invalid_material",
    }
)


@dataclass(frozen=True, slots=True)
class ValidatedSetup:
    """One locally authenticated and exact-model-validated setup."""

    host: str
    port: int
    identity: DeviceIdentity
    credentials: Credentials


@dataclass(frozen=True, slots=True)
class _ValidationFailure:
    error_key: str


class SetupValidationError(WindFreeError):
    """A fixed, sanitized setup failure category."""


class _ValidationCleanupError(Exception):
    """Internal marker for a validation session that could not close."""


async def async_resolve_host(hass: HomeAssistant, host: str) -> None:
    """Resolve a host without exposing resolver details to callers."""

    del hass
    loop = asyncio.get_running_loop()
    await loop.getaddrinfo(host, None, type=socket.SOCK_DGRAM)


def _bootstrap_error_key(error: BootstrapError) -> str:
    key = str(error).partition(":")[0]
    return key if key in _BOOTSTRAP_ERRORS else "bootstrap_unavailable"


async def _async_close_validation_transport(
    hass: HomeAssistant,
    transport: WindFreeTransport,
) -> bool:
    task = hass.async_create_task(
        transport.async_close(),
        "windfree setup transport cleanup",
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        cancellation_args = cancelled.args
        cancelled.__traceback__ = None
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as repeated:
                cancellation_args = repeated.args
                repeated.__traceback__ = None
                repeated = None
        try:
            task.result()
        except Exception:
            pass
        del transport, task
        raise asyncio.CancelledError(*cancellation_args) from None
    except Exception as error:
        error.__traceback__ = None
        error = None
        return False
    return True


async def _async_validate_pipeline(
    hass: HomeAssistant,
    host: str,
) -> ValidatedSetup | _ValidationFailure:
    credentials: Credentials | None = None
    transport: WindFreeTransport | None = None
    payloads: dict[str, Mapping[str, object]] | None = None
    cancellation_args: tuple[object, ...] | None = None
    result: ValidatedSetup | _ValidationFailure
    try:
        try:
            async with asyncio.timeout(HOST_RESOLVE_TIMEOUT):
                await async_resolve_host(hass, host)
        except TimeoutError:
            return _ValidationFailure("dns_timeout")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            error.__traceback__ = None
            error = None
            return _ValidationFailure("cannot_resolve")

        try:
            async with asyncio.timeout(BOOTSTRAP_TIMEOUT):
                credentials = await async_bootstrap_credentials(hass)
        except TimeoutError:
            return _ValidationFailure("fetch_timeout")
        except asyncio.CancelledError:
            raise
        except BootstrapError as error:
            key = _bootstrap_error_key(error)
            error.__traceback__ = None
            error = None
            return _ValidationFailure(key)
        except Exception as error:
            error.__traceback__ = None
            error = None
            return _ValidationFailure("bootstrap_unavailable")

        try:
            async with asyncio.timeout(SWEEP_TIMEOUT):
                port, transport = await async_discover_transport(
                    hass,
                    host,
                    credentials,
                )
        except TimeoutError:
            return _ValidationFailure("sweep_timeout")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            error.__traceback__ = None
            error = None
            return _ValidationFailure("cannot_connect")

        try:
            async with asyncio.timeout(IDENTITY_READ_TIMEOUT):
                payloads = {
                    path: await transport.async_get(path)
                    for path in ("/oic/d", "/oic/p", "/device/0")
                }
        except TimeoutError:
            return _ValidationFailure("read_timeout")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            error.__traceback__ = None
            error = None
            return _ValidationFailure("cannot_connect")

        tree = payloads["/device/0"]
        resources = {
            path: value
            for path, value in tree.items()
            if isinstance(path, str) and isinstance(value, Mapping)
        }
        try:
            identity = parse_identity(
                payloads["/oic/d"],
                payloads["/oic/p"],
                tree,
            )
            validate_contract(identity, resources, _COMPATIBILITY)
        except UnsupportedDevice:
            return _ValidationFailure("unsupported_device")
        except CapabilityMismatch:
            return _ValidationFailure("capability_mismatch")
        except Exception as error:
            error.__traceback__ = None
            error = None
            return _ValidationFailure("capability_mismatch")

        result = ValidatedSetup(
            host=host,
            port=port,
            identity=identity,
            credentials=credentials,
        )
    except asyncio.CancelledError as cancelled:
        cancellation_args = cancelled.args
        cancelled.__traceback__ = None
        cancelled = None
        result = _ValidationFailure("cancelled")
    finally:
        if transport is not None:
            closed = await _async_close_validation_transport(hass, transport)
            if not closed and cancellation_args is None:
                raise _ValidationCleanupError from None
        transport = None
        payloads = None

    if cancellation_args is not None:
        host = ""
        credentials = None
        args = cancellation_args
        cancellation_args = None
        del host, credentials, result, cancellation_args
        raise asyncio.CancelledError(*args) from None
    return result


async def _async_validate_setup_outcome(
    hass: HomeAssistant,
    host: str,
) -> ValidatedSetup | _ValidationFailure:
    cancellation_args: tuple[object, ...] | None = None
    try:
        async with asyncio.timeout(SETUP_TIMEOUT):
            return await _async_validate_pipeline(hass, host)
    except TimeoutError:
        return _ValidationFailure("setup_timeout")
    except _ValidationCleanupError:
        return _ValidationFailure("cannot_connect")
    except asyncio.CancelledError as cancelled:
        cancellation_args = cancelled.args
        cancelled.__traceback__ = None
        cancelled = None
    host = ""
    args = cancellation_args or ()
    cancellation_args = None
    del host, cancellation_args
    raise asyncio.CancelledError(*args) from None


async def async_validate_setup(
    hass: HomeAssistant,
    host: str,
) -> ValidatedSetup:
    """Bootstrap once, authenticate locally, and enforce the exact contract."""

    outcome: ValidatedSetup | _ValidationFailure | None = None
    cancellation_args: tuple[object, ...] | None = None
    try:
        outcome = await _async_validate_setup_outcome(hass, host)
    except asyncio.CancelledError as cancelled:
        cancellation_args = cancelled.args
        cancelled.__traceback__ = None
        cancelled = None
    host = ""
    if cancellation_args is not None:
        args = cancellation_args
        cancellation_args = None
        outcome = None
        del host, cancellation_args, outcome
        raise asyncio.CancelledError(*args) from None
    if isinstance(outcome, _ValidationFailure):
        key = outcome.error_key
        outcome = None
        del host, outcome
        raise SetupValidationError(key) from None
    return outcome


def _entry_data(validated: ValidatedSetup) -> dict[str, Any]:
    credentials = validated.credentials
    identity = validated.identity
    return {
        "host": validated.host,
        "port": validated.port,
        "device_id": identity.device_id,
        "model": identity.model,
        "firmware": identity.firmware,
        "platform": identity.platform,
        "client_key_pem": credentials.client_key_pem,
        "client_chain_pem": credentials.client_chain_pem,
        "not_before": credentials.not_before,
        "not_after": credentials.not_after,
    }


def _flow_error(error: BaseException) -> str:
    if isinstance(error, SetupValidationError):
        return str(error)
    if isinstance(error, BootstrapError):
        return _bootstrap_error_key(error)
    if isinstance(error, TransportError):
        return "cannot_connect"
    if isinstance(error, UnsupportedDevice):
        return "unsupported_device"
    if isinstance(error, CapabilityMismatch):
        return "capability_mismatch"
    if isinstance(error, TimeoutError):
        return "setup_timeout"
    return "unknown"


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure the exact WindFree model from a host alone."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._host = ""
        self._operation = "user"
        self._validation_task: asyncio.Task[ValidatedSetup] | None = None
        self._validated: ValidatedSetup | None = None
        self._error: str | None = None

    def _host_form(
        self,
        step_id: str,
        *,
        errors: dict[str, str] | None = None,
        suggested_host: str | None = None,
    ) -> ConfigFlowResult:
        schema = _HOST_SCHEMA
        if suggested_host:
            schema = self.add_suggested_values_to_schema(
                schema,
                {"host": suggested_host},
            )
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
        )

    def _start_validation(self, host: str, operation: str) -> ConfigFlowResult:
        self._host = host.strip()
        self._operation = operation
        self._validated = None
        self._error = None
        self._validation_task = self.hass.async_create_task(
            async_validate_setup(self.hass, self._host),
            "windfree config validation",
        )
        return self.async_show_progress(
            step_id="validate",
            progress_action="validate",
            progress_task=self._validation_task,
        )

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect only the host."""

        if user_input is None:
            return self._host_form("user")
        return self._start_validation(user_input["host"], "user")

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Validate a replacement address for the same OCF identity."""

        entry = self._get_reconfigure_entry()
        if user_input is None:
            return self._host_form(
                "reconfigure",
                suggested_host=str(entry.data["host"]),
            )
        return self._start_validation(user_input["host"], "reconfigure")

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Require explicit confirmation before fetching replacement credentials."""

        del entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm a one-time pinned bootstrap."""

        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=_CONFIRM_SCHEMA,
            )
        entry = self._get_reauth_entry()
        return self._start_validation(str(entry.data["host"]), "reauth")

    async def async_step_validate(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect the progress task outcome without exposing its exception."""

        del user_input
        task = self._validation_task
        if task is None:
            return self.async_abort(reason="unknown")
        if not task.done():
            return self.async_show_progress(
                step_id="validate",
                progress_action="validate",
                progress_task=task,
            )
        try:
            self._validated = await task
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._error = _flow_error(error)
            error.__traceback__ = None
            error = None
        finally:
            self._validation_task = None
        if self._error in {"bootstrap_pin_mismatch", "bootstrap_unavailable"}:
            async_sync_bootstrap_issue(self.hass, self._error)
        elif self._validated is not None or self._error in {
            "bootstrap_invalid_material",
            "cannot_connect",
            "unsupported_device",
            "capability_mismatch",
        }:
            async_sync_bootstrap_issue(self.hass, None)
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_finish(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Atomically create or update after local validation."""

        del user_input
        if self._error is not None:
            error = self._error
            self._error = None
            if self._operation == "reauth":
                return self.async_show_form(
                    step_id="reauth_confirm",
                    data_schema=_CONFIRM_SCHEMA,
                    errors={"base": error},
                )
            step_id = "reconfigure" if self._operation == "reconfigure" else "user"
            return self._host_form(
                step_id,
                errors={"base": error},
                suggested_host=self._host if step_id == "reconfigure" else None,
            )

        validated = self._validated
        if validated is None:
            return self.async_abort(reason="unknown")
        self._validated = None
        await self.async_set_unique_id(
            validated.identity.device_id,
            raise_on_progress=False,
        )
        data = _entry_data(validated)

        if self._operation == "user":
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Samsung WindFree AC",
                data=data,
            )

        if self._operation == "reconfigure":
            entry = self._get_reconfigure_entry()
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                entry,
                data=data,
                reason="reconfigure_successful",
            )

        entry = self._get_reauth_entry()
        self._abort_if_unique_id_mismatch()
        return self.async_update_reload_and_abort(
            entry,
            data=data,
            reason="reauth_successful",
        )
