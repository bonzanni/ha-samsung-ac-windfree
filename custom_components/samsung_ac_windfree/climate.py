"""Climate entity for the exact supported Samsung WindFree model."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from types import MappingProxyType
from typing import Any

from homeassistant.components.climate import (
    ATTR_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, PRESETS_BY_MODE
from .coordinator import WindFreeCoordinator
from .device import CommandKind
from .entity import WindFreeEntity
from .models import (
    CommandRejected,
    FanMode,
    HvacMode,
    PresetMode,
    SwingMode,
)

PARALLEL_UPDATES = 0

HVAC_TO_HA = MappingProxyType(
    {
        HvacMode.AUTO: HVACMode.AUTO,
        HvacMode.COOL: HVACMode.COOL,
        HvacMode.DRY: HVACMode.DRY,
        HvacMode.FAN: HVACMode.FAN_ONLY,
        HvacMode.HEAT: HVACMode.HEAT,
    }
)
FAN_TO_HA = MappingProxyType(
    {
        FanMode.AUTO: "auto",
        FanMode.LOW: "low",
        FanMode.MEDIUM: "medium",
        FanMode.HIGH: "high",
        FanMode.TURBO: "turbo",
    }
)
SWING_TO_HA = MappingProxyType(
    {
        SwingMode.FIXED: "fixed",
        SwingMode.VERTICAL: "vertical",
        SwingMode.HORIZONTAL: "horizontal",
        SwingMode.BOTH: "both",
    }
)
PRESET_TO_HA = MappingProxyType(
    {
        PresetMode.NONE: "none",
        PresetMode.QUIET: "quiet",
        PresetMode.SMART: "smart",
        PresetMode.BOOST: "boost",
        PresetMode.WINDFREE: "windfree",
        PresetMode.WINDFREE_SLEEP: "windfree_sleep",
        PresetMode.SLEEP: "sleep",
        PresetMode.DRY_COMFORT: "dry_comfort",
    }
)

_HA_TO_HVAC = MappingProxyType({value: key for key, value in HVAC_TO_HA.items()})
_HA_TO_FAN = MappingProxyType({value: key for key, value in FAN_TO_HA.items()})
_HA_TO_SWING = MappingProxyType({value: key for key, value in SWING_TO_HA.items()})
_HA_TO_PRESET = MappingProxyType({value: key for key, value in PRESET_TO_HA.items()})
_HVAC_MODES = (HVACMode.OFF, *HVAC_TO_HA.values())
_FAN_MODES = tuple(FAN_TO_HA.values())
_SWING_MODES = tuple(SWING_TO_HA.values())
_MODE_GATED = frozenset(
    {
        CommandKind.TEMPERATURE,
        CommandKind.FAN,
        CommandKind.SWING,
        CommandKind.PRESET,
    }
)


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


class WindFreeClimate(WindFreeEntity, ClimateEntity):
    """Expose local state and delegate every write to the coordinator."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 16.0
    _attr_max_temp = 30.0
    _attr_target_temperature_step = 1.0
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    @property
    def hvac_mode(self) -> HVACMode:
        """Return power and remembered mode as one HA HVAC state."""

        climate = self.coordinator.data.climate
        return HVAC_TO_HA[climate.mode] if climate.power else HVACMode.OFF

    @property
    def hvac_modes(self) -> list[HVACMode]:
        return list(_HVAC_MODES)

    @property
    def current_temperature(self) -> float | None:
        return self.coordinator.data.climate.current_temperature

    @property
    def target_temperature(self) -> float:
        return self.coordinator.data.climate.target_temperature

    @property
    def current_humidity(self) -> int | None:
        return self.coordinator.data.climate.humidity

    @property
    def fan_mode(self) -> str:
        return FAN_TO_HA[self.coordinator.data.climate.fan_mode]

    @property
    def fan_modes(self) -> list[str]:
        return list(_FAN_MODES)

    @property
    def swing_mode(self) -> str:
        return SWING_TO_HA[self.coordinator.data.climate.swing_mode]

    @property
    def swing_modes(self) -> list[str]:
        return list(_SWING_MODES)

    @property
    def preset_mode(self) -> str:
        return PRESET_TO_HA[self.coordinator.data.climate.preset_mode]

    @property
    def preset_modes(self) -> list[str]:
        return [
            PRESET_TO_HA[preset]
            for preset in PRESETS_BY_MODE.get(
                self.coordinator.data.climate.mode,
                (),
            )
        ]

    def _require_compatible(
        self,
        kind: CommandKind,
        value: object = None,
    ) -> None:
        if kind not in _MODE_GATED:
            return
        mode = self.coordinator.data.climate.mode
        if kind.value not in self.coordinator.data.contract.mode_controls.get(
            mode, frozenset()
        ):
            _raise_translated("command_incompatible")
        if kind is CommandKind.PRESET and value not in PRESETS_BY_MODE.get(mode, ()):
            _raise_translated("command_incompatible")

    async def _async_delegate(
        self,
        operation: Callable[..., Awaitable[None]] | None,
        *args: object,
    ) -> None:
        failure: str | None = None
        cancellation_args: tuple[object, ...] | None = None
        try:
            assert operation is not None
            await operation(*args)
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
        operation = None
        args = ()
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

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if not isinstance(hvac_mode, HVACMode):
            hvac_mode = HVACMode.OFF
            _raise_translated("invalid_command")
        if hvac_mode is HVACMode.OFF:
            try:
                await self._async_delegate(self.coordinator.async_turn_off)
            finally:
                hvac_mode = HVACMode.OFF
            return
        domain_mode = _HA_TO_HVAC.get(hvac_mode)
        if domain_mode is None:
            hvac_mode = HVACMode.OFF
            _raise_translated("invalid_command")
        try:
            await self._async_delegate(
                self.coordinator.async_set_hvac_mode,
                domain_mode,
            )
        finally:
            hvac_mode = HVACMode.OFF
            domain_mode = None

    async def async_turn_on(self) -> None:
        await self._async_delegate(self.coordinator.async_turn_on)

    async def async_turn_off(self) -> None:
        await self._async_delegate(self.coordinator.async_turn_off)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        value = kwargs.get(ATTR_TEMPERATURE)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not self.min_temp <= value <= self.max_temp
            or not math.isfinite(value)
            or not float(value).is_integer()
        ):
            value = None
            kwargs.clear()
            _raise_translated("invalid_temperature")
        try:
            self._require_compatible(CommandKind.TEMPERATURE)
            await self._async_delegate(
                self.coordinator.async_command,
                CommandKind.TEMPERATURE,
                float(value),
            )
        finally:
            value = None
            kwargs.clear()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        value = _HA_TO_FAN.get(fan_mode) if isinstance(fan_mode, str) else None
        if value is None:
            fan_mode = ""
            _raise_translated("invalid_command")
        try:
            self._require_compatible(CommandKind.FAN)
            await self._async_delegate(
                self.coordinator.async_command,
                CommandKind.FAN,
                value,
            )
        finally:
            fan_mode = ""
            value = None

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        value = _HA_TO_SWING.get(swing_mode) if isinstance(swing_mode, str) else None
        if value is None:
            swing_mode = ""
            _raise_translated("invalid_command")
        try:
            self._require_compatible(CommandKind.SWING)
            await self._async_delegate(
                self.coordinator.async_command,
                CommandKind.SWING,
                value,
            )
        finally:
            swing_mode = ""
            value = None

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        value = _HA_TO_PRESET.get(preset_mode) if isinstance(preset_mode, str) else None
        if value is None:
            preset_mode = ""
            _raise_translated("invalid_command")
        try:
            self._require_compatible(CommandKind.PRESET, value)
            await self._async_delegate(
                self.coordinator.async_command,
                CommandKind.PRESET,
                value,
            )
        finally:
            preset_mode = ""
            value = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[WindFreeCoordinator],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the single primary climate entity."""

    async_add_entities([WindFreeClimate(entry.runtime_data)])
