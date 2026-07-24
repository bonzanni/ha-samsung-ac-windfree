from __future__ import annotations

import asyncio
from dataclasses import replace
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, call

import pytest
import voluptuous as vol
from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    ATTR_SWING_MODE,
    ATTR_TEMPERATURE,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, Platform, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_ac_windfree.climate import (
    FAN_TO_HA,
    HVAC_TO_HA,
    PRESET_TO_HA,
    SWING_TO_HA,
    WindFreeClimate,
    async_setup_entry,
)
from custom_components.samsung_ac_windfree.const import DOMAIN
from custom_components.samsung_ac_windfree.coordinator import WindFreeCoordinator
from custom_components.samsung_ac_windfree.device import CommandKind
from custom_components.samsung_ac_windfree.models import (
    CapabilityContract,
    ClimateState,
    CommandRejected,
    DeviceIdentity,
    FanMode,
    HvacMode,
    PresetMode,
    SwingMode,
    WindFreeData,
)


@pytest.fixture
def coordinator() -> MagicMock:
    coordinator = MagicMock(spec=WindFreeCoordinator)
    coordinator.data = replace(
        WindFreeData.empty(),
        available=True,
        identity=DeviceIdentity(
            device_id="00000000-0000-4000-8000-000000000001",
            model="AR60F12C1AWNEU",
            device_type="oic.d.airconditioner",
            firmware="TP1X_DA-AC-RAC-01001_001",
            platform="TizenRT 4.0",
        ),
        climate=ClimateState(
            current_temperature=26,
            target_temperature=26,
            humidity=36,
        ),
        contract=CapabilityContract(
            mode_controls=MappingProxyType(
                {
                    HvacMode.AUTO: frozenset(),
                    HvacMode.COOL: frozenset({"temperature", "fan", "swing", "preset"}),
                    HvacMode.DRY: frozenset(),
                    HvacMode.FAN: frozenset(),
                    HvacMode.HEAT: frozenset(),
                }
            )
        ),
    )
    coordinator.last_update_success = True
    coordinator.async_command = AsyncMock()
    coordinator.async_set_hvac_mode = AsyncMock()
    coordinator.async_turn_on = AsyncMock()
    coordinator.async_turn_off = AsyncMock()
    return coordinator


@pytest.fixture
def climate(coordinator: MagicMock) -> WindFreeClimate:
    return WindFreeClimate(coordinator)


def _climate_traceback_locals(error: BaseException) -> str:
    values: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        if (
            traceback.tb_frame.f_globals.get("__name__")
            == "custom_components.samsung_ac_windfree.climate"
        ):
            values.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    return "\n".join(values)


@pytest.fixture
async def live_climate_entity(
    hass: HomeAssistant,
    coordinator: MagicMock,
) -> tuple[str, MagicMock]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Samsung WindFree AC",
        unique_id=coordinator.data.identity.device_id,
        state=ConfigEntryState.LOADED,
    )
    entry.runtime_data = coordinator
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "climate", {})
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.CLIMATE])
    await hass.async_block_till_done()
    entity_ids = hass.states.async_entity_ids("climate")
    assert len(entity_ids) == 1
    yield entity_ids[0], coordinator
    for entity in tuple(hass.data["climate"].entities):
        await entity.async_remove()


async def test_platform_adds_exactly_one_climate_entity(coordinator) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="entry-device")
    entry.runtime_data = coordinator
    added: list[WindFreeClimate] = []

    await async_setup_entry(None, entry, added.extend)  # type: ignore[arg-type]

    assert len(added) == 1
    assert isinstance(added[0], WindFreeClimate)
    assert added[0].coordinator is coordinator


def test_identity_device_info_and_privacy(climate, coordinator) -> None:
    identity = coordinator.data.identity
    assert identity is not None
    assert climate.unique_id == identity.device_id
    assert climate.name is None
    assert climate.device_info == {
        "identifiers": {(DOMAIN, identity.device_id)},
        "manufacturer": "Samsung",
        "model": identity.model,
        "sw_version": identity.firmware,
        "hw_version": identity.platform,
    }
    info_text = repr(climate.device_info)
    assert "serial_number" not in climate.device_info
    assert info_text.count(identity.device_id) == 1


def test_climate_exposes_complete_local_state(climate, coordinator) -> None:
    state = coordinator.data.climate
    assert climate.hvac_mode is HVACMode.OFF
    assert climate.hvac_modes == [HVACMode.OFF, *HVAC_TO_HA.values()]
    assert climate.current_temperature == state.current_temperature == 26
    assert climate.target_temperature == state.target_temperature == 26
    assert climate.current_humidity == state.humidity == 36
    assert climate.fan_mode == "auto"
    assert climate.fan_modes == list(FAN_TO_HA.values())
    assert climate.swing_mode == "fixed"
    assert climate.swing_modes == list(SWING_TO_HA.values())
    assert climate.preset_mode == "none"
    assert climate.preset_modes == list(PRESET_TO_HA.values())
    assert climate.temperature_unit is UnitOfTemperature.CELSIUS
    assert climate.min_temp == 16
    assert climate.max_temp == 30
    assert climate.target_temperature_step == 1
    assert climate.hvac_action is None


def test_supported_features_are_exact(climate) -> None:
    assert climate.supported_features == (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )


def test_mapping_tables_and_advertised_lists_are_isolated(coordinator) -> None:
    first = WindFreeClimate(coordinator)
    second = WindFreeClimate(coordinator)

    with pytest.raises(TypeError):
        HVAC_TO_HA[HvacMode.COOL] = HVACMode.HEAT  # type: ignore[index]
    with pytest.raises(TypeError):
        FAN_TO_HA[FanMode.AUTO] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        SWING_TO_HA[SwingMode.FIXED] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        PRESET_TO_HA[PresetMode.NONE] = "mutated"  # type: ignore[index]

    first.hvac_modes.append(HVACMode.HEAT_COOL)
    first.fan_modes.append("mutated")
    first.swing_modes.append("mutated")
    first.preset_modes.append("mutated")

    assert HVACMode.HEAT_COOL not in second.hvac_modes
    assert "mutated" not in second.fan_modes
    assert "mutated" not in second.swing_modes
    assert "mutated" not in second.preset_modes
    assert first.hvac_modes is not second.hvac_modes
    assert first.fan_modes is not second.fan_modes
    assert first.swing_modes is not second.swing_modes
    assert first.preset_modes is not second.preset_modes


@pytest.mark.parametrize(
    ("domain_mode", "ha_mode"),
    [
        (HvacMode.AUTO, HVACMode.AUTO),
        (HvacMode.COOL, HVACMode.COOL),
        (HvacMode.DRY, HVACMode.DRY),
        (HvacMode.FAN, HVACMode.FAN_ONLY),
        (HvacMode.HEAT, HVACMode.HEAT),
    ],
)
def test_hvac_state_mappings(climate, coordinator, domain_mode, ha_mode) -> None:
    coordinator.data = replace(
        coordinator.data,
        climate=replace(
            coordinator.data.climate,
            power=True,
            mode=domain_mode,
        ),
    )
    assert climate.hvac_mode is ha_mode


@pytest.mark.parametrize("domain_mode,ha_mode", list(FAN_TO_HA.items()))
def test_fan_state_mappings(climate, coordinator, domain_mode, ha_mode) -> None:
    coordinator.data = replace(
        coordinator.data,
        climate=replace(coordinator.data.climate, fan_mode=domain_mode),
    )
    assert climate.fan_mode == ha_mode


@pytest.mark.parametrize("domain_mode,ha_mode", list(SWING_TO_HA.items()))
def test_swing_state_mappings(climate, coordinator, domain_mode, ha_mode) -> None:
    coordinator.data = replace(
        coordinator.data,
        climate=replace(coordinator.data.climate, swing_mode=domain_mode),
    )
    assert climate.swing_mode == ha_mode


@pytest.mark.parametrize("domain_mode,ha_mode", list(PRESET_TO_HA.items()))
def test_preset_state_mappings(climate, coordinator, domain_mode, ha_mode) -> None:
    coordinator.data = replace(
        coordinator.data,
        climate=replace(coordinator.data.climate, preset_mode=domain_mode),
    )
    assert climate.preset_mode == ha_mode


def test_availability_follows_immutable_device_snapshot(climate, coordinator) -> None:
    coordinator.last_update_success = True
    assert climate.available
    coordinator.data = replace(coordinator.data, available=False)
    assert not climate.available
    coordinator.data = replace(coordinator.data, available=True)
    coordinator.last_update_success = False
    assert not climate.available


def test_properties_perform_no_io(climate, coordinator) -> None:
    coordinator.async_command = AsyncMock(
        side_effect=AssertionError("property performed I/O")
    )
    coordinator.async_set_hvac_mode = AsyncMock(
        side_effect=AssertionError("property performed I/O")
    )
    coordinator.async_turn_on = AsyncMock(
        side_effect=AssertionError("property performed I/O")
    )
    coordinator.async_turn_off = AsyncMock(
        side_effect=AssertionError("property performed I/O")
    )

    _ = (
        climate.available,
        climate.device_info,
        climate.unique_id,
        climate.hvac_mode,
        climate.hvac_modes,
        climate.current_temperature,
        climate.target_temperature,
        climate.current_humidity,
        climate.fan_mode,
        climate.fan_modes,
        climate.swing_mode,
        climate.swing_modes,
        climate.preset_mode,
        climate.preset_modes,
        climate.supported_features,
    )

    coordinator.async_command.assert_not_awaited()
    coordinator.async_set_hvac_mode.assert_not_awaited()
    coordinator.async_turn_on.assert_not_awaited()
    coordinator.async_turn_off.assert_not_awaited()


@pytest.mark.parametrize(
    ("ha_mode", "domain_mode"),
    [
        (HVACMode.AUTO, HvacMode.AUTO),
        (HVACMode.COOL, HvacMode.COOL),
        (HVACMode.DRY, HvacMode.DRY),
        (HVACMode.FAN_ONLY, HvacMode.FAN),
        (HVACMode.HEAT, HvacMode.HEAT),
    ],
)
async def test_setting_mode_delegates_one_logical_operation(
    climate, coordinator, ha_mode, domain_mode
) -> None:
    coordinator.async_set_hvac_mode = AsyncMock()
    coordinator.async_command = AsyncMock()

    await climate.async_set_hvac_mode(ha_mode)

    coordinator.async_set_hvac_mode.assert_awaited_once_with(domain_mode)
    coordinator.async_command.assert_not_awaited()


async def test_setting_off_delegates_to_turn_off_once(climate, coordinator) -> None:
    coordinator.async_turn_off = AsyncMock()
    coordinator.async_set_hvac_mode = AsyncMock()

    await climate.async_set_hvac_mode(HVACMode.OFF)

    coordinator.async_turn_off.assert_awaited_once_with()
    coordinator.async_set_hvac_mode.assert_not_awaited()


async def test_explicit_power_services_delegate_once(climate, coordinator) -> None:
    coordinator.async_turn_on = AsyncMock()
    coordinator.async_turn_off = AsyncMock()

    await climate.async_turn_on()
    await climate.async_turn_off()

    coordinator.async_turn_on.assert_awaited_once_with()
    coordinator.async_turn_off.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("method", "argument", "kind", "domain_value"),
    [
        ("async_set_fan_mode", "medium", CommandKind.FAN, FanMode.MEDIUM),
        ("async_set_swing_mode", "both", CommandKind.SWING, SwingMode.BOTH),
        ("async_set_preset_mode", "windfree", CommandKind.PRESET, PresetMode.WINDFREE),
    ],
)
async def test_controls_delegate_one_command(
    climate, coordinator, method, argument, kind, domain_value
) -> None:
    coordinator.async_command = AsyncMock()

    await getattr(climate, method)(argument)

    coordinator.async_command.assert_awaited_once_with(kind, domain_value)


@pytest.mark.parametrize("temperature", [16, 21, 30])
async def test_temperature_limits_delegate_exactly_once(
    climate, coordinator, temperature
) -> None:
    coordinator.async_command = AsyncMock()

    await climate.async_set_temperature(**{ATTR_TEMPERATURE: temperature})

    coordinator.async_command.assert_awaited_once_with(
        CommandKind.TEMPERATURE,
        float(temperature),
    )


@pytest.mark.parametrize(
    ("method", "argument"),
    [
        ("async_set_hvac_mode", HVACMode.HEAT_COOL),
        ("async_set_hvac_mode", "Cool"),
        ("async_set_fan_mode", "invalid"),
        ("async_set_fan_mode", []),
        ("async_set_swing_mode", "invalid"),
        ("async_set_swing_mode", []),
        ("async_set_preset_mode", "invalid"),
        ("async_set_preset_mode", []),
    ],
)
async def test_invalid_control_is_translated_and_never_reaches_coordinator(
    climate, coordinator, method, argument
) -> None:
    coordinator.async_command = AsyncMock()
    coordinator.async_set_hvac_mode = AsyncMock()
    coordinator.async_turn_off = AsyncMock()

    with pytest.raises(HomeAssistantError) as caught:
        await getattr(climate, method)(argument)

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "invalid_command"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    coordinator.async_command.assert_not_awaited()
    coordinator.async_set_hvac_mode.assert_not_awaited()
    coordinator.async_turn_off.assert_not_awaited()


@pytest.mark.parametrize("temperature", [15, 31, 16.5, "warm", None, 10**1000])
async def test_invalid_temperature_is_translated_without_transport(
    climate, coordinator, temperature
) -> None:
    coordinator.async_command = AsyncMock()

    with pytest.raises(HomeAssistantError) as caught:
        await climate.async_set_temperature(**{ATTR_TEMPERATURE: temperature})

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "invalid_temperature"
    assert caught.value.__cause__ is None
    coordinator.async_command.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "argument"),
    [
        ("async_set_fan_mode", "low"),
        ("async_set_swing_mode", "vertical"),
        ("async_set_preset_mode", "quiet"),
    ],
)
async def test_matrix_incompatible_control_is_rejected_before_transport(
    climate, coordinator, method, argument
) -> None:
    coordinator.data = replace(
        coordinator.data,
        climate=replace(coordinator.data.climate, mode=HvacMode.AUTO),
    )
    coordinator.async_command = AsyncMock()

    with pytest.raises(HomeAssistantError) as caught:
        await getattr(climate, method)(argument)

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "command_incompatible"
    assert caught.value.__cause__ is None
    coordinator.async_command.assert_not_awaited()


async def test_matrix_uses_remembered_mode_while_power_is_off(
    climate, coordinator
) -> None:
    assert coordinator.data.climate.power is False
    assert coordinator.data.climate.mode is HvacMode.COOL
    coordinator.async_command = AsyncMock()

    await climate.async_set_fan_mode("high")

    coordinator.async_command.assert_awaited_once_with(CommandKind.FAN, FanMode.HIGH)


@pytest.mark.parametrize(
    "method,argument",
    [
        ("async_set_temperature", {ATTR_TEMPERATURE: 27}),
        ("async_set_fan_mode", "low"),
        ("async_set_swing_mode", "vertical"),
        ("async_set_preset_mode", "quiet"),
    ],
)
async def test_sanitizes_coordinator_command_rejection(
    climate, coordinator, method, argument
) -> None:
    coordinator.async_command = AsyncMock(
        side_effect=CommandRejected("command_rejected")
    )

    with pytest.raises(HomeAssistantError) as caught:
        if isinstance(argument, dict):
            await getattr(climate, method)(**argument)
        else:
            await getattr(climate, method)(argument)

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "command_rejected"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_cancellation_is_preserved(climate, coordinator) -> None:
    coordinator.async_command = AsyncMock(
        side_effect=asyncio.CancelledError("climate_cancelled")
    )

    with pytest.raises(asyncio.CancelledError, match="climate_cancelled"):
        await climate.async_set_fan_mode("low")


async def test_rejection_traceback_does_not_retain_raw_error_or_call(
    climate, coordinator
) -> None:
    secret = "192.0.2.60 PRIVATE-LIVE-REJECTION"
    coordinator.async_command = AsyncMock(side_effect=CommandRejected(secret))

    with pytest.raises(HomeAssistantError) as caught:
        await climate.async_set_fan_mode("low")

    assert caught.value.translation_key == "command_failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    locals_text = _climate_traceback_locals(caught.value)
    assert secret not in locals_text
    assert "CommandRejected" not in locals_text
    assert "async_command" not in locals_text
    assert "FanMode.LOW" not in locals_text


async def test_invalid_caller_value_is_scrubbed_from_traceback(
    climate, coordinator
) -> None:
    secret = "192.0.2.60 PRIVATE-CALLER-VALUE"

    with pytest.raises(HomeAssistantError) as caught:
        await climate.async_set_fan_mode(secret)

    assert caught.value.translation_key == "invalid_command"
    assert secret not in _climate_traceback_locals(caught.value)


async def test_cancellation_traceback_scrubs_call_and_preserves_exact_args(
    climate, coordinator
) -> None:
    secret = "192.0.2.60 PRIVATE-CANCEL"
    coordinator.async_command = AsyncMock(
        side_effect=asyncio.CancelledError(secret, 17)
    )

    with pytest.raises(asyncio.CancelledError) as caught:
        await climate.async_set_fan_mode("low")

    assert caught.value.args == (secret, 17)
    locals_text = _climate_traceback_locals(caught.value)
    assert secret not in locals_text
    assert "async_command" not in locals_text
    assert "FanMode.LOW" not in locals_text


async def test_real_platform_projects_state(live_climate_entity, hass) -> None:
    entity_id, _coordinator = live_climate_entity
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == HVACMode.OFF
    assert state.attributes["current_temperature"] == 26
    assert state.attributes["temperature"] == 26
    assert state.attributes["current_humidity"] == 36
    assert state.attributes["fan_mode"] == "auto"
    assert state.attributes["fan_modes"] == list(FAN_TO_HA.values())
    assert state.attributes["swing_mode"] == "fixed"
    assert state.attributes["swing_modes"] == list(SWING_TO_HA.values())
    assert state.attributes["preset_mode"] == "none"
    assert state.attributes["preset_modes"] == list(PRESET_TO_HA.values())
    assert "hvac_action" not in state.attributes


@pytest.mark.parametrize(
    ("service", "data", "coordinator_method", "expected"),
    [
        (
            "set_hvac_mode",
            {ATTR_HVAC_MODE: "heat"},
            "async_set_hvac_mode",
            call(HvacMode.HEAT),
        ),
        (
            "set_hvac_mode",
            {ATTR_HVAC_MODE: "cool"},
            "async_set_hvac_mode",
            call(HvacMode.COOL),
        ),
        ("turn_on", {}, "async_turn_on", call()),
        ("turn_off", {}, "async_turn_off", call()),
        (
            "set_temperature",
            {ATTR_TEMPERATURE: 27},
            "async_command",
            call(CommandKind.TEMPERATURE, 27.0),
        ),
        (
            "set_fan_mode",
            {ATTR_FAN_MODE: "high"},
            "async_command",
            call(CommandKind.FAN, FanMode.HIGH),
        ),
        (
            "set_swing_mode",
            {ATTR_SWING_MODE: "both"},
            "async_command",
            call(CommandKind.SWING, SwingMode.BOTH),
        ),
        (
            "set_preset_mode",
            {ATTR_PRESET_MODE: "windfree"},
            "async_command",
            call(CommandKind.PRESET, PresetMode.WINDFREE),
        ),
    ],
)
async def test_real_climate_services_delegate_once(
    live_climate_entity,
    hass,
    service,
    data,
    coordinator_method,
    expected,
) -> None:
    entity_id, coordinator = live_climate_entity
    target = getattr(coordinator, coordinator_method)

    await hass.services.async_call(
        "climate",
        service,
        {ATTR_ENTITY_ID: entity_id, **data},
        blocking=True,
    )

    assert target.await_count == 1
    assert target.await_args == expected
    other_methods = {
        coordinator.async_command,
        coordinator.async_set_hvac_mode,
        coordinator.async_turn_on,
        coordinator.async_turn_off,
    } - {target}
    for other in other_methods:
        other.assert_not_awaited()


async def test_real_service_schema_rejects_invalid_mode_before_entity(
    live_climate_entity, hass
) -> None:
    entity_id, coordinator = live_climate_entity

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: "warmest"},
            blocking=True,
        )

    coordinator.async_set_hvac_mode.assert_not_awaited()


async def test_real_unavailable_entity_is_not_called(live_climate_entity, hass) -> None:
    entity_id, coordinator = live_climate_entity
    coordinator.data = replace(coordinator.data, available=False)
    listener = coordinator.async_add_listener.call_args.args[0]
    listener()
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "unavailable"

    await hass.services.async_call(
        "climate",
        "set_fan_mode",
        {ATTR_ENTITY_ID: entity_id, ATTR_FAN_MODE: "low"},
        blocking=True,
    )

    coordinator.async_command.assert_not_awaited()
