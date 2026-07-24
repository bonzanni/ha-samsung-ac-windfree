"""Local state sensors for the exact supported Samsung WindFree model."""

from __future__ import annotations

import math
import re

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import WindFreeCoordinator
from .entity import WindFreeEntity

_FILTER_STATUSES = ("normal", "wash", "replace")
_ALARM_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_MAX_ENERGY_KWH = (2**53 - 1) / 1000
_MAX_CURRENT_LIMIT_LEVEL = 2**31 - 1

SENSOR_DESCRIPTIONS = (
    SensorEntityDescription(
        key="filter_usage",
        translation_key="filter_usage",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
    ),
    SensorEntityDescription(
        key="filter_status",
        translation_key="filter_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(_FILTER_STATUSES),
    ),
    SensorEntityDescription(
        key="energy_consumption",
        translation_key="energy_consumption",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
    ),
    SensorEntityDescription(
        key="active_alarm",
        translation_key="active_alarm",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="current_limit_level",
        translation_key="current_limit_level",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


def _filter_usage(used: object, capacity: object) -> float | None:
    if (
        isinstance(used, bool)
        or not isinstance(used, int)
        or used < 0
        or isinstance(capacity, bool)
        or not isinstance(capacity, int)
        or capacity <= 0
    ):
        return None
    if used >= capacity:
        return 100.0
    try:
        percentage = used / capacity * 100
    except OverflowError:
        return None
    return percentage if math.isfinite(percentage) else None


def _energy_value(value: object) -> float | int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 <= value <= _MAX_ENERGY_KWH
    ):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _alarm_code(value: object) -> str | None:
    if not isinstance(value, str) or _ALARM_CODE.fullmatch(value) is None:
        return None
    return value


def _current_limit_level(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_CURRENT_LIMIT_LEVEL
    ):
        return None
    return value


class WindFreeSensor(WindFreeEntity, SensorEntity):
    """Project one validated field from the immutable device snapshot."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        coordinator: WindFreeCoordinator,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, entity_key=description.key)
        self.entity_description = description

    @property
    def options(self) -> list[str] | None:
        """Return an isolated list for the translated filter enum."""

        if self.entity_description.key == "filter_status":
            return list(_FILTER_STATUSES)
        return None

    @property
    def native_value(self) -> float | int | str | None:
        """Return a bounded, user-facing value without performing I/O."""

        key = self.entity_description.key
        data = self.coordinator.data
        if key == "filter_usage":
            return _filter_usage(data.filter.used, data.filter.capacity)
        if key == "filter_status":
            status = data.filter.status
            return status if status in _FILTER_STATUSES else None
        if key == "energy_consumption":
            return _energy_value(data.energy.cumulative_kwh)
        if key == "active_alarm":
            return _alarm_code(data.alarms.active_code)
        if key == "current_limit_level":
            return _current_limit_level(data.current_limit_level)
        return None

    @property
    def available(self) -> bool:
        """Require both coordinator health and a validated field value."""

        return super().available and self.native_value is not None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[WindFreeCoordinator],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the exact approved sensor inventory."""

    async_add_entities(
        WindFreeSensor(entry.runtime_data, description)
        for description in SENSOR_DESCRIPTIONS
    )
