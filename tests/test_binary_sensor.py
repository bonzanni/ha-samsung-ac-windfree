from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_ac_windfree.binary_sensor import (
    BINARY_SENSOR_DESCRIPTIONS,
    WindFreeBinarySensor,
    async_setup_entry,
)
from custom_components.samsung_ac_windfree.const import DOMAIN
from custom_components.samsung_ac_windfree.coordinator import WindFreeCoordinator
from custom_components.samsung_ac_windfree.models import (
    AlarmState,
    DeviceIdentity,
    FilterState,
    WindFreeData,
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
        filter=FilterState(attention=False),
        alarms=AlarmState(problem=False, filter_alarm=False),
        current_limit_enabled=True,
    )
    coordinator.last_update_success = True
    coordinator.async_command = AsyncMock()
    return coordinator


@pytest.fixture
def binary_sensors(coordinator) -> dict[str, WindFreeBinarySensor]:
    return {
        description.key: WindFreeBinarySensor(coordinator, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    }


@pytest.fixture
async def live_binary_sensors(
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
    assert await async_setup_component(hass, Platform.BINARY_SENSOR, {})
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform.BINARY_SENSOR]
    )
    await hass.async_block_till_done()
    entities = {
        entity.unique_id.removeprefix(f"{DEVICE_ID}_"): entity.entity_id
        for entity in hass.data[Platform.BINARY_SENSOR].entities
    }
    yield entities, coordinator
    for entity in tuple(hass.data[Platform.BINARY_SENSOR].entities):
        await entity.async_remove()


async def test_platform_adds_exact_binary_sensor_inventory(coordinator) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_ID)
    entry.runtime_data = coordinator
    added: list[WindFreeBinarySensor] = []

    await async_setup_entry(None, entry, added.extend)  # type: ignore[arg-type]

    assert [entity.unique_id for entity in added] == [
        f"{DEVICE_ID}_filter_attention",
        f"{DEVICE_ID}_problem",
        f"{DEVICE_ID}_current_limit_enabled",
    ]
    assert added[0].device_class is BinarySensorDeviceClass.PROBLEM
    assert added[1].device_class is BinarySensorDeviceClass.PROBLEM
    assert added[2].entity_category is EntityCategory.DIAGNOSTIC
    assert not added[2].entity_registry_enabled_default
    assert all(
        entity.entity_registry_enabled_default
        for entity in added
        if entity.entity_description.key != "current_limit_enabled"
    )


@pytest.mark.parametrize(
    ("attention", "filter_alarm", "expected"),
    [
        (False, False, False),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
)
def test_filter_attention_combines_filter_state_and_filter_alarm(
    attention, filter_alarm, expected, binary_sensors, coordinator
) -> None:
    coordinator.data = replace(
        coordinator.data,
        filter=FilterState(attention=attention),
        alarms=AlarmState(filter_alarm=filter_alarm),
    )

    assert binary_sensors["filter_attention"].is_on is expected


def test_problem_is_only_non_filter_alarm(binary_sensors, coordinator) -> None:
    coordinator.data = replace(
        coordinator.data,
        alarms=AlarmState(problem=False, filter_alarm=True),
    )
    assert binary_sensors["problem"].is_on is False
    coordinator.data = replace(
        coordinator.data,
        alarms=AlarmState(problem=True, filter_alarm=False),
    )
    assert binary_sensors["problem"].is_on is True


@pytest.mark.parametrize(
    ("key", "filter_state", "alarms"),
    [
        (
            "filter_attention",
            FilterState(attention=1),  # type: ignore[arg-type]
            AlarmState(),
        ),
        (
            "filter_attention",
            FilterState(),
            AlarmState(filter_alarm="raw"),  # type: ignore[arg-type]
        ),
        (
            "problem",
            FilterState(),
            AlarmState(problem="raw"),  # type: ignore[arg-type]
        ),
    ],
)
def test_alarm_binary_sensors_reject_malformed_booleans(
    key, filter_state, alarms, binary_sensors, coordinator
) -> None:
    coordinator.data = replace(
        coordinator.data,
        filter=filter_state,
        alarms=alarms,
    )

    assert binary_sensors[key].is_on is None
    assert not binary_sensors[key].available


def test_binary_properties_perform_no_io(binary_sensors, coordinator) -> None:
    coordinator.async_command = AsyncMock(
        side_effect=AssertionError("property performed I/O")
    )

    assert binary_sensors["filter_attention"].is_on is False
    assert binary_sensors["problem"].is_on is False
    assert binary_sensors["current_limit_enabled"].is_on is True
    assert all(entity.available for entity in binary_sensors.values())
    coordinator.async_command.assert_not_awaited()


@pytest.mark.parametrize("value", [None, 0, 1, "on", object()])
def test_current_limit_binary_rejects_non_boolean(
    value, binary_sensors, coordinator
) -> None:
    coordinator.data = replace(coordinator.data, current_limit_enabled=value)

    assert binary_sensors["current_limit_enabled"].is_on is None
    assert not binary_sensors["current_limit_enabled"].available


def test_global_availability_applies_to_all_binary_sensors(
    binary_sensors, coordinator
) -> None:
    coordinator.data = replace(coordinator.data, available=False)

    assert not any(entity.available for entity in binary_sensors.values())


async def test_real_binary_states_registry_defaults_and_updates(
    live_binary_sensors, hass
) -> None:
    entities, coordinator = live_binary_sensors
    assert hass.states.get(entities["filter_attention"]).state == "off"  # type: ignore[union-attr]
    assert hass.states.get(entities["problem"]).state == "off"  # type: ignore[union-attr]

    registry = er.async_get(hass)
    current_limit_entry = registry.async_get_entity_id(
        Platform.BINARY_SENSOR,
        DOMAIN,
        f"{DEVICE_ID}_current_limit_enabled",
    )
    assert current_limit_entry is not None
    assert hass.states.get(current_limit_entry) is None
    assert registry.async_get(current_limit_entry).disabled_by is not None  # type: ignore[union-attr]

    coordinator.data = replace(
        coordinator.data,
        filter=FilterState(attention=False),
        alarms=AlarmState(problem=True, filter_alarm=True),
    )
    for registered in coordinator.async_add_listener.call_args_list:
        registered.args[0]()
    await hass.async_block_till_done()

    assert hass.states.get(entities["filter_attention"]).state == "on"  # type: ignore[union-attr]
    assert hass.states.get(entities["problem"]).state == "on"  # type: ignore[union-attr]
