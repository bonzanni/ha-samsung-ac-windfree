"""Local integration for the exact Samsung WindFree AC model."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import CERT_REPAIR_WINDOW, DOMAIN, PLATFORMS
from .coordinator import WindFreeCoordinator
from .models import (
    AuthenticationRejected,
    CapabilityMismatch,
    Credentials,
    UnsupportedDevice,
)

type WindFreeConfigEntry = ConfigEntry[WindFreeCoordinator]

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


def _stored_credentials(data: Mapping[str, Any]) -> Credentials | None:
    try:
        credentials = Credentials(
            client_key_pem=data["client_key_pem"],
            client_chain_pem=data["client_chain_pem"],
            not_before=data["not_before"],
            not_after=data["not_after"],
        )
        if not all(
            isinstance(value, str)
            for value in (
                credentials.client_key_pem,
                credentials.client_chain_pem,
                credentials.not_before,
                credentials.not_after,
            )
        ):
            raise ValueError
        not_before = datetime.fromisoformat(credentials.not_before)
        not_after = datetime.fromisoformat(credentials.not_after)
        if (
            not_before.tzinfo is None
            or not_after.tzinfo is None
            or not_before >= not_after
        ):
            raise ValueError
    except KeyError, TypeError, ValueError:
        return None
    return credentials


def _stored_endpoint(data: Mapping[str, Any]) -> tuple[str, int] | None:
    try:
        host = data["host"]
        port = data["port"]
        if (
            not isinstance(host, str)
            or not host
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 49152 <= port <= 49160
        ):
            raise ValueError
    except KeyError, TypeError, ValueError:
        return None
    return host, port


def _certificate_expiry(credentials: Credentials) -> datetime | None:
    try:
        expires = datetime.fromisoformat(credentials.not_after)
        if expires.tzinfo is None:
            raise ValueError
    except TypeError, ValueError:
        return None
    return expires


async def _async_shutdown_cancellation_safe(
    hass: HomeAssistant,
    coordinator: WindFreeCoordinator,
) -> None:
    task = hass.async_create_task(
        coordinator.async_shutdown(),
        "windfree entry shutdown",
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        args = cancelled.args
        cancelled.__traceback__ = None
        cancelled = None
        try:
            await asyncio.shield(task)
        except Exception:
            pass
        del coordinator, task
        raise asyncio.CancelledError(*args) from None


async def _async_reload_entry(
    hass: HomeAssistant,
    entry: WindFreeConfigEntry,
) -> None:
    """Reload an entry after its atomically updated stored data changes."""

    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WindFreeConfigEntry,
) -> bool:
    """Set up exclusively from persisted per-installation material."""

    credentials = _stored_credentials(entry.data)
    if credentials is None:
        entry = None
        raise ConfigEntryError("invalid_stored_credentials") from None
    endpoint = _stored_endpoint(entry.data)
    if endpoint is None:
        credentials = None
        entry = None
        raise ConfigEntryError("invalid_stored_entry") from None
    host, port = endpoint
    endpoint = None
    expires = _certificate_expiry(credentials)
    if expires is None:
        host = ""
        credentials = None
        entry = None
        raise ConfigEntryError("invalid_stored_credentials") from None
    now = dt_util.utcnow()
    if expires <= now:
        host = ""
        credentials = None
        entry.async_start_reauth(hass)
        entry = None
        del host, credentials, expires, now, entry
        raise ConfigEntryAuthFailed("credentials_expired") from None

    if expires - now <= CERT_REPAIR_WINDOW:
        ir.async_create_issue(
            hass,
            DOMAIN,
            "certificate_expiring",
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="certificate_expiring",
        )

    coordinator = WindFreeCoordinator(
        hass,
        host=host,
        port=port,
        credentials=credentials,
        compatibility=_COMPATIBILITY,
    )
    reauth_started = False

    @callback
    def _async_handle_update() -> None:
        nonlocal reauth_started
        if coordinator.authentication_rejected and not reauth_started:
            reauth_started = True
            entry.async_start_reauth(hass)

    try:
        await coordinator.async_start()
        if coordinator.authentication_rejected:
            _async_handle_update()
            raise AuthenticationRejected("authentication_rejected")
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(coordinator.async_add_listener(_async_handle_update))
        entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
        _async_handle_update()
    except asyncio.CancelledError as cancelled:
        args = cancelled.args
        cancelled.__traceback__ = None
        cancelled = None
        await _async_shutdown_cancellation_safe(hass, coordinator)
        host = ""
        credentials = None
        entry = None
        coordinator = None
        del host, credentials, entry, coordinator
        raise asyncio.CancelledError(*args) from None
    except AuthenticationRejected:
        _async_handle_update()
        await _async_shutdown_cancellation_safe(hass, coordinator)
        host = ""
        credentials = None
        entry = None
        coordinator = None
        del host, credentials, entry, coordinator
        raise ConfigEntryAuthFailed("authentication_rejected") from None
    except UnsupportedDevice:
        await _async_shutdown_cancellation_safe(hass, coordinator)
        host = ""
        credentials = None
        entry = None
        coordinator = None
        del host, credentials, entry, coordinator
        raise ConfigEntryError("unsupported_device") from None
    except CapabilityMismatch:
        await _async_shutdown_cancellation_safe(hass, coordinator)
        host = ""
        credentials = None
        entry = None
        coordinator = None
        del host, credentials, entry, coordinator
        raise ConfigEntryError("capability_mismatch") from None
    except Exception as error:
        error.__traceback__ = None
        error = None
        await _async_shutdown_cancellation_safe(hass, coordinator)
        host = ""
        credentials = None
        entry = None
        coordinator = None
        del host, credentials, entry, coordinator
        raise ConfigEntryNotReady("setup_failed") from None
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: WindFreeConfigEntry,
) -> bool:
    """Unload entity platforms before terminal coordinator shutdown."""

    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await _async_shutdown_cancellation_safe(hass, entry.runtime_data)
    return True


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: WindFreeConfigEntry,
) -> bool:
    """Accept the initial version-one schema without performing network I/O."""

    del hass
    return entry.version == 1
