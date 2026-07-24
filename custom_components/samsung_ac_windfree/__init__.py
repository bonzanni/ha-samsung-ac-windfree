"""Local integration for the exact Samsung WindFree AC model."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN, PLATFORMS
from .coordinator import WindFreeCoordinator
from .models import (
    AuthenticationRejected,
    CapabilityMismatch,
    Credentials,
    UnsupportedDevice,
)
from .repairs import (
    async_handle_entry_unload,
    async_purge_entry_issues,
    async_sync_certificate_issue,
    async_sync_runtime_issues,
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
_LIFECYCLE_DATA = f"{DOMAIN}_entry_lifecycle"
_ENTRY_MINOR_VERSION = 1


def _translated_entry_error(
    error_type: (
        type[ConfigEntryError] | type[ConfigEntryNotReady] | type[ConfigEntryAuthFailed]
    ),
    translation_key: str,
) -> ConfigEntryError | ConfigEntryNotReady | ConfigEntryAuthFailed:
    """Create a lifecycle error containing translation metadata only."""

    return error_type(
        translation_domain=DOMAIN,
        translation_key=translation_key,
        translation_placeholders=None,
    )


@dataclass(frozen=True, slots=True)
class _ShutdownOutcome:
    completed: bool
    cancellation_args: tuple[object, ...] | None = None


@dataclass(slots=True)
class _EntryLifecycle:
    entry_id: str
    coordinator: WindFreeCoordinator
    start_reauth: Callable[[], None]
    unsubscribe: Callable[[], None] | None = None
    suppressed: bool = True
    reauth_started: bool = False

    def handle_update(self) -> None:
        """Start reauth once, only while the entry is fully active."""

        async_sync_runtime_issues(
            self.coordinator.hass,
            self.entry_id,
            self.coordinator.health,
        )
        if (
            not self.suppressed
            and self.coordinator.authentication_rejected
            and not self.reauth_started
        ):
            self.reauth_started = True
            self.start_reauth()

    def attach(self) -> None:
        """Install exactly one active coordinator listener."""

        if self.unsubscribe is not None:
            return
        self.unsubscribe = self.coordinator.async_add_listener(self.handle_update)
        self.suppressed = False
        self.handle_update()

    def suspend(self) -> None:
        """Suppress first, then detach to close callback races."""

        self.suppressed = True
        unsubscribe = self.unsubscribe
        self.unsubscribe = None
        if unsubscribe is not None:
            unsubscribe()


def _lifecycles(hass: HomeAssistant) -> dict[str, _EntryLifecycle]:
    return hass.data.setdefault(_LIFECYCLE_DATA, {})


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


def _certificate_validity(
    credentials: Credentials,
) -> tuple[datetime, datetime] | None:
    try:
        starts = datetime.fromisoformat(credentials.not_before)
        expires = datetime.fromisoformat(credentials.not_after)
        if starts.tzinfo is None or expires.tzinfo is None:
            raise ValueError
    except TypeError, ValueError:
        return None
    return starts, expires


async def _async_shutdown_cancellation_safe(
    hass: HomeAssistant,
    coordinator: WindFreeCoordinator,
    cancellation_args: tuple[object, ...] | None = None,
) -> _ShutdownOutcome:
    task = hass.async_create_task(
        coordinator.async_shutdown(),
        "windfree entry shutdown",
    )
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            cancellation_args = cancelled.args
            cancelled.__traceback__ = None
            cancelled = None
        except Exception as error:
            error.__traceback__ = None
            error = None

    completed = True
    try:
        task.result()
    except asyncio.CancelledError as cancelled:
        cancelled.__traceback__ = None
        cancelled = None
        completed = False
    except Exception as error:
        error.__traceback__ = None
        error = None
        completed = False
    coordinator = None
    task = None
    del coordinator, task
    return _ShutdownOutcome(completed, cancellation_args)


async def _async_reload_entry(
    hass: HomeAssistant,
    entry: WindFreeConfigEntry,
) -> None:
    """Reload an entry after its atomically updated stored data changes."""

    lifecycle = _lifecycles(hass).get(entry.entry_id)
    if lifecycle is not None:
        lifecycle.suspend()
    reload_failed = False
    cancellation_args: tuple[object, ...] | None = None
    try:
        reloaded = await hass.config_entries.async_reload(entry.entry_id)
        reload_failed = not reloaded
    except asyncio.CancelledError as cancelled:
        cancellation_args = cancelled.args
        cancelled.__traceback__ = None
        cancelled = None
    except Exception as error:
        error.__traceback__ = None
        error = None
        reload_failed = True
    if (
        reload_failed
        and lifecycle is not None
        and _lifecycles(hass).get(entry.entry_id) is lifecycle
    ):
        lifecycle.attach()
    lifecycle = None
    entry = None
    del lifecycle, entry
    if cancellation_args is not None:
        args = cancellation_args
        cancellation_args = None
        del cancellation_args
        raise asyncio.CancelledError(*args) from None
    if reload_failed:
        raise _translated_entry_error(ConfigEntryError, "reload_failed") from None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WindFreeConfigEntry,
) -> bool:
    """Set up exclusively from persisted per-installation material."""

    credentials = _stored_credentials(entry.data)
    if credentials is None:
        entry = None
        raise _translated_entry_error(
            ConfigEntryError,
            "invalid_stored_credentials",
        ) from None
    endpoint = _stored_endpoint(entry.data)
    if endpoint is None:
        credentials = None
        entry = None
        raise _translated_entry_error(
            ConfigEntryError,
            "invalid_stored_entry",
        ) from None
    host, port = endpoint
    endpoint = None
    validity = _certificate_validity(credentials)
    if validity is None:
        host = ""
        credentials = None
        entry = None
        raise _translated_entry_error(
            ConfigEntryError,
            "invalid_stored_credentials",
        ) from None
    starts, expires = validity
    validity = None
    now = dt_util.utcnow()
    async_sync_certificate_issue(
        hass,
        entry.entry_id,
        expires,
        now=now,
    )
    if starts > now:
        host = ""
        credentials = None
        entry.async_start_reauth(hass)
        entry = None
        del host, credentials, starts, expires, now, entry
        raise _translated_entry_error(
            ConfigEntryAuthFailed,
            "credentials_not_yet_valid",
        ) from None
    if expires <= now:
        host = ""
        credentials = None
        entry.async_start_reauth(hass)
        entry = None
        del host, credentials, starts, expires, now, entry
        raise _translated_entry_error(
            ConfigEntryAuthFailed,
            "credentials_expired",
        ) from None

    coordinator = WindFreeCoordinator(
        hass,
        host=host,
        port=port,
        credentials=credentials,
        compatibility=_COMPATIBILITY,
    )
    failure: str | None = None
    cancellation_args: tuple[object, ...] | None = None
    try:
        await coordinator.async_start()
        async_sync_runtime_issues(hass, entry.entry_id, coordinator.health)
        if coordinator.authentication_rejected:
            raise AuthenticationRejected("authentication_rejected")
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        lifecycle = _EntryLifecycle(
            entry.entry_id,
            coordinator,
            partial(entry.async_start_reauth, hass),
        )
        lifecycle.attach()
        _lifecycles(hass)[entry.entry_id] = lifecycle
        entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    except asyncio.CancelledError as cancelled:
        cancellation_args = cancelled.args
        cancelled.__traceback__ = None
        cancelled = None
        failure = "cancelled"
    except AuthenticationRejected as error:
        async_sync_runtime_issues(hass, entry.entry_id, coordinator.health)
        error.__traceback__ = None
        error = None
        failure = "authentication_rejected"
    except UnsupportedDevice as error:
        error.__traceback__ = None
        error = None
        failure = "unsupported_device"
    except CapabilityMismatch as error:
        error.__traceback__ = None
        error = None
        failure = "capability_mismatch"
    except Exception as error:
        error.__traceback__ = None
        error = None
        failure = "setup_failed"

    if failure is None:
        return True

    if failure == "authentication_rejected":
        entry.async_start_reauth(hass)
    shutdown = await _async_shutdown_cancellation_safe(
        hass,
        coordinator,
        cancellation_args,
    )
    cancellation_args = shutdown.cancellation_args
    host = ""
    credentials = None
    entry = None
    coordinator = None
    shutdown = None
    del host, credentials, entry, coordinator, shutdown
    if cancellation_args is not None:
        args = cancellation_args
        cancellation_args = None
        del cancellation_args
        raise asyncio.CancelledError(*args) from None
    if failure == "authentication_rejected":
        raise _translated_entry_error(
            ConfigEntryAuthFailed,
            "authentication_rejected",
        ) from None
    if failure == "unsupported_device":
        raise _translated_entry_error(ConfigEntryError, "unsupported_device") from None
    if failure == "capability_mismatch":
        raise _translated_entry_error(
            ConfigEntryError,
            "capability_mismatch",
        ) from None
    raise _translated_entry_error(ConfigEntryNotReady, "setup_failed") from None


async def async_unload_entry(
    hass: HomeAssistant,
    entry: WindFreeConfigEntry,
) -> bool:
    """Unload entity platforms before terminal coordinator shutdown."""

    lifecycle = _lifecycles(hass).get(entry.entry_id)
    if lifecycle is not None:
        lifecycle.suspend()
    platform_error = False
    cancellation_args: tuple[object, ...] | None = None
    try:
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    except asyncio.CancelledError as cancelled:
        cancellation_args = cancelled.args
        cancelled.__traceback__ = None
        cancelled = None
        unloaded = False
    except Exception as error:
        error.__traceback__ = None
        error = None
        unloaded = False
        platform_error = True

    if not unloaded:
        if lifecycle is not None:
            lifecycle.attach()
        lifecycle = None
        coordinator = None
        entry = None
        del lifecycle, coordinator, entry
        if cancellation_args is not None:
            args = cancellation_args
            cancellation_args = None
            del cancellation_args
            raise asyncio.CancelledError(*args) from None
        if platform_error:
            raise _translated_entry_error(ConfigEntryError, "unload_failed") from None
        return False

    coordinator = entry.runtime_data
    _lifecycles(hass).pop(entry.entry_id, None)
    shutdown = await _async_shutdown_cancellation_safe(hass, coordinator)
    cancellation_args = shutdown.cancellation_args
    completed = shutdown.completed
    if completed:
        async_handle_entry_unload(
            hass,
            entry.entry_id,
            enabled=entry.disabled_by is None,
        )
    shutdown = None
    lifecycle = None
    coordinator = None
    entry = None
    del shutdown, lifecycle, coordinator, entry
    if cancellation_args is not None:
        args = cancellation_args
        cancellation_args = None
        del cancellation_args
        raise asyncio.CancelledError(*args) from None
    if not completed:
        raise _translated_entry_error(ConfigEntryError, "unload_failed") from None
    return True


async def async_remove_entry(
    hass: HomeAssistant,
    entry: WindFreeConfigEntry,
) -> None:
    """Purge private repair state after permanent config-entry removal."""

    async_purge_entry_issues(hass, entry.entry_id)


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: WindFreeConfigEntry,
) -> bool:
    """Upgrade the initial schema minor version without network I/O."""

    if entry.version != 1:
        return False
    if entry.minor_version > _ENTRY_MINOR_VERSION:
        return False
    if entry.minor_version < _ENTRY_MINOR_VERSION:
        hass.config_entries.async_update_entry(
            entry,
            minor_version=_ENTRY_MINOR_VERSION,
        )
    return True
