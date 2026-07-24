from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_ac_windfree.const import (
    DOMAIN,
    SUPPORTED_MODEL,
)
from custom_components.samsung_ac_windfree.coordinator import (
    COLD_PATHS,
    HOT_PATHS,
    WARM_PATHS,
    WindFreeCoordinator,
)
from custom_components.samsung_ac_windfree.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.samsung_ac_windfree.models import UpdateSource, WindFreeData
from custom_components.samsung_ac_windfree.repairs import (
    async_purge_entry_issues,
    async_sync_bootstrap_issue,
    async_sync_certificate_issue,
    async_sync_runtime_issues,
)

_SECRETS = (
    "192.0.2.10",
    "00000000-0000-4000-8000-000000000001",
    "PRIVATE KEY",
    "AA:BB:CC:DD:EE:FF",
    "E101",
)


@pytest.fixture
def coordinator(hass, credentials):
    instance = WindFreeCoordinator(
        hass,
        host=_SECRETS[0],
        port=49154,
        credentials=credentials,
        compatibility={},
        start_scheduler=False,
    )
    instance._transport = AsyncMock()
    instance._generation = 3
    instance._reconnect_attempts = 2
    instance._update_counts[UpdateSource.POLL] = 4
    instance._update_counts[UpdateSource.OBSERVE] = 8
    instance._update_counts[UpdateSource.RECONCILE] = 2
    instance._update_counts[UpdateSource.COMMAND] = 1
    instance._latency_buckets.update(
        {
            "under_100ms": 9,
            "under_500ms": 3,
            "under_1s": 1,
            "at_least_1s": 0,
        }
    )
    instance.data = replace(
        WindFreeData.empty(),
        available=True,
        generation=3,
        update_source=UpdateSource.RECONCILE,
    )
    paths = HOT_PATHS + WARM_PATHS + COLD_PATHS
    instance._resources = {path: {} for path in paths}
    instance._last_updates = dict.fromkeys(paths, instance._monotonic())
    return instance


def _entry(coordinator, *, expires: datetime) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": _SECRETS[0],
            "device_id": _SECRETS[1],
            "client_key_pem": _SECRETS[2],
            "client_chain_pem": _SECRETS[3],
            "alarm_code": _SECRETS[4],
            "not_before": "2026-07-01T00:00:00+00:00",
            "not_after": expires.isoformat(),
            "arbitrary": {"nested": _SECRETS},
        },
    )
    entry.runtime_data = coordinator
    return entry


async def test_diagnostics_are_explicit_allowlist_and_zero_io(
    coordinator,
) -> None:
    now = datetime(2026, 7, 24, tzinfo=UTC)
    entry = _entry(coordinator, expires=now + timedelta(days=91))
    transport = coordinator.transport
    transport.async_get.reset_mock()
    transport.async_post.reset_mock()
    transport.async_observe.reset_mock()

    result = await async_get_config_entry_diagnostics(
        coordinator.hass,
        entry,
        now=now,
    )

    assert set(result) == {
        "integration_version",
        "dependency_version",
        "supported_product",
        "connection",
        "updates",
        "resource_coverage",
        "certificate",
        "entity_support",
    }
    assert result["supported_product"] == SUPPORTED_MODEL
    assert set(result["connection"]) == {
        "available",
        "generation",
        "reason",
        "failure_count",
        "reconnect_attempts",
        "reconnect_delay_seconds",
        "port_range_exhausted",
    }
    assert set(result["updates"]) == {
        "source",
        "hot_age_seconds",
        "poll_count",
        "observe_count",
        "reconcile_count",
        "command_count",
        "latency_under_100ms",
        "latency_under_500ms",
        "latency_under_1s",
        "latency_at_least_1s",
    }
    assert set(result["certificate"]) == {
        "not_before",
        "not_after",
        "days_to_expiry",
    }
    assert set(result["entity_support"]) == {
        "climate",
        "humidity",
        "filter_usage",
        "filter_status",
        "energy_consumption",
        "active_alarm",
        "problem",
        "filter_attention",
        "display_light",
        "auto_clean",
        "current_limit_enabled",
        "current_limit_level",
    }
    assert result["certificate"]["days_to_expiry"] == 91
    transport.async_get.assert_not_called()
    transport.async_post.assert_not_called()
    transport.async_observe.assert_not_called()


async def test_adversarial_entry_and_runtime_secrets_never_escape(
    coordinator,
) -> None:
    now = datetime(2026, 7, 24, tzinfo=UTC)
    entry = _entry(coordinator, expires=now + timedelta(days=30))
    coordinator._host = _SECRETS[0]

    result = await async_get_config_entry_diagnostics(
        coordinator.hass,
        entry,
        now=now,
    )

    rendered = repr(result)
    assert all(secret not in rendered for secret in _SECRETS)


async def test_missing_or_invalid_runtime_and_certificate_are_safe(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "not_before": object(),
            "not_after": "not-a-date",
            "private": _SECRETS,
        },
    )
    entry.runtime_data = MagicMock(side_effect=RuntimeError(_SECRETS[0]))

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["connection"] == {
        "available": False,
        "generation": 0,
        "reason": "not_loaded",
        "failure_count": 0,
        "reconnect_attempts": 0,
        "reconnect_delay_seconds": 0,
        "port_range_exhausted": False,
    }
    assert result["certificate"] == {
        "not_before": None,
        "not_after": None,
        "days_to_expiry": None,
    }
    assert all(secret not in repr(result) for secret in _SECRETS)


async def test_spoofed_coordinator_type_cannot_supply_diagnostic_values(hass) -> None:
    spoof = MagicMock(spec=WindFreeCoordinator)
    spoof.health.connection_reason = _SECRETS[0]
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.runtime_data = spoof

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["connection"]["reason"] == "not_loaded"
    assert _SECRETS[0] not in repr(result)


async def test_adversarial_entry_accessors_cannot_raise_or_leak(hass) -> None:
    class AdversarialEntry:
        @property
        def runtime_data(self):
            raise RuntimeError(_SECRETS[0])

        @property
        def data(self):
            raise RuntimeError(_SECRETS[2])

    result = await async_get_config_entry_diagnostics(hass, AdversarialEntry())

    assert result["connection"]["reason"] == "not_loaded"
    assert result["certificate"]["not_after"] is None
    assert all(secret not in repr(result) for secret in _SECRETS)


async def test_runtime_repairs_create_once_and_delete_on_recovery(
    hass,
    coordinator,
) -> None:
    registry = ir.async_get(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    active = replace(
        coordinator.health,
        authentication_rejected=True,
        unsupported_identity_after_update=True,
        resource_contract_changed=True,
        port_range_exhausted=True,
    )

    async_sync_runtime_issues(hass, entry.entry_id, active)
    async_sync_runtime_issues(hass, entry.entry_id, active)

    expected = {
        "authentication_rejected",
        "resource_contract_changed",
        "unsupported_identity_after_update",
        "port_range_exhausted",
    }
    for issue_id in expected:
        issue = registry.async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.data is None
        assert issue.translation_placeholders is None
        assert all(secret not in repr(issue) for secret in _SECRETS)
    assert registry.async_get_issue(DOMAIN, "authentication_rejected").is_fixable
    assert not registry.async_get_issue(DOMAIN, "resource_contract_changed").is_fixable

    recovered = replace(
        active,
        authentication_rejected=False,
        unsupported_identity_after_update=False,
        resource_contract_changed=False,
        port_range_exhausted=False,
    )
    async_sync_runtime_issues(hass, entry.entry_id, recovered)

    assert all(registry.async_get_issue(DOMAIN, item) is None for item in expected)


async def test_entry_listener_synchronizes_runtime_transitions(
    hass, coordinator
) -> None:
    from custom_components.samsung_ac_windfree import _EntryLifecycle

    callback = None

    def add_listener(listener):
        nonlocal callback
        callback = listener
        return MagicMock()

    coordinator.async_add_listener = MagicMock(side_effect=add_listener)
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    lifecycle = _EntryLifecycle(entry.entry_id, coordinator, MagicMock())
    lifecycle.attach()
    assert callback is not None

    coordinator._disabled_write_paths = {"/private/resource"}
    callback()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "resource_contract_changed")
        is not None
    )

    coordinator._disabled_write_paths.clear()
    callback()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, "resource_contract_changed") is None
    )
    lifecycle.suspend()


async def test_bootstrap_repairs_are_mutually_exclusive_and_recover(hass) -> None:
    registry = ir.async_get(hass)

    async_sync_bootstrap_issue(hass, "bootstrap_pin_mismatch")
    assert registry.async_get_issue(DOMAIN, "bootstrap_pin_changed") is not None
    assert registry.async_get_issue(DOMAIN, "bootstrap_unavailable") is None

    async_sync_bootstrap_issue(hass, "bootstrap_unavailable")
    assert registry.async_get_issue(DOMAIN, "bootstrap_pin_changed") is None
    assert registry.async_get_issue(DOMAIN, "bootstrap_unavailable") is not None

    async_sync_bootstrap_issue(hass, None)
    assert registry.async_get_issue(DOMAIN, "bootstrap_pin_changed") is None
    assert registry.async_get_issue(DOMAIN, "bootstrap_unavailable") is None


async def test_certificate_repair_boundary_and_recovery(hass) -> None:
    registry = ir.async_get(hass)
    now = datetime(2026, 7, 24, tzinfo=UTC)
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    async_sync_certificate_issue(
        hass,
        entry.entry_id,
        now + timedelta(days=90),
        now=now,
    )
    issue = registry.async_get_issue(DOMAIN, "certificate_expiring")
    assert issue is not None
    assert issue.is_fixable
    assert issue.data is None
    assert issue.translation_placeholders is None

    async_sync_certificate_issue(
        hass,
        entry.entry_id,
        now + timedelta(days=90, microseconds=1),
        now=now,
    )
    assert registry.async_get_issue(DOMAIN, "certificate_expiring") is None


async def test_multi_entry_runtime_issue_aggregates_and_unload_recomputes(
    hass,
    coordinator,
) -> None:
    registry = ir.async_get(hass)
    healthy_entry = MockConfigEntry(domain=DOMAIN, data={})
    unhealthy_entry = MockConfigEntry(domain=DOMAIN, data={})
    healthy_entry.add_to_hass(hass)
    unhealthy_entry.add_to_hass(hass)
    healthy = coordinator.health
    unhealthy = replace(healthy, authentication_rejected=True)
    ir.async_create_issue(
        hass,
        DOMAIN,
        "authentication_rejected",
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="authentication_rejected",
    )

    async_sync_runtime_issues(hass, healthy_entry.entry_id, healthy)
    assert registry.async_get_issue(DOMAIN, "authentication_rejected") is not None

    async_sync_runtime_issues(hass, unhealthy_entry.entry_id, unhealthy)
    async_sync_runtime_issues(hass, healthy_entry.entry_id, healthy)
    assert registry.async_get_issue(DOMAIN, "authentication_rejected") is not None

    async_purge_entry_issues(hass, unhealthy_entry.entry_id)
    assert registry.async_get_issue(DOMAIN, "authentication_rejected") is None


async def test_multi_entry_certificate_issue_waits_for_all_entries(
    hass,
) -> None:
    registry = ir.async_get(hass)
    now = datetime(2026, 7, 24, tzinfo=UTC)
    first = MockConfigEntry(domain=DOMAIN, data={})
    second = MockConfigEntry(domain=DOMAIN, data={})
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        "certificate_expiring",
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="certificate_expiring",
    )

    async_sync_certificate_issue(
        hass,
        first.entry_id,
        now + timedelta(days=120),
        now=now,
    )
    assert registry.async_get_issue(DOMAIN, "certificate_expiring") is not None

    async_sync_certificate_issue(
        hass,
        second.entry_id,
        now + timedelta(days=30),
        now=now,
    )
    async_sync_certificate_issue(
        hass,
        first.entry_id,
        now + timedelta(days=120),
        now=now,
    )
    assert registry.async_get_issue(DOMAIN, "certificate_expiring") is not None

    async_sync_certificate_issue(
        hass,
        second.entry_id,
        now + timedelta(days=120),
        now=now,
    )
    assert registry.async_get_issue(DOMAIN, "certificate_expiring") is None
