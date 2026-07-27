from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from homeassistant.config_entries import (
    ConfigEntryAuthFailed,
    ConfigEntryDisabler,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_ac_windfree.const import (
    DOMAIN,
    PROBE_PORTS,
    SUPPORTED_DEVICE_TYPE,
    SUPPORTED_FIRMWARE,
    SUPPORTED_MODEL,
    SUPPORTED_PLATFORM,
    SUPPORTED_PLATFORM_FIRMWARE,
    SUPPORTED_PRODUCT_VERSION,
    SUPPORTED_UNIT_FINGERPRINT_SHA256,
)
from custom_components.samsung_ac_windfree.models import Credentials, UnsupportedDevice

HOST = "ac.example.test"
DEVICE_ID = "11111111-2222-3333-4444-555555555555"


def _entry_data(credentials: Credentials, *, not_after: str | None = None):
    return {
        "host": HOST,
        "port": 49154,
        "device_id": DEVICE_ID,
        "model": SUPPORTED_MODEL,
        "firmware": SUPPORTED_FIRMWARE,
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


def _assert_translated_entry_error(
    error: BaseException,
    translation_key: str,
) -> None:
    assert error.args == (translation_key,)
    assert error.translation_domain == DOMAIN
    assert error.translation_key == translation_key
    assert error.translation_placeholders is None
    assert error.generate_message is True


def test_fixed_product_contract() -> None:
    assert DOMAIN == "samsung_ac_windfree"
    assert SUPPORTED_MODEL == "AR60F12C1AWNEU"
    assert SUPPORTED_DEVICE_TYPE == "oic.d.airconditioner"
    assert SUPPORTED_FIRMWARE == "TP1X_DA-AC-RAC-01001_0000"
    assert SUPPORTED_PLATFORM == "TizenRT 4.0"
    assert SUPPORTED_PRODUCT_VERSION == "SYSTEM 2.0"
    assert SUPPORTED_PLATFORM_FIRMWARE == "ARA-KR-TP1-25-ARXX00_11260401"
    assert re.fullmatch(r"[0-9a-f]{64}", SUPPORTED_UNIT_FINGERPRINT_SHA256)
    sanitized_identity = json.loads(
        (Path(__file__).parent / "fixtures" / "device_identity.json").read_text()
    )
    sanitized_model_number = sanitized_identity["device_0"]["/information/vs/0"][
        "x.com.samsung.da.modelNum"
    ]
    assert (
        SUPPORTED_UNIT_FINGERPRINT_SHA256
        != hashlib.sha256(sanitized_model_number.encode()).hexdigest()
    )
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


async def test_setup_shuts_down_coordinator_on_home_assistant_stop(
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

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    coordinator.async_shutdown.assert_awaited_once_with()


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
        pytest.raises(ConfigEntryNotReady) as caught,
    ):
        await async_setup_entry(hass, entry)

    coordinator.async_shutdown.assert_awaited_once_with()
    _assert_translated_entry_error(caught.value, "setup_failed")
    traceback_locals = _integration_traceback_locals(caught.value)
    assert HOST not in traceback_locals
    assert credentials.client_key_pem not in traceback_locals
    assert credentials.client_chain_pem not in traceback_locals


@pytest.mark.parametrize(
    ("startup_error", "expected_type", "expected_message"),
    [
        (
            RuntimeError("private setup failure"),
            ConfigEntryNotReady,
            "setup_failed",
        ),
        (
            UnsupportedDevice("unsupported_device"),
            ConfigEntryError,
            "unsupported_device",
        ),
    ],
)
async def test_setup_cleanup_failure_does_not_replace_sanitized_error(
    hass,
    credentials,
    startup_error,
    expected_type,
    expected_message,
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    entry = _entry(credentials)
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock(side_effect=startup_error)
    coordinator.async_shutdown = AsyncMock(
        side_effect=RuntimeError(f"{HOST} {credentials.client_key_pem}")
    )

    with (
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator",
            return_value=coordinator,
        ),
        pytest.raises(expected_type) as caught,
    ):
        await async_setup_entry(hass, entry)

    _assert_translated_entry_error(caught.value, expected_message)
    assert caught.value.__context__ is None
    traceback_locals = _integration_traceback_locals(caught.value)
    assert HOST not in traceback_locals
    assert credentials.client_key_pem not in traceback_locals
    assert credentials.client_chain_pem not in traceback_locals


async def test_delayed_setup_cleanup_failure_is_sanitized(hass, credentials) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    entry = _entry(credentials)
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock(side_effect=RuntimeError("private startup"))

    async def delayed_shutdown_failure():
        await asyncio.sleep(0)
        raise RuntimeError(f"{HOST} {credentials.client_key_pem}")

    coordinator.async_shutdown.side_effect = delayed_shutdown_failure

    with (
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator",
            return_value=coordinator,
        ),
        pytest.raises(ConfigEntryNotReady) as caught,
    ):
        await async_setup_entry(hass, entry)

    _assert_translated_entry_error(caught.value, "setup_failed")
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
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


async def test_setup_repeated_cancellation_retains_shutdown_and_latest_args(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    entry = _entry(credentials)
    coordinator = MagicMock()
    started = asyncio.Event()
    shutdown_started = asyncio.Event()
    shutdown_release = asyncio.Event()
    shutdown_finished = asyncio.Event()

    async def start():
        started.set()
        await asyncio.Event().wait()

    async def shutdown():
        shutdown_started.set()
        try:
            await shutdown_release.wait()
        finally:
            shutdown_finished.set()

    coordinator.async_start.side_effect = start
    coordinator.async_shutdown.side_effect = shutdown

    with patch(
        "custom_components.samsung_ac_windfree.WindFreeCoordinator",
        return_value=coordinator,
    ):
        task = hass.async_create_task(async_setup_entry(hass, entry))
        await started.wait()
        task.cancel("first_setup_cancel")
        await shutdown_started.wait()
        task.cancel("second_setup_cancel")
        await asyncio.sleep(0)
        assert not shutdown_finished.is_set()
        shutdown_release.set()
        with pytest.raises(
            asyncio.CancelledError, match="second_setup_cancel"
        ) as caught:
            await task

    assert shutdown_finished.is_set()
    assert caught.value.__context__ is None
    traceback_locals = _integration_traceback_locals(caught.value)
    assert HOST not in traceback_locals
    assert credentials.client_key_pem not in traceback_locals


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
    assert issue.is_fixable
    assert issue.data is None
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
        pytest.raises(ConfigEntryAuthFailed) as caught,
    ):
        await async_setup_entry(hass, entry)

    _assert_translated_entry_error(caught.value, "credentials_expired")
    entry.async_start_reauth.assert_called_once_with(hass)
    constructor.assert_not_called()


async def test_fresh_certificate_deletes_stale_expiry_issue(
    hass,
    credentials,
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    now = datetime(2026, 7, 24, tzinfo=UTC)
    entry = _entry(
        credentials,
        not_after=(now + timedelta(days=91)).isoformat(),
    )
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.async_add_listener.return_value = MagicMock()
    coordinator.authentication_rejected = False
    coordinator.health.authentication_rejected = False
    coordinator.health.resource_contract_changed = False
    coordinator.health.unsupported_identity_after_update = False
    coordinator.health.port_range_exhausted = False
    ir.async_create_issue(
        hass,
        DOMAIN,
        "certificate_expiring",
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="certificate_expiring",
    )

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

    assert ir.async_get(hass).async_get_issue(DOMAIN, "certificate_expiring") is None


async def test_not_yet_valid_certificate_starts_reauth_offline(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    now = datetime(2026, 7, 24, tzinfo=UTC)
    entry = _entry(credentials)
    entry.add_to_hass(hass)
    data = dict(entry.data)
    data["not_before"] = (now + timedelta(seconds=1)).isoformat()
    hass.config_entries.async_update_entry(entry, data=data)
    entry.async_start_reauth = MagicMock()

    with (
        patch(
            "custom_components.samsung_ac_windfree.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator"
        ) as constructor,
        pytest.raises(ConfigEntryAuthFailed) as caught,
    ):
        await async_setup_entry(hass, entry)

    _assert_translated_entry_error(caught.value, "credentials_not_yet_valid")
    entry.async_start_reauth.assert_called_once_with(hass)
    constructor.assert_not_called()
    assert caught.value.__context__ is None
    traceback_locals = _integration_traceback_locals(caught.value)
    assert HOST not in traceback_locals
    assert credentials.client_key_pem not in traceback_locals


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


async def test_unload_shutdown_failure_is_sanitized(hass, credentials) -> None:
    from custom_components.samsung_ac_windfree import async_unload_entry

    entry = _entry(credentials)
    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock(
        side_effect=RuntimeError(f"{HOST} {credentials.client_key_pem}")
    )
    entry.runtime_data = coordinator

    with (
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new=AsyncMock(return_value=True),
        ),
        pytest.raises(ConfigEntryError) as caught,
    ):
        await async_unload_entry(hass, entry)

    _assert_translated_entry_error(caught.value, "unload_failed")
    assert caught.value.__context__ is None
    traceback_locals = _integration_traceback_locals(caught.value)
    assert HOST not in traceback_locals
    assert credentials.client_key_pem not in traceback_locals


async def test_delayed_unload_shutdown_failure_is_sanitized(hass, credentials) -> None:
    from custom_components.samsung_ac_windfree import async_unload_entry

    entry = _entry(credentials)
    coordinator = MagicMock()

    async def delayed_shutdown_failure():
        await asyncio.sleep(0)
        raise RuntimeError(f"{HOST} {credentials.client_key_pem}")

    coordinator.async_shutdown.side_effect = delayed_shutdown_failure
    entry.runtime_data = coordinator

    with (
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new=AsyncMock(return_value=True),
        ),
        pytest.raises(ConfigEntryError) as caught,
    ):
        await async_unload_entry(hass, entry)

    _assert_translated_entry_error(caught.value, "unload_failed")
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    traceback_locals = _integration_traceback_locals(caught.value)
    assert HOST not in traceback_locals
    assert credentials.client_key_pem not in traceback_locals
    assert credentials.client_chain_pem not in traceback_locals


async def test_unload_repeated_cancellation_retains_shutdown_and_latest_args(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_unload_entry

    entry = _entry(credentials)
    coordinator = MagicMock()
    shutdown_started = asyncio.Event()
    shutdown_release = asyncio.Event()
    shutdown_finished = asyncio.Event()

    async def shutdown():
        shutdown_started.set()
        try:
            await shutdown_release.wait()
        finally:
            shutdown_finished.set()

    coordinator.async_shutdown.side_effect = shutdown
    entry.runtime_data = coordinator

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=True),
    ):
        task = hass.async_create_task(async_unload_entry(hass, entry))
        await shutdown_started.wait()
        task.cancel("first_unload_cancel")
        await asyncio.sleep(0)
        task.cancel("second_unload_cancel")
        await asyncio.sleep(0)
        assert not shutdown_finished.is_set()
        shutdown_release.set()
        with pytest.raises(
            asyncio.CancelledError, match="second_unload_cancel"
        ) as caught:
            await task

    assert shutdown_finished.is_set()
    assert caught.value.__context__ is None
    traceback_locals = _integration_traceback_locals(caught.value)
    assert HOST not in traceback_locals
    assert credentials.client_key_pem not in traceback_locals


async def test_auth_listener_is_suppressed_during_unload_and_restored_once_on_failure(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import (
        async_setup_entry,
        async_unload_entry,
    )

    entry = _entry(credentials)
    entry.async_start_reauth = MagicMock()
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.authentication_rejected = False
    listeners = []
    unsubscribes = []

    def add_listener(listener):
        listeners.append(listener)
        unsubscribe = MagicMock()
        unsubscribes.append(unsubscribe)
        return unsubscribe

    coordinator.async_add_listener.side_effect = add_listener

    async def fail_unload(_entry, _platforms):
        coordinator.authentication_rejected = True
        listeners[-1]()
        return False

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

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        side_effect=fail_unload,
    ):
        assert not await async_unload_entry(hass, entry)

    unsubscribes[0].assert_called_once_with()
    assert len(listeners) == 2
    assert len(unsubscribes) == 2
    entry.async_start_reauth.assert_called_once_with(hass)
    listeners[-1]()
    entry.async_start_reauth.assert_called_once_with(hass)


async def test_auth_listener_stays_suppressed_during_successful_shutdown(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import (
        async_setup_entry,
        async_unload_entry,
    )

    entry = _entry(credentials)
    entry.async_start_reauth = MagicMock()
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.authentication_rejected = False
    listener = None
    unsubscribe = MagicMock()

    def add_listener(candidate):
        nonlocal listener
        listener = candidate
        return unsubscribe

    coordinator.async_add_listener.side_effect = add_listener

    async def shutdown():
        coordinator.authentication_rejected = True
        assert listener is not None
        listener()

    coordinator.async_shutdown.side_effect = shutdown

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

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=True),
    ):
        assert await async_unload_entry(hass, entry)

    unsubscribe.assert_called_once_with()
    entry.async_start_reauth.assert_not_called()


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


async def test_failed_unload_retains_active_private_repair_state(
    hass,
    credentials,
) -> None:
    from custom_components.samsung_ac_windfree import async_unload_entry
    from custom_components.samsung_ac_windfree.repairs import (
        _store,
        async_sync_runtime_issues,
    )

    entry = _entry(credentials)
    entry.add_to_hass(hass)
    async_sync_runtime_issues(
        hass,
        entry.entry_id,
        SimpleNamespace(
            authentication_rejected=True,
            resource_contract_changed=False,
            unsupported_identity_after_update=False,
            port_range_exhausted=False,
        ),
    )
    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock()
    entry.runtime_data = coordinator

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=False),
    ):
        assert not await async_unload_entry(hass, entry)

    store = _store(hass)
    assert store.entries[entry.entry_id].authentication_rejected is True
    assert entry.entry_id not in store.runtime_pending
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "authentication_rejected")
        is not None
    )
    coordinator.async_shutdown.assert_not_awaited()


async def test_successful_unload_removes_only_its_private_repair_state(
    hass,
    credentials,
) -> None:
    from custom_components.samsung_ac_windfree import async_unload_entry
    from custom_components.samsung_ac_windfree.repairs import (
        _store,
        async_sync_runtime_issues,
    )

    healthy_entry = _entry(credentials)
    unhealthy_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data=_entry_data(credentials),
        version=1,
        disabled_by=ConfigEntryDisabler.USER,
    )
    healthy_entry.add_to_hass(hass)
    unhealthy_entry.add_to_hass(hass)
    healthy = SimpleNamespace(
        authentication_rejected=False,
        resource_contract_changed=False,
        unsupported_identity_after_update=False,
        port_range_exhausted=False,
    )
    unhealthy = SimpleNamespace(
        authentication_rejected=True,
        resource_contract_changed=False,
        unsupported_identity_after_update=False,
        port_range_exhausted=False,
    )
    async_sync_runtime_issues(hass, healthy_entry.entry_id, healthy)
    async_sync_runtime_issues(hass, unhealthy_entry.entry_id, unhealthy)
    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock()
    unhealthy_entry.runtime_data = coordinator

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=True),
    ):
        assert await async_unload_entry(hass, unhealthy_entry)

    assert ir.async_get(hass).async_get_issue(DOMAIN, "authentication_rejected") is None
    store = _store(hass)
    assert unhealthy_entry.entry_id not in store.entries
    assert unhealthy_entry.entry_id not in store.runtime_pending
    assert unhealthy_entry.entry_id not in store.certificate_pending
    coordinator.async_shutdown.assert_awaited_once_with()


async def test_enabled_reload_preserves_issue_until_runtime_is_evaluated(
    hass,
    credentials,
) -> None:
    from custom_components.samsung_ac_windfree import (
        async_setup_entry,
        async_unload_entry,
    )
    from custom_components.samsung_ac_windfree.repairs import (
        _store,
        async_sync_runtime_issues,
    )

    entry = _entry(credentials)
    entry.add_to_hass(hass)
    rejected = SimpleNamespace(
        authentication_rejected=True,
        resource_contract_changed=False,
        unsupported_identity_after_update=False,
        port_range_exhausted=False,
    )
    async_sync_runtime_issues(hass, entry.entry_id, rejected)
    old_coordinator = MagicMock()
    old_coordinator.async_shutdown = AsyncMock()
    entry.runtime_data = old_coordinator

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=True),
    ):
        assert await async_unload_entry(hass, entry)

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "authentication_rejected") is not None
    store = _store(hass)
    assert entry.entry_id in store.runtime_pending
    assert entry.entry_id in store.certificate_pending

    failed = MagicMock()
    failed.async_start = AsyncMock(side_effect=RuntimeError("offline"))
    failed.async_shutdown = AsyncMock()
    with (
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator",
            return_value=failed,
        ),
        pytest.raises(ConfigEntryNotReady) as caught,
    ):
        await async_setup_entry(hass, entry)

    _assert_translated_entry_error(caught.value, "setup_failed")
    assert registry.async_get_issue(DOMAIN, "authentication_rejected") is not None
    assert entry.entry_id in store.runtime_pending

    healthy = MagicMock()
    healthy.async_start = AsyncMock()
    healthy.async_shutdown = AsyncMock()
    healthy.async_add_listener.return_value = MagicMock()
    healthy.authentication_rejected = False
    healthy.health.authentication_rejected = False
    healthy.health.resource_contract_changed = False
    healthy.health.unsupported_identity_after_update = False
    healthy.health.port_range_exhausted = False
    with (
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator",
            return_value=healthy,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ),
    ):
        assert await async_setup_entry(hass, entry)

    assert registry.async_get_issue(DOMAIN, "authentication_rejected") is None
    assert entry.entry_id not in store.runtime_pending


async def test_permanent_entry_removal_purges_private_repair_state(
    hass,
    credentials,
) -> None:
    from custom_components.samsung_ac_windfree import async_remove_entry
    from custom_components.samsung_ac_windfree.repairs import (
        _store,
        async_sync_runtime_issues,
    )

    entry = _entry(credentials)
    entry.add_to_hass(hass)
    async_sync_runtime_issues(
        hass,
        entry.entry_id,
        SimpleNamespace(
            authentication_rejected=True,
            resource_contract_changed=False,
            unsupported_identity_after_update=False,
            port_range_exhausted=False,
        ),
    )

    await async_remove_entry(hass, entry)

    store = _store(hass)
    assert entry.entry_id not in store.entries
    assert entry.entry_id not in store.runtime_pending
    assert entry.entry_id not in store.certificate_pending
    assert ir.async_get(hass).async_get_issue(DOMAIN, "authentication_rejected") is None


async def test_update_listener_reloads_entry(hass, credentials) -> None:
    from custom_components.samsung_ac_windfree import _async_reload_entry

    entry = _entry(credentials)
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    ) as reload_entry:
        await _async_reload_entry(hass, entry)
    reload_entry.assert_awaited_once_with(entry.entry_id)


async def test_update_reload_suppresses_auth_listener_and_does_not_start_reauth(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import (
        _async_reload_entry,
        async_setup_entry,
    )

    entry = _entry(credentials)
    entry.async_start_reauth = MagicMock()
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.authentication_rejected = False
    listener = None
    unsubscribe = MagicMock()

    def add_listener(candidate):
        nonlocal listener
        listener = candidate
        return unsubscribe

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

    async def reload(_entry_id):
        coordinator.authentication_rejected = True
        assert listener is not None
        listener()
        return True

    with patch.object(hass.config_entries, "async_reload", side_effect=reload):
        await _async_reload_entry(hass, entry)

    unsubscribe.assert_called_once_with()
    entry.async_start_reauth.assert_not_called()


async def test_failed_reload_after_successful_unload_does_not_restore_old_lifecycle(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import (
        _async_reload_entry,
        _lifecycles,
        async_setup_entry,
    )

    entry = _entry(credentials)
    entry.async_start_reauth = MagicMock()
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.authentication_rejected = False
    listeners = []
    unsubscribes = []

    def add_listener(listener):
        listeners.append(listener)
        unsubscribe = MagicMock()
        unsubscribes.append(unsubscribe)
        return unsubscribe

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

    async def reload(_entry_id):
        _lifecycles(hass).pop(entry.entry_id)
        coordinator.authentication_rejected = True
        return False

    with (
        patch.object(hass.config_entries, "async_reload", side_effect=reload),
        pytest.raises(ConfigEntryError) as caught,
    ):
        await _async_reload_entry(hass, entry)

    _assert_translated_entry_error(caught.value, "reload_failed")
    unsubscribes[0].assert_called_once_with()
    assert len(listeners) == 1
    assert entry.entry_id not in _lifecycles(hass)
    listeners[0]()
    entry.async_start_reauth.assert_not_called()


async def test_failed_reload_unload_restores_and_reconciles_auth_once(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import (
        _async_reload_entry,
        async_setup_entry,
    )

    entry = _entry(credentials)
    entry.async_start_reauth = MagicMock()
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_shutdown = AsyncMock()
    coordinator.authentication_rejected = False
    listeners = []

    def add_listener(listener):
        listeners.append(listener)
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

    async def reload(_entry_id):
        coordinator.authentication_rejected = True
        return False

    with (
        patch.object(hass.config_entries, "async_reload", side_effect=reload),
        pytest.raises(ConfigEntryError) as caught,
    ):
        await _async_reload_entry(hass, entry)

    _assert_translated_entry_error(caught.value, "reload_failed")
    assert len(listeners) == 2
    entry.async_start_reauth.assert_called_once_with(hass)
    listeners[-1]()
    entry.async_start_reauth.assert_called_once_with(hass)


@pytest.mark.parametrize("version", [1])
async def test_version_one_migration_is_idempotent(hass, credentials, version) -> None:
    from custom_components.samsung_ac_windfree import async_migrate_entry

    entry = _entry(credentials)
    original = dict(entry.data)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == 1
    assert dict(entry.data) == original


async def test_version_one_minor_zero_migrates_once(hass, credentials) -> None:
    from custom_components.samsung_ac_windfree import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data=_entry_data(credentials),
        version=1,
        minor_version=0,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == 1
    assert entry.minor_version == 1
    with patch.object(hass.config_entries, "async_update_entry") as update:
        assert await async_migrate_entry(hass, entry)
    update.assert_not_called()


async def test_future_minor_version_is_rejected_without_downgrade(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data=_entry_data(credentials),
        version=1,
        minor_version=2,
    )
    entry.add_to_hass(hass)

    with patch.object(hass.config_entries, "async_update_entry") as update:
        assert not await async_migrate_entry(hass, entry)

    assert entry.minor_version == 2
    update.assert_not_called()


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
        pytest.raises(ConfigEntryError) as caught,
    ):
        await async_setup_entry(hass, entry)

    _assert_translated_entry_error(caught.value, "invalid_stored_credentials")
    constructor.assert_not_called()
    assert HOST not in repr(caught.value)
    assert credentials.client_key_pem not in repr(caught.value)
    traceback_locals = _integration_traceback_locals(caught.value)
    assert HOST not in traceback_locals
    assert credentials.client_key_pem not in traceback_locals
    assert credentials.client_chain_pem not in traceback_locals


_INTEGRATION_DIR = Path("custom_components/samsung_ac_windfree")
_METADATA_FILES = (
    _INTEGRATION_DIR / "strings.json",
    _INTEGRATION_DIR / "translations/en.json",
)
_CONFIG_ERRORS = {
    "bootstrap_invalid_material",
    "bootstrap_pin_mismatch",
    "bootstrap_unavailable",
    "cannot_connect",
    "cannot_resolve",
    "capability_mismatch",
    "dns_timeout",
    "fetch_timeout",
    "invalid_clock",
    "read_timeout",
    "setup_timeout",
    "sweep_timeout",
    "unknown",
    "unsupported_device",
}
_CONFIG_ABORTS = {
    "already_configured",
    "already_in_progress",
    "reauth_successful",
    "reconfigure_successful",
    "unique_id_mismatch",
    "unknown",
}
_ENTITY_KEYS = {
    "binary_sensor": {
        "current_limit_enabled",
        "filter_attention",
        "problem",
    },
    "sensor": {
        "active_alarm",
        "current_limit_level",
        "energy_consumption",
        "filter_status",
        "filter_usage",
    },
    "switch": {"auto_clean", "display_light"},
}
_LIFECYCLE_EXCEPTION_KEYS = {
    "authentication_rejected",
    "capability_mismatch",
    "credentials_expired",
    "credentials_not_yet_valid",
    "invalid_stored_credentials",
    "invalid_stored_entry",
    "reload_failed",
    "setup_failed",
    "unload_failed",
    "unsupported_device",
}
_EXCEPTION_KEYS = _LIFECYCLE_EXCEPTION_KEYS | {
    "command_failed",
    "command_incompatible",
    "command_rejected",
    "command_unavailable",
    "invalid_command",
    "invalid_temperature",
}
_ISSUE_KEYS = {
    "authentication_rejected",
    "bootstrap_pin_changed",
    "bootstrap_unavailable",
    "certificate_expiring",
    "port_range_exhausted",
    "resource_contract_changed",
    "unsupported_identity_after_update",
}
_BRONZE_SILVER_RULES = {
    "action-exceptions",
    "action-setup",
    "appropriate-polling",
    "brands",
    "common-modules",
    "config-entry-unloading",
    "config-flow",
    "config-flow-test-coverage",
    "dependency-transparency",
    "docs-actions",
    "docs-conditions",
    "docs-configuration-parameters",
    "docs-high-level-description",
    "docs-installation-instructions",
    "docs-installation-parameters",
    "docs-removal-instructions",
    "docs-triggers",
    "entity-event-setup",
    "entity-unavailable",
    "entity-unique-id",
    "has-entity-name",
    "integration-owner",
    "log-when-unavailable",
    "parallel-updates",
    "reauthentication-flow",
    "runtime-data",
    "test-before-configure",
    "test-before-setup",
    "test-coverage",
    "unique-config-entry",
}


def _load_strings() -> dict[str, object]:
    return json.loads((_INTEGRATION_DIR / "strings.json").read_text())


def test_translation_mirrors_strings_and_uses_current_schema() -> None:
    strings = _load_strings()
    english = json.loads((_INTEGRATION_DIR / "translations/en.json").read_text())

    assert english == strings
    assert set(strings) == {"config", "entity", "exceptions", "issues"}


@pytest.mark.parametrize(
    ("category", "expected_key"),
    [
        ("config", "component.samsung_ac_windfree.config.step.user.title"),
        (
            "entity",
            "component.samsung_ac_windfree.entity.sensor.filter_status.state.wash",
        ),
        (
            "exceptions",
            "component.samsung_ac_windfree.exceptions.command_failed.message",
        ),
        (
            "issues",
            "component.samsung_ac_windfree.issues.certificate_expiring."
            "fix_flow.step.confirm.title",
        ),
    ],
)
async def test_home_assistant_loads_translation_categories(
    hass, category, expected_key
) -> None:
    from homeassistant.helpers import translation

    translated = await translation.async_get_translations(
        hass,
        "en",
        category,
        integrations={DOMAIN},
    )

    assert expected_key in translated


def test_all_config_flow_translation_keys_are_defined() -> None:
    config = _load_strings()["config"]

    assert set(config["step"]) == {"reauth_confirm", "reconfigure", "user"}
    assert set(config["progress"]) == {"validate"}
    assert set(config["error"]) == _CONFIG_ERRORS
    assert set(config["abort"]) == _CONFIG_ABORTS
    for step in ("user", "reconfigure"):
        assert set(config["step"][step]["data"]) == {"host"}
        assert set(config["step"][step]["data_description"]) == {"host"}


def test_all_entity_and_command_translation_keys_are_defined() -> None:
    strings = _load_strings()
    entities = strings["entity"]

    assert set(entities) == set(_ENTITY_KEYS)
    for platform, keys in _ENTITY_KEYS.items():
        assert set(entities[platform]) == keys
    assert set(entities["sensor"]["filter_status"]["state"]) == {
        "normal",
        "replace",
        "wash",
    }
    assert set(strings["exceptions"]) == _EXCEPTION_KEYS
    assert all(set(value) == {"message"} for value in strings["exceptions"].values())


def test_platforms_explicitly_delegate_parallelism_to_coordinator() -> None:
    from custom_components.samsung_ac_windfree import (
        binary_sensor,
        climate,
        sensor,
        switch,
    )

    assert {
        binary_sensor.PARALLEL_UPDATES,
        climate.PARALLEL_UPDATES,
        sensor.PARALLEL_UPDATES,
        switch.PARALLEL_UPDATES,
    } == {0}


@pytest.mark.parametrize(
    ("error_type", "translation_key"),
    [
        (ConfigEntryAuthFailed, "authentication_rejected"),
        (ConfigEntryError, "capability_mismatch"),
        (ConfigEntryAuthFailed, "credentials_expired"),
        (ConfigEntryAuthFailed, "credentials_not_yet_valid"),
        (ConfigEntryError, "invalid_stored_credentials"),
        (ConfigEntryError, "invalid_stored_entry"),
        (ConfigEntryError, "reload_failed"),
        (ConfigEntryNotReady, "setup_failed"),
        (ConfigEntryError, "unload_failed"),
        (ConfigEntryError, "unsupported_device"),
    ],
)
def test_lifecycle_entry_errors_use_translation_metadata(
    error_type, translation_key
) -> None:
    from custom_components.samsung_ac_windfree import _translated_entry_error

    error = _translated_entry_error(error_type, translation_key)

    assert type(error) is error_type
    _assert_translated_entry_error(error, translation_key)


def test_every_lifecycle_error_callsite_uses_the_translated_factory() -> None:
    tree = ast.parse((_INTEGRATION_DIR / "__init__.py").read_text())
    callsite_keys = {
        call.args[1].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_translated_entry_error"
        and len(call.args) == 2
        and isinstance(call.args[1], ast.Constant)
        and isinstance(call.args[1].value, str)
    }
    direct_lifecycle_constructors = {
        call.func.id
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id
        in {"ConfigEntryAuthFailed", "ConfigEntryError", "ConfigEntryNotReady"}
    }

    assert callsite_keys == _LIFECYCLE_EXCEPTION_KEYS
    assert direct_lifecycle_constructors == set()


def test_all_repair_issue_and_fix_flow_keys_are_defined() -> None:
    issues = _load_strings()["issues"]

    assert set(issues) == _ISSUE_KEYS
    for issue_id, issue in issues.items():
        if issue_id in {"authentication_rejected", "certificate_expiring"}:
            assert set(issue) == {"title", "fix_flow"}
            fix_flow = issue["fix_flow"]
            assert set(fix_flow["step"]) == {"confirm"}
            assert {"title", "description"} == set(fix_flow["step"]["confirm"])
            assert {"issue_resolved", "unknown_issue"} <= set(fix_flow["abort"])
        else:
            assert {"title", "description"} <= set(issue)
            assert "fix_flow" not in issue


def test_user_visible_metadata_contains_no_private_protocol_material() -> None:
    from custom_components.samsung_ac_windfree.const import (
        REMOVED_SIGNING_DIGEST_NAME,
        BUNDLE_SHA256,
        BUNDLE_URL,
        SAMSUNG_IDENTITY_HOST,
        SAMSUNG_IDENTITY_LEAF_SHA256,
        SAMSUNG_IDENTITY_SPKI_SHA256,
    )

    text = "\n".join(
        path.read_text()
        for path in (*_METADATA_FILES, Path("README.md"), Path("CHANGELOG.md"))
    )
    for private_value in (
        REMOVED_SIGNING_DIGEST_NAME,
        BUNDLE_SHA256,
        BUNDLE_URL,
        SAMSUNG_IDENTITY_HOST,
        SAMSUNG_IDENTITY_LEAF_SHA256,
        SAMSUNG_IDENTITY_SPKI_SHA256,
    ):
        assert private_value not in text
    assert "BEGIN PRIVATE KEY" not in text
    assert (
        re.search(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            text,
            flags=re.IGNORECASE,
        )
        is None
    )
    assert "/oic/" not in text
    assert "/vs/" not in text


def test_readme_documents_security_scope_operation_and_examples() -> None:
    readme = Path("README.md").read_text()

    for phrase in (
        "AR60F12C1AWNEU",
        "TP1X_DA-AC-RAC-01001_0000",
        "does not report the consumer SKU",
        "fully local after setup",
        "unofficial certificate",
        "Home Assistant backups",
        "SmartThings is not required",
        "host or IP address",
        "one-time internet bootstrap",
        "OBSERVE",
        "polling",
        "Reconfigure",
        "reauthentication",
        "certificate expiry",
        "competing clients",
        "BUNDLE_SHA256",
        "update only `BUNDLE_URL`",
        "unpinned fallback mirrors",
        "must not be committed to Git",
        "climate.set_temperature",
        "climate.set_hvac_mode",
        "mode: heat",
        "mode: cool",
        "Requires Home Assistant 2026.7.3",
    ):
        assert phrase in readme
    assert "ordered 39-resource directory" not in readme
    for heading in (
        "## Installation",
        "## Removal",
        "## Supported device and firmware",
        "## Entities and controls",
        "## Update behavior",
        "## Limitations",
        "## Troubleshooting",
        "## Automation examples",
        "## Bootstrap source and pin maintenance",
    ):
        assert heading in readme
    assert "https://github.com/bonzanni/ha-samsung-ac-windfree/issues" in readme


def test_readme_warm_cool_automation_chooses_exactly_one_mode() -> None:
    readme = Path("README.md").read_text()
    example = readme.partition("Switch between cool and warm mode from an input:")[2]
    yaml_text = example.partition("```yaml")[2].partition("```")[0]

    automation = yaml.safe_load(yaml_text)
    assert "input_select.windfree_mode" in yaml_text
    assert len(automation["actions"]) == 1
    choice = automation["actions"][0]
    assert set(choice) == {"choose", "default"}
    sequences = [option["sequence"] for option in choice["choose"]]
    sequences.append(choice["default"])
    assert all(len(sequence) == 1 for sequence in sequences)
    assert {sequence[0]["data"]["hvac_mode"] for sequence in sequences} == {
        "cool",
        "heat",
    }
    assert all(
        sequence[0]["action"] == "climate.set_hvac_mode" for sequence in sequences
    )


def test_release_metadata_matches_manifest_and_supported_scope() -> None:
    manifest = json.loads((_INTEGRATION_DIR / "manifest.json").read_text())
    changelog = Path("CHANGELOG.md").read_text()
    license_text = Path("LICENSE").read_text()

    assert manifest["version"] == "0.2.0"
    assert manifest["documentation"] in Path("README.md").read_text()
    assert "## [0.2.0]" in changelog
    for phrase in (
        "AR60F12C1AWNEU",
        "TP1X_DA-AC-RAC-01001_0000",
        "SmartThings",
        "Older models",
        "multi-device",
        "cloud control",
    ):
        assert phrase in changelog
    assert license_text.startswith("MIT License")
    assert "Copyright (c) 2026" in license_text
    assert "Permission is hereby granted, free of charge" in license_text


def test_quality_scale_truthfully_covers_bronze_and_silver() -> None:
    quality = yaml.safe_load((_INTEGRATION_DIR / "quality_scale.yaml").read_text())
    rules = quality["rules"]

    assert set(rules) == _BRONZE_SILVER_RULES
    for value in rules.values():
        if isinstance(value, str):
            assert value == "done"
        else:
            assert value["status"] == "exempt"
            assert value["comment"].strip()
