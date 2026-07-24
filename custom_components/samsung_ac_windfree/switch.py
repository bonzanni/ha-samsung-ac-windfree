"""Local settings switches for the exact supported Samsung WindFree model."""

from __future__ import annotations

import asyncio

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import WindFreeCoordinator
from .device import CommandKind
from .entity import WindFreeEntity
from .models import CommandRejected

PARALLEL_UPDATES = 0

SWITCH_DESCRIPTIONS = (
    SwitchEntityDescription(
        key="auto_clean",
        translation_key="auto_clean",
    ),
    SwitchEntityDescription(
        key="display_light",
        translation_key="display_light",
    ),
)

_COMMAND_KIND = {
    "auto_clean": CommandKind.AUTO_CLEAN,
    "display_light": CommandKind.DISPLAY_LIGHT,
}


def _translated_error(key: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
    )


def _raise_translated(key: str) -> None:
    try:
        raise _translated_error(key) from None
    finally:
        key = ""


def _raise_cancelled(args: tuple[object, ...]) -> None:
    try:
        raise asyncio.CancelledError(*args) from None
    finally:
        args = ()


class WindFreeSwitch(WindFreeEntity, SwitchEntity):
    """Project one immutable setting and delegate local writes."""

    entity_description: SwitchEntityDescription

    def __init__(
        self,
        coordinator: WindFreeCoordinator,
        description: SwitchEntityDescription,
    ) -> None:
        super().__init__(coordinator, entity_key=description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the authoritative setting when it is a strict boolean."""

        value = getattr(self.coordinator.data, self.entity_description.key, None)
        return value if isinstance(value, bool) else None

    @property
    def available(self) -> bool:
        """Require both coordinator health and a valid setting value."""

        return super().available and self.is_on is not None

    async def _async_set(self, value: bool) -> None:
        kind: CommandKind | None = _COMMAND_KIND[self.entity_description.key]
        failure: str | None = None
        cancellation_args: tuple[object, ...] | None = None
        try:
            await self.coordinator.async_command(kind, value)
        except asyncio.CancelledError as error:
            cancellation_args = error.args
            error.__traceback__ = None
            error = None
        except CommandRejected as error:
            category = str(error)
            failure = (
                category
                if category
                in {
                    "command_unavailable",
                    "command_incompatible",
                    "command_rejected",
                }
                else "command_failed"
            )
            category = None
            error.__traceback__ = None
            error = None
        kind = None
        value = False
        if cancellation_args is not None:
            try:
                _raise_cancelled(cancellation_args)
            finally:
                cancellation_args = None
        if failure is not None:
            try:
                _raise_translated(failure)
            finally:
                failure = None

    async def async_turn_on(self, **kwargs: object) -> None:
        """Enable the local setting."""

        kwargs.clear()
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Disable the local setting."""

        kwargs.clear()
        await self._async_set(False)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[WindFreeCoordinator],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the two model-specific setting switches."""

    async_add_entities(
        WindFreeSwitch(entry.runtime_data, description)
        for description in SWITCH_DESCRIPTIONS
    )
