from __future__ import annotations

import math
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    Platform,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_ac_windfree.const import DOMAIN
from custom_components.samsung_ac_windfree.coordinator import WindFreeCoordinator
from custom_components.samsung_ac_windfree.models import (
    AlarmState,
    DeviceIdentity,
    EnergyState,
    FilterState,
    WindFreeData,
)
from custom_components.samsung_ac_windfree.sensor import (
    SENSOR_DESCRIPTIONS,
    WindFreeSensor,
    async_setup_entry,
)

DEVICE_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def coordinator() -> MagicMock:
    coordinator = MagicMock(spec=WindFreeCoordinator)
    coordinator.data = replace(
        WindFreeData.empty(),
        available=True,
        identity=DeviceIdentity(
            device_id=DEVICE_ID,
            model="AR60F12C1AWNEU",
            device_type="oic.d.airconditioner",
            firmware="TP1X_DA-AC-RAC-01001_0000",
            platform="TizenRT 4.0",
        ),
        filter=FilterState(used=321, capacity=1000, status="normal"),
        energy=EnergyState(cumulative_kwh=12.345),
        alarms=AlarmState(active_code="ErrorCode_E101", problem=True),
        current_limit_level=3,
    )
    coordinator.last_update_success = True
    coordinator.async_command = AsyncMock()
    return coordinator


@pytest.fixture
def sensors(coordinator) -> dict[str, WindFreeSensor]:
    return {
        description.key: WindFreeSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    }


@pytest.fixture
async def live_sensors(
    hass: HomeAssistant,
    coordinator: MagicMock,
) -> tuple[dict[str, str], MagicMock]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Samsung WindFree AC",
        unique_id=DEVICE_ID,
        state=ConfigEntryState.LOADED,
    )
    entry.runtime_data = coordinator
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, Platform.SENSOR, {})
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
    await hass.async_block_till_done()
    entities = {
        entity.unique_id.removeprefix(f"{DEVICE_ID}_"): entity.entity_id
        for entity in hass.data[Platform.SENSOR].entities
    }
    yield entities, coordinator
    for entity in tuple(hass.data[Platform.SENSOR].entities):
        await entity.async_remove()


@pytest.fixture
async def live_all_entities(
    hass: HomeAssistant,
    coordinator: MagicMock,
) -> MagicMock:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Samsung WindFree AC",
        unique_id=DEVICE_ID,
        state=ConfigEntryState.LOADED,
    )
    entry.runtime_data = coordinator
    entry.add_to_hass(hass)
    platforms = (
        Platform.BINARY_SENSOR,
        Platform.CLIMATE,
        Platform.SENSOR,
        Platform.SWITCH,
    )
    for platform in platforms:
        assert await async_setup_component(hass, platform, {})
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    await hass.async_block_till_done()
    yield coordinator
    for platform in platforms:
        for entity in tuple(hass.data[platform].entities):
            await entity.async_remove()


async def test_platform_adds_exact_sensor_inventory(coordinator) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_ID)
    entry.runtime_data = coordinator
    added: list[WindFreeSensor] = []

    await async_setup_entry(None, entry, added.extend)  # type: ignore[arg-type]

    assert [entity.unique_id for entity in added] == [
        f"{DEVICE_ID}_filter_usage",
        f"{DEVICE_ID}_filter_status",
        f"{DEVICE_ID}_energy_consumption",
        f"{DEVICE_ID}_active_alarm",
        f"{DEVICE_ID}_current_limit_level",
    ]
    enabled = {
        entity.entity_description.key
        for entity in added
        if entity.entity_registry_enabled_default
    }
    assert enabled == {
        "filter_usage",
        "filter_status",
        "energy_consumption",
        "active_alarm",
    }
    assert added[-1].entity_category is EntityCategory.DIAGNOSTIC


def test_sensor_metadata_is_exact(sensors) -> None:
    usage = sensors["filter_usage"]
    assert usage.native_unit_of_measurement == PERCENTAGE
    assert usage.suggested_display_precision == 1

    status = sensors["filter_status"]
    assert status.device_class is SensorDeviceClass.ENUM
    assert status.options == ["normal", "wash", "replace"]

    energy = sensors["energy_consumption"]
    assert energy.device_class is SensorDeviceClass.ENERGY
    assert energy.state_class is SensorStateClass.TOTAL_INCREASING
    assert energy.native_unit_of_measurement is UnitOfEnergy.KILO_WATT_HOUR
    assert energy.suggested_display_precision == 3

    assert sensors["active_alarm"].entity_category is EntityCategory.DIAGNOSTIC
    assert sensors["current_limit_level"].entity_category is EntityCategory.DIAGNOSTIC
    assert not sensors["current_limit_level"].entity_registry_enabled_default


def test_filter_status_options_are_isolated_between_callers(coordinator) -> None:
    description = next(
        item for item in SENSOR_DESCRIPTIONS if item.key == "filter_status"
    )
    first = WindFreeSensor(coordinator, description)
    second = WindFreeSensor(coordinator, description)

    first.options.append("raw_payload")  # type: ignore[union-attr]

    assert second.options == ["normal", "wash", "replace"]
    assert first.options is not second.options


def test_sensor_device_info_is_exact_and_private(sensors, coordinator) -> None:
    identity = coordinator.data.identity
    assert identity is not None
    expected = {
        "identifiers": {(DOMAIN, identity.device_id)},
        "manufacturer": "Samsung",
        "model": identity.model,
        "sw_version": identity.firmware,
        "hw_version": identity.platform,
    }
    assert all(sensor.device_info == expected for sensor in sensors.values())
    assert all("serial_number" not in sensor.device_info for sensor in sensors.values())


def test_sensors_project_snapshot_without_io(sensors, coordinator) -> None:
    coordinator.async_command = AsyncMock(
        side_effect=AssertionError("property performed I/O")
    )

    assert sensors["filter_usage"].native_value == 32.1
    assert sensors["filter_status"].native_value == "normal"
    assert sensors["energy_consumption"].native_value == 12.345
    assert sensors["active_alarm"].native_value == "ErrorCode_E101"
    assert sensors["current_limit_level"].native_value == 3
    assert all(sensor.available for sensor in sensors.values())
    coordinator.async_command.assert_not_awaited()


@pytest.mark.parametrize(
    ("used", "capacity", "expected"),
    [
        (0, 1000, 0.0),
        (1000, 1000, 100.0),
        (2000, 1000, 100.0),
        (10**1000, 10**1001, 10.0),
    ],
)
def test_filter_usage_clamps_only_valid_ratio(
    used, capacity, expected, sensors, coordinator
) -> None:
    coordinator.data = replace(
        coordinator.data,
        filter=FilterState(used=used, capacity=capacity, status="normal"),
    )

    assert sensors["filter_usage"].native_value == expected
    assert sensors["filter_usage"].available


@pytest.mark.parametrize(
    ("used", "capacity"),
    [
        (1, 0),
        (-1, 100),
        (True, 100),
        (1, False),
        (math.nan, 100),
        (1, math.inf),
        ("1", 100),
        (1, "100"),
        (None, 100),
    ],
)
def test_filter_usage_rejects_malformed_values(
    used, capacity, sensors, coordinator
) -> None:
    coordinator.data = replace(
        coordinator.data,
        filter=FilterState(  # type: ignore[arg-type]
            used=used,
            capacity=capacity,
            status="normal",
        ),
    )

    assert sensors["filter_usage"].native_value is None
    assert not sensors["filter_usage"].available


@pytest.mark.parametrize("status", [None, "", "secret payload", "Normal", 3])
def test_filter_status_never_exposes_unknown_raw_value(
    status, sensors, coordinator
) -> None:
    coordinator.data = replace(
        coordinator.data,
        filter=replace(coordinator.data.filter, status=status),
    )

    assert sensors["filter_status"].native_value is None
    assert not sensors["filter_status"].available


@pytest.mark.parametrize(
    "value",
    [
        None,
        -1,
        math.nan,
        math.inf,
        -math.inf,
        True,
        "12.345",
        10**100,
        10**1000,
    ],
)
def test_energy_rejects_invalid_negative_nonfinite_or_overflow(
    value, sensors, coordinator
) -> None:
    coordinator.data = replace(
        coordinator.data,
        energy=EnergyState(cumulative_kwh=value),  # type: ignore[arg-type]
    )

    assert sensors["energy_consumption"].native_value is None
    assert not sensors["energy_consumption"].available


def test_energy_decrease_is_published_unchanged(sensors, coordinator) -> None:
    assert sensors["energy_consumption"].native_value == 12.345
    coordinator.data = replace(
        coordinator.data,
        energy=EnergyState(cumulative_kwh=0.125),
    )

    assert sensors["energy_consumption"].native_value == 0.125


@pytest.mark.parametrize(
    "value",
    [None, "", "contains space", "x" * 65, "192.0.2.60", 17],
)
def test_alarm_sensor_rejects_malformed_or_privacy_risky_codes(
    value, sensors, coordinator
) -> None:
    coordinator.data = replace(
        coordinator.data,
        alarms=AlarmState(active_code=value),  # type: ignore[arg-type]
    )

    assert sensors["active_alarm"].native_value is None
    assert not sensors["active_alarm"].available


@pytest.mark.parametrize("value", [None, -1, True, 2**31, "3", math.inf])
def test_current_limit_level_rejects_malformed_or_overflow(
    value, sensors, coordinator
) -> None:
    coordinator.data = replace(coordinator.data, current_limit_level=value)

    assert sensors["current_limit_level"].native_value is None
    assert not sensors["current_limit_level"].available


async def test_real_energy_semantics_and_snapshot_updates(live_sensors, hass) -> None:
    entities, coordinator = live_sensors
    state = hass.states.get(entities["energy_consumption"])
    assert state is not None
    assert state.state == "12.345"
    assert state.attributes["device_class"] == "energy"
    assert state.attributes["state_class"] == "total_increasing"
    assert state.attributes["unit_of_measurement"] == "kWh"

    coordinator.data = replace(
        coordinator.data,
        energy=EnergyState(cumulative_kwh=0.125),
    )
    for registered in coordinator.async_add_listener.call_args_list:
        registered.args[0]()
    await hass.async_block_till_done()

    state = hass.states.get(entities["energy_consumption"])
    assert state is not None
    assert state.state == "0.125"


async def test_real_sensor_registry_defaults_and_device_info(
    live_sensors, hass
) -> None:
    entities, coordinator = live_sensors
    registry = er.async_get(hass)
    enabled = {
        entry.unique_id
        for entry in registry.entities.values()
        if entry.platform == DOMAIN and entry.disabled_by is None
    }
    disabled = {
        entry.unique_id
        for entry in registry.entities.values()
        if entry.platform == DOMAIN and entry.disabled_by is not None
    }
    assert enabled == {
        f"{DEVICE_ID}_filter_usage",
        f"{DEVICE_ID}_filter_status",
        f"{DEVICE_ID}_energy_consumption",
        f"{DEVICE_ID}_active_alarm",
    }
    assert disabled == {f"{DEVICE_ID}_current_limit_level"}
    state = hass.states.get(entities["filter_usage"])
    assert state is not None
    assert state.attributes["unit_of_measurement"] == "%"
    assert coordinator.async_command.await_count == 0


async def test_real_sensor_update_can_become_unavailable(live_sensors, hass) -> None:
    entities, coordinator = live_sensors
    coordinator.data = replace(
        coordinator.data,
        filter=FilterState(),
        energy=EnergyState(),
        alarms=AlarmState(),
    )
    for registered in coordinator.async_add_listener.call_args_list:
        registered.args[0]()
    await hass.async_block_till_done()

    for key in ("filter_usage", "filter_status", "energy_consumption", "active_alarm"):
        state = hass.states.get(entities[key])
        assert state is not None
        assert state.state == "unavailable"


async def test_real_registry_contains_exact_total_entity_inventory(
    live_all_entities, hass
) -> None:
    registry = er.async_get(hass)
    enabled = {
        entry.unique_id
        for entry in registry.entities.values()
        if entry.platform == DOMAIN and entry.disabled_by is None
    }
    disabled = {
        entry.unique_id
        for entry in registry.entities.values()
        if entry.platform == DOMAIN and entry.disabled_by is not None
    }
    assert enabled == {
        DEVICE_ID,
        f"{DEVICE_ID}_auto_clean",
        f"{DEVICE_ID}_display_light",
        f"{DEVICE_ID}_filter_usage",
        f"{DEVICE_ID}_filter_status",
        f"{DEVICE_ID}_filter_attention",
        f"{DEVICE_ID}_energy_consumption",
        f"{DEVICE_ID}_problem",
        f"{DEVICE_ID}_active_alarm",
    }
    assert disabled == {
        f"{DEVICE_ID}_current_limit_enabled",
        f"{DEVICE_ID}_current_limit_level",
    }
    assert live_all_entities.async_command.await_count == 0


async def test_real_sensor_states_expose_no_opaque_payload_or_timestamps(
    live_sensors, hass
) -> None:
    entities, _coordinator = live_sensors
    rendered = "\n".join(
        repr(hass.states.get(entity_id)) for entity_id in entities.values()
    )
    assert "timestamp" not in rendered.casefold()
    assert "opaque" not in rendered.casefold()
    assert "192.0.2.60" not in rendered
