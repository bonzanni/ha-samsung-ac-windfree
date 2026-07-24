"""Zero-I/O privacy allowlist for Samsung WindFree diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import SUPPORTED_MODEL
from .coordinator import ResourceCoverage, WindFreeCoordinator

_INTEGRATION_VERSION = "0.1.0"
_DEPENDENCY_VERSION = "0.1.0"


def _certificate(
    data: object,
    now: datetime,
) -> dict[str, str | int | None]:
    starts: datetime | None = None
    expires: datetime | None = None
    if isinstance(data, Mapping):
        try:
            start_value = data.get("not_before")
            expiry_value = data.get("not_after")
        except Exception as error:
            error.__traceback__ = None
            error = None
            start_value = None
            expiry_value = None
        if isinstance(start_value, str) and len(start_value) <= 64:
            try:
                candidate = datetime.fromisoformat(start_value)
            except ValueError:
                pass
            else:
                if candidate.tzinfo is not None:
                    starts = candidate
        if isinstance(expiry_value, str) and len(expiry_value) <= 64:
            try:
                candidate = datetime.fromisoformat(expiry_value)
            except ValueError:
                pass
            else:
                if candidate.tzinfo is not None:
                    expires = candidate
    days = None
    if expires is not None and now.tzinfo is not None:
        days = int((expires - now).total_seconds() // 86400)
    return {
        "not_before": starts.isoformat() if starts is not None else None,
        "not_after": expires.isoformat() if expires is not None else None,
        "days_to_expiry": days,
    }


def _empty_coverage() -> ResourceCoverage:
    return ResourceCoverage(
        power=False,
        hvac_mode=False,
        temperature=False,
        fan=False,
        swing=False,
        preset=False,
        humidity=False,
        energy=False,
        alarms=False,
        display_light=False,
        auto_clean=False,
        filter=False,
        current_limit=False,
    )


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return fixed scalar diagnostics without transport or registry I/O."""

    del hass
    try:
        coordinator = entry.runtime_data
    except Exception as error:
        error.__traceback__ = None
        error = None
        coordinator = None
    if type(coordinator) is WindFreeCoordinator:
        health = coordinator.health
        coverage = coordinator.resource_coverage
        connection = {
            "available": health.available,
            "generation": health.generation,
            "reason": health.connection_reason
            or ("connected" if health.available else "disconnected"),
            "failure_count": health.failure_count,
            "reconnect_attempts": health.reconnect_attempts,
            "reconnect_delay_seconds": health.reconnect_delay_seconds,
            "port_range_exhausted": health.port_range_exhausted,
        }
        updates = {
            "source": health.source.value,
            "hot_age_seconds": health.hot_age_seconds,
            "poll_count": health.poll_count,
            "observe_count": health.observe_count,
            "reconcile_count": health.reconcile_count,
            "command_count": health.command_count,
            "latency_under_100ms": health.latency_under_100ms,
            "latency_under_500ms": health.latency_under_500ms,
            "latency_under_1s": health.latency_under_1s,
            "latency_at_least_1s": health.latency_at_least_1s,
        }
    else:
        coverage = _empty_coverage()
        connection = {
            "available": False,
            "generation": 0,
            "reason": "not_loaded",
            "failure_count": 0,
            "reconnect_attempts": 0,
            "reconnect_delay_seconds": 0,
            "port_range_exhausted": False,
        }
        updates = {
            "source": "none",
            "hot_age_seconds": 0.0,
            "poll_count": 0,
            "observe_count": 0,
            "reconcile_count": 0,
            "command_count": 0,
            "latency_under_100ms": 0,
            "latency_under_500ms": 0,
            "latency_under_1s": 0,
            "latency_at_least_1s": 0,
        }

    resource_coverage = {
        "power": coverage.power,
        "hvac_mode": coverage.hvac_mode,
        "temperature": coverage.temperature,
        "fan": coverage.fan,
        "swing": coverage.swing,
        "preset": coverage.preset,
        "humidity": coverage.humidity,
        "energy": coverage.energy,
        "alarms": coverage.alarms,
        "display_light": coverage.display_light,
        "auto_clean": coverage.auto_clean,
        "filter": coverage.filter,
        "current_limit": coverage.current_limit,
    }
    entity_support = {
        "climate": all(
            (
                coverage.power,
                coverage.hvac_mode,
                coverage.temperature,
                coverage.fan,
                coverage.swing,
                coverage.preset,
            )
        ),
        "humidity": coverage.humidity,
        "filter_usage": coverage.filter,
        "filter_status": coverage.filter,
        "energy_consumption": coverage.energy,
        "active_alarm": coverage.alarms,
        "problem": coverage.alarms,
        "filter_attention": coverage.filter,
        "display_light": coverage.display_light,
        "auto_clean": coverage.auto_clean,
        "current_limit_enabled": coverage.current_limit,
        "current_limit_level": coverage.current_limit,
    }
    try:
        entry_data = entry.data
    except Exception as error:
        error.__traceback__ = None
        error = None
        entry_data = {}
    result = {
        "integration_version": _INTEGRATION_VERSION,
        "dependency_version": _DEPENDENCY_VERSION,
        "supported_product": SUPPORTED_MODEL,
        "connection": connection,
        "updates": updates,
        "resource_coverage": resource_coverage,
        "certificate": _certificate(entry_data, now or dt_util.utcnow()),
        "entity_support": entity_support,
    }
    entry_data = None
    entry = None
    del entry_data, entry
    return result
