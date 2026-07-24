from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_ac_windfree.const import DOMAIN
from custom_components.samsung_ac_windfree.coordinator import WindFreeCoordinator
from custom_components.samsung_ac_windfree.device import CommandKind
from custom_components.samsung_ac_windfree.models import (
    CommandRejected,
    DeviceIdentity,
    WindFreeData,
)
from custom_components.samsung_ac_windfree.switch import (
    SWITCH_DESCRIPTIONS,
    WindFreeSwitch,
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
            firmware="TP1X_DA-AC-RAC-01001_001",
            platform="TizenRT 4.0",
        ),
        auto_clean=True,
        display_light=False,
    )
    coordinator.last_update_success = True
    coordinator.async_command = AsyncMock()
    return coordinator


@pytest.fixture
def switches(coordinator) -> dict[str, WindFreeSwitch]:
    return {
        description.key: WindFreeSwitch(coordinator, description)
        for description in SWITCH_DESCRIPTIONS
    }


@pytest.fixture
async def live_switches(
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
    assert await async_setup_component(hass, Platform.SWITCH, {})
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SWITCH])
    await hass.async_block_till_done()
    entities = {
        entity.unique_id.removeprefix(f"{DEVICE_ID}_"): entity.entity_id
        for entity in hass.data[Platform.SWITCH].entities
    }
    yield entities, coordinator
    for entity in tuple(hass.data[Platform.SWITCH].entities):
        await entity.async_remove()


def _traceback_locals(error: BaseException) -> str:
    values: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__") == (
            "custom_components.samsung_ac_windfree.switch"
        ):
            values.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    return "\n".join(values)


async def test_platform_adds_exact_enabled_switch_inventory(coordinator) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_ID)
    entry.runtime_data = coordinator
    added: list[WindFreeSwitch] = []

    await async_setup_entry(None, entry, added.extend)  # type: ignore[arg-type]

    assert [entity.unique_id for entity in added] == [
        f"{DEVICE_ID}_auto_clean",
        f"{DEVICE_ID}_display_light",
    ]
    assert all(entity.entity_registry_enabled_default for entity in added)


def test_switches_project_snapshot_without_io(switches, coordinator) -> None:
    coordinator.async_command = AsyncMock(
        side_effect=AssertionError("property performed I/O")
    )

    assert switches["auto_clean"].is_on is True
    assert switches["display_light"].is_on is False
    assert switches["auto_clean"].available
    assert switches["display_light"].available
    coordinator.async_command.assert_not_awaited()


@pytest.mark.parametrize("key", ["auto_clean", "display_light"])
def test_missing_switch_state_is_unavailable(key, switches, coordinator) -> None:
    coordinator.data = replace(coordinator.data, **{key: None})

    assert switches[key].is_on is None
    assert not switches[key].available


@pytest.mark.parametrize(
    ("key", "method", "expected"),
    [
        ("auto_clean", "async_turn_on", call(CommandKind.AUTO_CLEAN, True)),
        ("auto_clean", "async_turn_off", call(CommandKind.AUTO_CLEAN, False)),
        ("display_light", "async_turn_on", call(CommandKind.DISPLAY_LIGHT, True)),
        ("display_light", "async_turn_off", call(CommandKind.DISPLAY_LIGHT, False)),
    ],
)
async def test_switch_commands_delegate_exactly_once(
    key, method, expected, switches, coordinator
) -> None:
    await getattr(switches[key], method)()

    assert coordinator.async_command.await_count == 1
    assert coordinator.async_command.await_args == expected


async def test_rejected_switch_command_is_translated_and_sanitized(
    switches, coordinator
) -> None:
    secret = "192.0.2.60 PRIVATE-REJECTED"
    coordinator.async_command = AsyncMock(side_effect=CommandRejected(secret))

    with pytest.raises(HomeAssistantError) as caught:
        await switches["auto_clean"].async_turn_on()

    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "command_failed"
    assert secret not in str(caught.value)
    assert secret not in _traceback_locals(caught.value)
    assert "AsyncMock" not in _traceback_locals(caught.value)


async def test_switch_cancellation_preserves_args_and_scrubs_traceback(
    switches, coordinator
) -> None:
    secret = "192.0.2.60 PRIVATE-CANCEL"
    coordinator.async_command = AsyncMock(
        side_effect=asyncio.CancelledError(secret, 17)
    )

    with pytest.raises(asyncio.CancelledError) as caught:
        await switches["display_light"].async_turn_off()

    assert caught.value.args == (secret, 17)
    assert secret not in _traceback_locals(caught.value)
    assert "AsyncMock" not in _traceback_locals(caught.value)


async def test_real_switch_services_delegate_once(live_switches, hass) -> None:
    entities, coordinator = live_switches

    await hass.services.async_call(
        Platform.SWITCH,
        "turn_off",
        {ATTR_ENTITY_ID: entities["auto_clean"]},
        blocking=True,
    )
    await hass.services.async_call(
        Platform.SWITCH,
        "turn_on",
        {ATTR_ENTITY_ID: entities["display_light"]},
        blocking=True,
    )

    assert coordinator.async_command.await_args_list == [
        call(CommandKind.AUTO_CLEAN, False),
        call(CommandKind.DISPLAY_LIGHT, True),
    ]


async def test_real_switch_state_tracks_coordinator_and_availability(
    live_switches, hass
) -> None:
    entities, coordinator = live_switches
    assert hass.states.get(entities["auto_clean"]).state == "on"  # type: ignore[union-attr]
    assert hass.states.get(entities["display_light"]).state == "off"  # type: ignore[union-attr]

    coordinator.data = replace(
        coordinator.data,
        auto_clean=False,
        display_light=None,
    )
    for registered in coordinator.async_add_listener.call_args_list:
        registered.args[0]()
    await hass.async_block_till_done()

    assert hass.states.get(entities["auto_clean"]).state == "off"  # type: ignore[union-attr]
    assert hass.states.get(entities["display_light"]).state == "unavailable"  # type: ignore[union-attr]
