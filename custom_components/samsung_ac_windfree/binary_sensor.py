"""Local binary sensors for the exact supported Samsung WindFree model."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import WindFreeCoordinator
from .entity import WindFreeEntity

PARALLEL_UPDATES = 0

BINARY_SENSOR_DESCRIPTIONS = (
    BinarySensorEntityDescription(
        key="filter_attention",
        translation_key="filter_attention",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    BinarySensorEntityDescription(
        key="problem",
        translation_key="problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    BinarySensorEntityDescription(
        key="current_limit_enabled",
        translation_key="current_limit_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


class WindFreeBinarySensor(WindFreeEntity, BinarySensorEntity):
    """Project one bounded boolean from the immutable device snapshot."""

    entity_description: BinarySensorEntityDescription

    def __init__(
        self,
        coordinator: WindFreeCoordinator,
        description: BinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, entity_key=description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the exact approved binary state without performing I/O."""

        key = self.entity_description.key
        data = self.coordinator.data
        if key == "filter_attention":
            attention = data.filter.attention
            alarm = data.alarms.filter_alarm
            if not isinstance(attention, bool) or not isinstance(alarm, bool):
                return None
            return attention or alarm
        if key == "problem":
            problem = data.alarms.problem
            return problem if isinstance(problem, bool) else None
        if key == "current_limit_enabled":
            value = data.current_limit_enabled
            return value if isinstance(value, bool) else None
        return None

    @property
    def available(self) -> bool:
        """Require valid model state in addition to coordinator health."""

        return super().available and self.is_on is not None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[WindFreeCoordinator],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the exact approved binary-sensor inventory."""

    async_add_entities(
        WindFreeBinarySensor(entry.runtime_data, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )
