"""Shared coordinator entity for the exact supported WindFree device."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WindFreeCoordinator


class WindFreeEntity(CoordinatorEntity[WindFreeCoordinator]):
    """Base entity backed only by immutable coordinator snapshots."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WindFreeCoordinator,
        *,
        entity_key: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        identity = coordinator.data.identity
        if identity is None:
            raise ValueError("identity_unavailable") from None
        self._attr_unique_id = (
            identity.device_id
            if entity_key is None
            else f"{identity.device_id}_{entity_key}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity.device_id)},
            manufacturer="Samsung",
            model=identity.model,
            sw_version=identity.firmware,
            hw_version=identity.platform,
        )

    @property
    def available(self) -> bool:
        """Require both coordinator health and an available device snapshot."""

        return super().available and self.coordinator.data.available
