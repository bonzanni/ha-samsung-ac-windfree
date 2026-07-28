"""Host-only setup, reconfigure, and reauthentication flows."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import FileSelector, FileSelectorConfig

from .const import DOMAIN, HOST_RESOLVE_TIMEOUT, SETUP_TIMEOUT
from .credentials import (
    MAX_CREDENTIAL_BYTES,
    CredentialError,
    parse_uploaded_credential,
    stored_credentials,
)
from .device import parse_identity, validate_contract
from .models import (
    CapabilityMismatch,
    Credentials,
    DeviceIdentity,
    UnsupportedDevice,
    WindFreeError,
)
from .transport import TransportError, WindFreeTransport, async_discover_transport

SWEEP_TIMEOUT = 54.0
IDENTITY_READ_TIMEOUT = 24.0

# The stored credential fields reconfigure must never rewrite.
_CREDENTIAL_KEYS = frozenset(
    {"client_key_pem", "client_chain_pem", "not_before", "not_after"}
)

CONF_CLIENT_KEY_FILE = "client_key_file"
CONF_CLIENT_CHAIN_FILE = "client_chain_file"

_HOST_SCHEMA = vol.Schema({vol.Required("host"): str})
_CREDENTIAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_KEY_FILE): FileSelector(
            FileSelectorConfig(accept=".pem,.key,application/x-pem-file")
        ),
        vol.Required(CONF_CLIENT_CHAIN_FILE): FileSelector(
            FileSelectorConfig(accept=".pem,.crt,.cer,application/x-pem-file")
        ),
    }
)
_COMPATIBILITY: Mapping[str, object] = {
    "always_allowed": ["power", "hvac_mode", "display_light", "auto_clean"],
    "by_mode": {
        "Auto": [],
        "Cool": ["temperature", "fan", "swing", "preset"],
        "Dry": ["preset"],
        "Fan": [],
        "Heat": ["temperature"],
    },
}


@dataclass(frozen=True, slots=True)
class ValidatedSetup:
    """One locally authenticated and exact-fingerprint-validated setup."""

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


async def _async_read_uploaded_credential(
    hass: HomeAssistant,
    key_id: str,
    chain_id: str,
) -> Credentials:
    """Consume both uploads exactly once and validate what they contain.

    process_uploaded_file is a synchronous context manager that deletes the
    upload on exit, so both handles are consumed in a single executor call: if
    the chain were read after a failure on the key, the second upload would be
    left behind in Home Assistant's temporary storage.
    """

    def _consume(upload_id: str) -> bytes | None:
        """Consume one upload, refusing to read more than a credential's worth.

        The size is checked inside the context so an oversized file is still
        deleted, and is never loaded into memory: the upload endpoint accepts
        far larger files than this.
        """

        with process_uploaded_file(hass, upload_id) as path:
            if path.stat().st_size > MAX_CREDENTIAL_BYTES:
                return None
            return path.read_bytes()

    def _read() -> tuple[bytes | None, bytes | None]:
        # Every handle is consumed even when an earlier one fails, so a rejected
        # upload never strands a file in Home Assistant's temporary storage.
        key_bytes: bytes | None = None
        chain_bytes: bytes | None = None
        try:
            key_bytes = _consume(key_id)
        finally:
            if chain_id != key_id:
                try:
                    chain_bytes = _consume(chain_id)
                except Exception:
                    chain_bytes = None
        return key_bytes, chain_bytes

    if key_id == chain_id:
        # A single handle cannot be consumed twice, but it must still be
        # consumed once: returning early would leave the key file behind.
        try:
            await hass.async_add_executor_job(_read)
        except Exception:
            pass
        raise CredentialError("credentials_duplicate_file")

    try:
        key_bytes, chain_bytes = await hass.async_add_executor_job(_read)
    except Exception:
        raise CredentialError("credentials_unreadable") from None

    if key_bytes is None or chain_bytes is None:
        raise CredentialError("credentials_unreadable")

    return parse_uploaded_credential(key_bytes, chain_bytes)


async def _async_close_validation_transport(
    hass: HomeAssistant,
    transport: WindFreeTransport,
) -> bool:
    task = hass.async_create_task(
        transport.async_close(),
        "windfree setup transport cleanup",
    )
    cancellation_args: tuple[object, ...] | None = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as cancelled:
            cancellation_args = cancelled.args
            cancelled.__traceback__ = None
            cancelled = None
    closed = True
    try:
        task.result()
    except asyncio.CancelledError as cancelled:
        cancelled.__traceback__ = None
        cancelled = None
        closed = False
    except Exception as error:
        error.__traceback__ = None
        error = None
        closed = False
    if cancellation_args is not None:
        args = cancellation_args
        cancellation_args = None
        del transport, task
        raise asyncio.CancelledError(*args) from None
    return closed


async def _async_validate_pipeline(
    hass: HomeAssistant,
    host: str,
    credentials: Credentials,
) -> ValidatedSetup | _ValidationFailure:
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
    credentials: Credentials,
) -> ValidatedSetup | _ValidationFailure:
    cancellation_args: tuple[object, ...] | None = None
    try:
        async with asyncio.timeout(SETUP_TIMEOUT):
            return await _async_validate_pipeline(hass, host, credentials)
    except TimeoutError:
        return _ValidationFailure("setup_timeout")
    except _ValidationCleanupError:
        return _ValidationFailure("cannot_connect")
    except asyncio.CancelledError as cancelled:
        cancellation_args = cancelled.args
        cancelled.__traceback__ = None
        cancelled = None
    host = ""
    credentials = None
    args = cancellation_args or ()
    cancellation_args = None
    del host, credentials, cancellation_args
    raise asyncio.CancelledError(*args) from None


async def async_validate_setup(
    hass: HomeAssistant,
    host: str,
    credentials: Credentials,
) -> ValidatedSetup:
    """Authenticate locally with a supplied credential and enforce the contract."""

    outcome: ValidatedSetup | _ValidationFailure | None = None
    cancellation_args: tuple[object, ...] | None = None
    try:
        outcome = await _async_validate_setup_outcome(hass, host, credentials)
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
    if isinstance(error, CredentialError):
        return str(error)
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
        self._credentials: Credentials | None = None

    def _credential_form(
        self,
        step_id: str,
        *,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        # Upload ids are single-use, so nothing is ever suggested back into the
        # form: a retry must re-upload both files.
        return self.async_show_form(
            step_id=step_id,
            data_schema=_CREDENTIAL_SCHEMA,
            errors=errors,
        )

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
        credentials = self._credentials
        assert credentials is not None
        self._validation_task = self.hass.async_create_task(
            async_validate_setup(self.hass, self._host, credentials),
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
        self._host = str(user_input["host"]).strip()
        self._operation = "user"
        return self._credential_form("credentials")

    async def async_step_credentials(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Accept the uploaded client key and chain, then validate locally."""

        if user_input is None:
            return self._credential_form("credentials")
        try:
            self._credentials = await _async_read_uploaded_credential(
                self.hass,
                str(user_input[CONF_CLIENT_KEY_FILE]),
                str(user_input[CONF_CLIENT_CHAIN_FILE]),
            )
        except CredentialError as error:
            key = str(error)
            error.__traceback__ = None
            error = None
            return self._credential_form("credentials", errors={"base": key})
        return self._start_validation(self._host, self._operation)

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
        # A new address for the same device reuses the stored credential; the
        # owner is not asked to upload one again, and it is never replaced.
        stored = stored_credentials(entry.data)
        if stored is None:
            return self._host_form(
                "reconfigure",
                errors={"base": "invalid_stored_credentials"},
                suggested_host=str(entry.data["host"]),
            )
        self._credentials = stored
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
        """Take a replacement credential for the existing entry."""

        entry = self._get_reauth_entry()
        self._host = str(entry.data["host"])
        self._operation = "reauth"
        if user_input is None:
            return self._credential_form("reauth_confirm")
        try:
            self._credentials = await _async_read_uploaded_credential(
                self.hass,
                str(user_input[CONF_CLIENT_KEY_FILE]),
                str(user_input[CONF_CLIENT_CHAIN_FILE]),
            )
        except CredentialError as error:
            key = str(error)
            error.__traceback__ = None
            error = None
            return self._credential_form("reauth_confirm", errors={"base": key})
        return self._start_validation(self._host, "reauth")

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
            self._credentials = None
            if self._operation == "reauth":
                # The uploads were consumed, so the owner must supply them
                # again; a confirm-only form would have nothing to retry with.
                return self._credential_form("reauth_confirm", errors={"base": error})
            if self._operation == "user":
                return self._credential_form("credentials", errors={"base": error})
            return self._host_form(
                "reconfigure",
                errors={"base": error},
                suggested_host=self._host,
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
            # Narrow update: a wholesale data= replacement would drop any stored
            # field this version does not know about.
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    key: value
                    for key, value in data.items()
                    if key not in _CREDENTIAL_KEYS
                },
                reason="reconfigure_successful",
            )

        entry = self._get_reauth_entry()
        self._abort_if_unique_id_mismatch()
        return self.async_update_reload_and_abort(
            entry,
            data=data,
            reason="reauth_successful",
        )
