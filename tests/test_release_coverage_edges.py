from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryAuthFailed, ConfigEntryError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_ac_windfree.const import DOMAIN
from custom_components.samsung_ac_windfree.models import (
    CapabilityMismatch,
    ClimateState,
    Credentials,
)


def test_stored_value_helpers_reject_every_invalid_shape(credentials) -> None:
    from custom_components.samsung_ac_windfree import (
        _certificate_validity,
        _stored_credentials,
        _stored_endpoint,
    )

    valid = {
        "client_key_pem": credentials.client_key_pem,
        "client_chain_pem": credentials.client_chain_pem,
        "not_before": credentials.not_before,
        "not_after": credentials.not_after,
    }
    assert _stored_credentials(valid) == credentials
    for replacement in (
        {"client_key_pem": object()},
        {"not_before": "2026-07-24T00:00:00"},
        {"not_after": credentials.not_before},
    ):
        assert _stored_credentials(valid | replacement) is None
    assert _stored_credentials({}) is None

    assert _stored_endpoint({"host": "ac.example.test", "port": 49152}) == (
        "ac.example.test",
        49152,
    )
    for endpoint in (
        {},
        {"host": "", "port": 49152},
        {"host": "ac.example.test", "port": True},
        {"host": "ac.example.test", "port": 49161},
    ):
        assert _stored_endpoint(endpoint) is None

    assert (
        _certificate_validity(
            Credentials(
                client_key_pem="key",
                client_chain_pem="chain",
                not_before="2026-07-24T00:00:00",
                not_after=credentials.not_after,
            )
        )
        is None
    )


def test_lifecycle_attach_is_idempotent_and_suspend_without_listener_is_safe() -> None:
    from custom_components.samsung_ac_windfree import _EntryLifecycle

    coordinator = MagicMock()
    unsubscribe = MagicMock()
    coordinator.async_add_listener.return_value = unsubscribe
    coordinator.authentication_rejected = False
    lifecycle = _EntryLifecycle("entry", coordinator, MagicMock())

    with patch(
        "custom_components.samsung_ac_windfree.async_sync_runtime_issues"
    ) as sync:
        lifecycle.attach()
        lifecycle.attach()
        lifecycle.suspend()
        lifecycle.suspend()

    coordinator.async_add_listener.assert_called_once_with(lifecycle.handle_update)
    unsubscribe.assert_called_once_with()
    sync.assert_called_once()


async def test_shutdown_helper_reports_child_cancellation(hass) -> None:
    from custom_components.samsung_ac_windfree import _async_shutdown_cancellation_safe

    coordinator = MagicMock()
    coordinator.async_shutdown = AsyncMock(
        side_effect=asyncio.CancelledError("child_cancelled")
    )

    outcome = await _async_shutdown_cancellation_safe(hass, coordinator)

    assert not outcome.completed
    assert outcome.cancellation_args is None


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (asyncio.CancelledError("reload_cancelled"), asyncio.CancelledError),
        (RuntimeError("private reload failure"), ConfigEntryError),
    ],
)
async def test_reload_sanitizes_direct_failures(hass, side_effect, expected) -> None:
    from custom_components.samsung_ac_windfree import _async_reload_entry

    entry = MockConfigEntry(domain=DOMAIN)
    with (
        patch.object(
            hass.config_entries,
            "async_reload",
            new=AsyncMock(side_effect=side_effect),
        ),
        pytest.raises(expected),
    ):
        await _async_reload_entry(hass, entry)


def _entry_data(credentials: Credentials) -> dict[str, object]:
    return {
        "host": "ac.example.test",
        "port": 49154,
        "client_key_pem": credentials.client_key_pem,
        "client_chain_pem": credentials.client_chain_pem,
        "not_before": credentials.not_before,
        "not_after": credentials.not_after,
    }


@pytest.mark.parametrize(
    "replacement",
    [
        {"host": "", "port": 49154},
        {"host": "ac.example.test", "port": 49161},
    ],
)
async def test_setup_rejects_invalid_stored_endpoint_offline(
    hass, credentials, replacement
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_entry_data(credentials) | replacement,
    )
    with (
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator"
        ) as constructor,
        pytest.raises(ConfigEntryError, match="invalid_stored_entry"),
    ):
        await async_setup_entry(hass, entry)
    constructor.assert_not_called()


@pytest.mark.parametrize(
    ("startup_error", "expected_key"),
    [
        (CapabilityMismatch("private contract"), "capability_mismatch"),
        (None, "authentication_rejected"),
    ],
)
async def test_setup_maps_capability_and_post_start_auth_failures(
    hass, credentials, startup_error, expected_key
) -> None:
    from custom_components.samsung_ac_windfree import async_setup_entry

    entry = MockConfigEntry(domain=DOMAIN, data=_entry_data(credentials))
    entry.async_start_reauth = MagicMock()
    coordinator = MagicMock()
    coordinator.async_start = AsyncMock(side_effect=startup_error)
    coordinator.async_shutdown = AsyncMock()
    coordinator.authentication_rejected = startup_error is None
    coordinator.health = SimpleNamespace(
        authentication_rejected=startup_error is None,
    )
    with (
        patch(
            "custom_components.samsung_ac_windfree.WindFreeCoordinator",
            return_value=coordinator,
        ),
        patch("custom_components.samsung_ac_windfree.async_sync_runtime_issues"),
        pytest.raises(
            ConfigEntryAuthFailed
            if expected_key == "authentication_rejected"
            else ConfigEntryError,
            match=expected_key,
        ),
    ):
        await async_setup_entry(hass, entry)

    coordinator.async_shutdown.assert_awaited_once_with()
    if expected_key == "authentication_rejected":
        entry.async_start_reauth.assert_called_once_with(hass)
    else:
        entry.async_start_reauth.assert_not_called()


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (asyncio.CancelledError("unload_cancelled"), asyncio.CancelledError),
        (RuntimeError("private unload failure"), ConfigEntryError),
    ],
)
async def test_unload_sanitizes_platform_failures(
    hass, credentials, side_effect, expected
) -> None:
    from custom_components.samsung_ac_windfree import async_unload_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=_entry_data(credentials),
    )
    with (
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new=AsyncMock(side_effect=side_effect),
        ),
        pytest.raises(expected),
    ):
        await async_unload_entry(hass, entry)


async def test_major_and_future_minor_migrations_are_both_rejected(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree import async_migrate_entry

    major = MockConfigEntry(
        domain=DOMAIN,
        data=_entry_data(credentials),
        version=2,
        minor_version=0,
    )
    future_minor = MockConfigEntry(
        domain=DOMAIN,
        data=_entry_data(credentials),
        version=1,
        minor_version=2,
    )

    assert not await async_migrate_entry(hass, major)
    assert not await async_migrate_entry(hass, future_minor)


async def test_host_resolution_delegates_to_datagram_resolver(hass) -> None:
    from custom_components.samsung_ac_windfree.config_flow import async_resolve_host

    loop = MagicMock()
    loop.getaddrinfo = AsyncMock()
    with patch("asyncio.get_running_loop", return_value=loop):
        await async_resolve_host(hass, "ac.example.test")
    loop.getaddrinfo.assert_awaited_once()


async def test_validation_cleanup_retains_latest_cancellation_and_owns_close(
    hass,
) -> None:
    from custom_components.samsung_ac_windfree.config_flow import (
        _async_close_validation_transport,
    )

    started = asyncio.Event()
    release = asyncio.Event()
    transport = MagicMock()

    async def close() -> None:
        started.set()
        await release.wait()
        raise RuntimeError("private close failure")

    transport.async_close = AsyncMock(side_effect=close)
    task = hass.async_create_task(_async_close_validation_transport(hass, transport))
    await started.wait()
    task.cancel("first")
    await asyncio.sleep(0)
    task.cancel("second")
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError, match="second"):
        await task
    transport.async_close.assert_awaited_once_with()


@pytest.mark.parametrize("failure_stage", ["discover", "read", "parse"])
async def test_validation_pipeline_sanitizes_unexpected_stage_errors(
    hass, credentials, failure_stage
) -> None:
    from custom_components.samsung_ac_windfree.config_flow import (
        _async_validate_pipeline,
    )

    transport = AsyncMock()
    discover = AsyncMock(return_value=(49154, transport))
    parse = MagicMock(
        return_value=SimpleNamespace(device_id="device"),
    )
    if failure_stage == "discover":
        discover.side_effect = RuntimeError("private discovery failure")
    elif failure_stage == "read":
        transport.async_get.side_effect = RuntimeError("private read failure")
    else:
        transport.async_get.return_value = {}
        parse.side_effect = RuntimeError("private parser failure")

    with (
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_resolve_host",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow."
            "async_bootstrap_credentials",
            new=AsyncMock(return_value=credentials),
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow."
            "async_discover_transport",
            new=discover,
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow.parse_identity",
            new=parse,
        ),
    ):
        outcome = await _async_validate_pipeline(hass, "ac.example.test")

    expected = "capability_mismatch" if failure_stage == "parse" else "cannot_connect"
    assert outcome.error_key == expected
    if failure_stage != "discover":
        transport.async_close.assert_awaited_once_with()


async def test_flow_handles_unknown_pending_cancelled_and_empty_states(hass) -> None:
    from custom_components.samsung_ac_windfree.config_flow import (
        ConfigFlow,
        SetupValidationError,
        _flow_error,
    )

    flow = ConfigFlow()
    flow.hass = hass
    assert (await flow.async_step_validate())["reason"] == "unknown"

    release = asyncio.Event()
    pending = hass.async_create_task(release.wait())
    flow._validation_task = pending
    assert (await flow.async_step_validate())["type"].value == "progress"
    release.set()
    await pending

    async def cancelled() -> None:
        raise asyncio.CancelledError("validation_cancelled")

    flow._validation_task = hass.async_create_task(cancelled())
    await asyncio.sleep(0)
    with pytest.raises(asyncio.CancelledError, match="validation_cancelled"):
        await flow.async_step_validate()

    async def fetch_timeout() -> None:
        raise SetupValidationError("fetch_timeout")

    flow = ConfigFlow()
    flow.hass = hass
    flow._validation_task = hass.async_create_task(fetch_timeout())
    await asyncio.sleep(0)
    assert (await flow.async_step_validate())["type"].value == "progress_done"
    assert flow._bootstrap_status == "unavailable"

    flow = ConfigFlow()
    flow.hass = hass
    assert (await flow.async_step_finish())["reason"] == "unknown"
    assert _flow_error(RuntimeError("private")) == "unknown"


class _BrokenMapping(dict[str, object]):
    def get(self, key: str, default: object = None) -> object:
        raise RuntimeError("private mapping failure")

    def __iter__(self) -> Iterator[str]:
        return super().__iter__()


def test_diagnostic_certificate_handles_hostile_and_invalid_mappings() -> None:
    from custom_components.samsung_ac_windfree.diagnostics import _certificate

    now = datetime(2026, 7, 24, tzinfo=UTC)
    assert _certificate(_BrokenMapping(), now) == {
        "not_before": None,
        "not_after": None,
        "days_to_expiry": None,
    }
    result = _certificate(
        {
            "not_before": "not-a-date",
            "not_after": "also-not-a-date",
        },
        now,
    )
    assert result["not_before"] is None
    assert result["not_after"] is None


def test_identity_and_humidity_invariants_reject_missing_or_invalid_state() -> None:
    from custom_components.samsung_ac_windfree.entity import WindFreeEntity

    coordinator = MagicMock()
    coordinator.data.identity = None
    with pytest.raises(ValueError, match="identity_unavailable"):
        WindFreeEntity(coordinator)
    with pytest.raises(ValueError, match="humidity"):
        ClimateState(humidity=0)
