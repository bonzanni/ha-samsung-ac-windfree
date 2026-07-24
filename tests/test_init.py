from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_ac_windfree.const import (
    DOMAIN,
    PROBE_PORTS,
    SUPPORTED_DEVICE_TYPE,
    SUPPORTED_FIRMWARE_PREFIX,
    SUPPORTED_MODEL,
    SUPPORTED_PLATFORM,
)
from custom_components.samsung_ac_windfree.models import Credentials

HOST = "ac.example.test"
DEVICE_ID = "11111111-2222-3333-4444-555555555555"


def _entry_data(credentials: Credentials, *, not_after: str | None = None):
    return {
        "host": HOST,
        "port": 49154,
        "device_id": DEVICE_ID,
        "model": SUPPORTED_MODEL,
        "firmware": f"{SUPPORTED_FIRMWARE_PREFIX}.1",
        "platform": SUPPORTED_PLATFORM,
        "client_key_pem": credentials.client_key_pem,
        "client_chain_pem": credentials.client_chain_pem,
        "not_before": credentials.not_before,
        "not_after": not_after or credentials.not_after,
    }


def _entry(credentials: Credentials, *, not_after: str | None = None):
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data=_entry_data(credentials, not_after=not_after),
        version=1,
    )


def _integration_traceback_locals(error: BaseException) -> str:
    values: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_globals.get("__name__") == "custom_components.samsung_ac_windfree":
            values.extend(repr(value) for value in frame.f_locals.values())
        traceback = traceback.tb_next
    return "\n".join(values)


def test_fixed_product_contract() -> None:
    assert DOMAIN == "samsung_ac_windfree"
    assert SUPPORTED_MODEL == "AR60F12C1AWNEU"
    assert SUPPORTED_DEVICE_TYPE == "oic.d.airconditioner"
    assert SUPPORTED_FIRMWARE_PREFIX == "TP1X_DA-AC-RAC-01001"
    assert SUPPORTED_PLATFORM == "TizenRT 4.0"
    assert PROBE_PORTS == tuple(range(49152, 49161))


def test_manifest_pins_isolated_requirements() -> None:
    manifest = json.loads(
        Path("custom_components/samsung_ac_windfree/manifest.json").read_text()
    )
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "local_push"
    assert manifest["integration_type"] == "device"
    assert manifest["requirements"] == [
        "smartthings-local==0.1.0",
        "cbor2==6.1.3",
    ]


async def test_setup_is_offline_and_forwards_only_after_coordinator_start(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    entry = _entry(credentials)
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.async_add_listener.return_value = MagicMock()
    coordinator.authentication_rejected = False
    calls: list[str] = []
    coordinator.async_start.side_effect = lambda: calls.append("start")

    async def forward(_entry, _platforms):
        calls.append("forward")

    with (
        patch(
            "custom_components.samsung_ac_windfree.async_bootstrap_credentials",
            new=AsyncMock(side_effect=AssertionError("startup must be offline")),
            create=True,
        ),
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator",
            return_value=coordinator,
        ) as constructor,
        patch.object(
            hass.config_entries, "async_forward_entry_setups", side_effect=forward
        ),
    ):
        assert await async_setup_entry(hass, entry)

    assert calls == ["start", "forward"]
    assert entry.runtime_data is coordinator
    supplied = constructor.call_args.kwargs
    assert supplied["host"] == HOST
    assert supplied["port"] == 49154
    assert supplied["credentials"] == credentials
    assert credentials.client_key_pem not in repr(constructor.call_args)


async def test_setup_failure_shuts_down_coordinator_before_reraising(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    entry = _entry(credentials)
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock(side_effect=RuntimeError("synthetic"))
    coordinator.async_shutdown = AsyncMock()

    with (
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator",
            return_value=coordinator,
        ),
        pytest.raises(Exception, match="setup_failed") as caught,
    ):
        await async_setup_entry(hass, entry)

    coordinator.async_shutdown.assert_awaited_once_with()
    traceback_locals = _integration_traceback_locals(caught.value)
    assert HOST not in traceback_locals
    assert credentials.client_key_pem not in traceback_locals
    assert credentials.client_chain_pem not in traceback_locals


async def test_setup_cancellation_shuts_down_and_scrubs_traceback(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    entry = _entry(credentials)
    coordinator = MagicMock()
    started = asyncio.Event()

    async def start():
        started.set()
        await asyncio.Event().wait()

    coordinator.async_start.side_effect = start
    coordinator.async_shutdown = AsyncMock()
    coordinator.async_add_listener.return_value = MagicMock()
    coordinator.authentication_rejected = False

    with patch(
        "custom_components.samsung_ac_windfree.WindFreeCoordinator",
        return_value=coordinator,
    ):
        task = hass.async_create_task(async_setup_entry(hass, entry))
        await started.wait()
        task.cancel("setup_cancelled")
        with pytest.raises(asyncio.CancelledError, match="setup_cancelled") as caught:
            await task

    coordinator.async_shutdown.assert_awaited_once_with()
    traceback_locals = _integration_traceback_locals(caught.value)
    assert HOST not in traceback_locals
    assert credentials.client_key_pem not in traceback_locals
    assert credentials.client_chain_pem not in traceback_locals


async def test_expiring_certificate_creates_sanitized_repair_before_start(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    now = datetime(2026, 7, 24, tzinfo=UTC)
    entry = _entry(credentials, not_after=(now + timedelta(days=89)).isoformat())
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.async_add_listener.return_value = MagicMock()
    coordinator.authentication_rejected = False

    with (
        patch(
            "custom_components.samsung_ac_windfree.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator",
            return_value=coordinator,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ),
    ):
        assert await async_setup_entry(hass, entry)

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "certificate_expiring")
    assert issue is not None
    assert issue.translation_placeholders is None
    assert HOST not in repr(issue)
    assert DEVICE_ID not in repr(issue)


async def test_expired_certificate_starts_reauth_without_constructing_coordinator(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    now = datetime(2026, 7, 24, tzinfo=UTC)
    entry = _entry(credentials, not_after=(now - timedelta(seconds=1)).isoformat())
    entry.async_start_reauth = MagicMock()

    with (
        patch(
            "custom_components.samsung_ac_windfree.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator"
        ) as constructor,
        pytest.raises(ConfigEntryAuthFailed, match="credentials_expired"),
    ):
        await async_setup_entry(hass, entry)

    entry.async_start_reauth.assert_called_once_with(hass)
    constructor.assert_not_called()


async def test_repeated_fatal_auth_starts_only_one_reauth_flow(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    entry = _entry(credentials)
    entry.async_start_reauth = MagicMock()
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.authentication_rejected = False
    listener = None

    def add_listener(candidate):
        nonlocal listener
        listener = candidate
        return MagicMock()

    coordinator.async_add_listener.side_effect = add_listener

    with (
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator",
            return_value=coordinator,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ),
    ):
        assert await async_setup_entry(hass, entry)

    assert listener is not None
    coordinator.authentication_rejected = True
    listener()
    listener()
    entry.async_start_reauth.assert_called_once_with(hass)


async def test_fatal_auth_during_platform_forwarding_is_not_missed(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    entry = _entry(credentials)
    entry.async_start_reauth = MagicMock()
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.authentication_rejected = False
    coordinator.async_add_listener.return_value = MagicMock()

    async def forward(_entry, _platforms):
        coordinator.authentication_rejected = True

    with (
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator",
            return_value=coordinator,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            side_effect=forward,
        ),
    ):
        assert await async_setup_entry(hass, entry)

    entry.async_start_reauth.assert_called_once_with(hass)


async def test_unload_orders_platforms_before_cancellation_safe_shutdown(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_unload_entry

    entry = _entry(credentials)
    coordinator = MagicMock()
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    calls: list[str] = []

    async def unload(_entry, _platforms):
        calls.append("unload_platforms")
        return True

    async def shutdown():
        calls.append("shutdown")
        entered.set()
        try:
            await release.wait()
        finally:
            finished.set()

    coordinator.async_shutdown.side_effect = shutdown
    entry.runtime_data = coordinator

    with patch.object(
        hass.config_entries, "async_unload_platforms", side_effect=unload
    ):
        task = hass.async_create_task(async_unload_entry(hass, entry))
        await entered.wait()
        task.cancel("caller_cancelled")
        await asyncio.sleep(0)
        assert not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="caller_cancelled"):
            await task

    assert finished.is_set()
    assert calls == ["unload_platforms", "shutdown"]


async def test_failed_platform_unload_keeps_coordinator_running(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_unload_entry

    entry = _entry(credentials)
    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock()
    entry.runtime_data = coordinator

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=False),
    ):
        assert not await async_unload_entry(hass, entry)

    coordinator.async_shutdown.assert_not_awaited()


async def test_update_listener_reloads_entry(hass, credentials) -> None:
    from custom_components.samsung_ac_windfree import _async_reload_entry

    entry = _entry(credentials)
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    ) as reload_entry:
        await _async_reload_entry(hass, entry)
    reload_entry.assert_awaited_once_with(entry.entry_id)


@pytest.mark.parametrize("version", [1])
async def test_version_one_migration_is_idempotent(hass, credentials, version) -> None:
    from custom_components.samsung_ac_windfree import async_migrate_entry

    entry = _entry(credentials)
    original = dict(entry.data)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == 1
    assert dict(entry.data) == original


async def test_invalid_stored_validity_is_sanitized_and_offline(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    entry = _entry(credentials)
    entry.add_to_hass(hass)
    data = dict(entry.data)
    data["not_after"] = "private-host.invalid"
    hass.config_entries.async_update_entry(entry, data=data)

    with (
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator"
        ) as constructor,
        pytest.raises(Exception, match="invalid_stored_credentials") as caught,
    ):
        await async_setup_entry(hass, entry)

    constructor.assert_not_called()
    assert HOST not in repr(caught.value)
    assert credentials.client_key_pem not in repr(caught.value)
    traceback_locals = _integration_traceback_locals(caught.value)
    assert HOST not in traceback_locals
    assert credentials.client_key_pem not in traceback_locals
    assert credentials.client_chain_pem not in traceback_locals
