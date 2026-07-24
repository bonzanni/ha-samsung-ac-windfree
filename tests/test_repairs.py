from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.data_entry_flow import FlowResultType, UnknownStep
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_ac_windfree.const import DOMAIN


async def test_certificate_expiry_fix_confirms_then_starts_reauth(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree.repairs import async_create_fix_flow

    now = datetime(2026, 7, 24, tzinfo=UTC)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "not_after": (now + timedelta(days=30)).isoformat(),
            "host": "ac.example.test",
            "client_key_pem": credentials.client_key_pem,
        },
    )
    entry.add_to_hass(hass)
    entry.async_start_reauth = MagicMock()
    flow = await async_create_fix_flow(hass, "certificate_expiring", None)
    flow.hass = hass

    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    with patch(
        "custom_components.samsung_ac_windfree.repairs.dt_util.utcnow",
        return_value=now,
    ):
        result = await flow.async_step_confirm({})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry.async_start_reauth.assert_called_once_with(hass)
    assert "ac.example.test" not in repr(result)
    assert credentials.client_key_pem not in repr(result)


async def test_certificate_expiry_fix_aborts_when_issue_is_resolved(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree.repairs import async_create_fix_flow

    now = datetime(2026, 7, 24, tzinfo=UTC)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"not_after": (now + timedelta(days=120)).isoformat()},
    )
    entry.add_to_hass(hass)
    entry.async_start_reauth = MagicMock()
    flow = await async_create_fix_flow(hass, "certificate_expiring", None)
    flow.hass = hass

    with patch(
        "custom_components.samsung_ac_windfree.repairs.dt_util.utcnow",
        return_value=now,
    ):
        result = await flow.async_step_confirm({})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "issue_resolved"
    entry.async_start_reauth.assert_not_called()


async def test_resolved_fix_via_repairs_manager_deletes_issue_and_cannot_reopen(
    hass,
) -> None:
    now = datetime(2026, 7, 24, tzinfo=UTC)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"not_after": (now + timedelta(days=120)).isoformat()},
    )
    entry.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        "certificate_expiring",
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="certificate_expiring",
    )
    assert await async_setup_component(hass, "repairs", {})
    hass.config.components.add(DOMAIN)
    manager = repairs_flow_manager(hass)
    assert manager is not None

    result = await manager.async_init(
        DOMAIN,
        context={},
        data={"issue_id": "certificate_expiring"},
    )
    assert result["type"] is FlowResultType.FORM

    with patch(
        "custom_components.samsung_ac_windfree.repairs.dt_util.utcnow",
        return_value=now,
    ):
        result = await manager.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "issue_resolved"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "certificate_expiring") is None
    with pytest.raises(UnknownStep):
        await manager.async_init(
            DOMAIN,
            context={},
            data={"issue_id": "certificate_expiring"},
        )


async def test_unknown_repair_aborts_without_action(hass) -> None:
    from custom_components.samsung_ac_windfree.repairs import async_create_fix_flow

    flow = await async_create_fix_flow(hass, "unknown", None)
    flow.hass = hass
    result = await flow.async_step_init()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown_issue"


async def test_authentication_repair_confirms_reauth_without_private_data(
    hass,
) -> None:
    from custom_components.samsung_ac_windfree.repairs import async_create_fix_flow

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "192.0.2.10",
            "device_id": "00000000-0000-4000-8000-000000000001",
            "client_key_pem": "PRIVATE KEY",
        },
    )
    entry.add_to_hass(hass)
    entry.async_start_reauth = MagicMock()
    from custom_components.samsung_ac_windfree.repairs import (
        async_sync_runtime_issues,
    )

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
    flow = await async_create_fix_flow(hass, "authentication_rejected", None)
    flow.hass = hass

    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    result = await flow.async_step_confirm({})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry.async_start_reauth.assert_called_once_with(hass)
    assert "192.0.2.10" not in repr(result)
    assert "00000000-0000-4000-8000-000000000001" not in repr(result)
    assert "PRIVATE KEY" not in repr(result)


async def test_repair_flows_reauthenticate_only_affected_entries(hass) -> None:
    from custom_components.samsung_ac_windfree.coordinator import CoordinatorHealth
    from custom_components.samsung_ac_windfree.models import UpdateSource
    from custom_components.samsung_ac_windfree.repairs import (
        async_create_fix_flow,
        async_sync_certificate_issue,
        async_sync_runtime_issues,
    )

    now = datetime(2026, 7, 24, tzinfo=UTC)
    affected = MockConfigEntry(
        domain=DOMAIN,
        data={"not_after": (now + timedelta(days=30)).isoformat()},
    )
    healthy = MockConfigEntry(
        domain=DOMAIN,
        data={"not_after": (now + timedelta(days=120)).isoformat()},
    )
    affected.add_to_hass(hass)
    healthy.add_to_hass(hass)
    affected.async_start_reauth = MagicMock()
    healthy.async_start_reauth = MagicMock()
    base = CoordinatorHealth(
        available=False,
        generation=1,
        connection_reason=None,
        failure_count=0,
        reconnect_attempts=0,
        reconnect_delay_seconds=0,
        authentication_rejected=False,
        port_range_exhausted=False,
        resource_contract_changed=False,
        unsupported_identity_after_update=False,
        source=UpdateSource.NONE,
        hot_age_seconds=0,
        poll_count=0,
        observe_count=0,
        reconcile_count=0,
        command_count=0,
        latency_under_100ms=0,
        latency_under_500ms=0,
        latency_under_1s=0,
        latency_at_least_1s=0,
    )
    async_sync_runtime_issues(
        hass,
        affected.entry_id,
        replace(base, authentication_rejected=True),
    )
    async_sync_runtime_issues(hass, healthy.entry_id, base)
    async_sync_certificate_issue(
        hass,
        affected.entry_id,
        now + timedelta(days=30),
        now=now,
    )
    async_sync_certificate_issue(
        hass,
        healthy.entry_id,
        now + timedelta(days=120),
        now=now,
    )

    auth_flow = await async_create_fix_flow(hass, "authentication_rejected", None)
    auth_flow.hass = hass
    await auth_flow.async_step_confirm({})
    with patch(
        "custom_components.samsung_ac_windfree.repairs.dt_util.utcnow",
        return_value=now,
    ):
        cert_flow = await async_create_fix_flow(hass, "certificate_expiring", None)
        cert_flow.hass = hass
        await cert_flow.async_step_confirm({})

    assert affected.async_start_reauth.call_count == 2
    healthy.async_start_reauth.assert_not_called()


async def test_disabled_entry_is_purged_and_never_selected_by_fix_flows(
    hass,
) -> None:
    from custom_components.samsung_ac_windfree.coordinator import CoordinatorHealth
    from custom_components.samsung_ac_windfree.models import UpdateSource
    from custom_components.samsung_ac_windfree.repairs import (
        async_create_fix_flow,
        async_sync_certificate_issue,
        async_sync_runtime_issues,
    )

    now = datetime(2026, 7, 24, tzinfo=UTC)
    entry = MockConfigEntry(
        domain=DOMAIN,
        disabled_by=ConfigEntryDisabler.USER,
        data={"not_after": (now + timedelta(days=30)).isoformat()},
    )
    entry.add_to_hass(hass)
    entry.async_start_reauth = MagicMock()
    health = CoordinatorHealth(
        available=False,
        generation=1,
        connection_reason=None,
        failure_count=0,
        reconnect_attempts=0,
        reconnect_delay_seconds=0,
        authentication_rejected=True,
        port_range_exhausted=False,
        resource_contract_changed=False,
        unsupported_identity_after_update=False,
        source=UpdateSource.NONE,
        hot_age_seconds=0,
        poll_count=0,
        observe_count=0,
        reconcile_count=0,
        command_count=0,
        latency_under_100ms=0,
        latency_under_500ms=0,
        latency_under_1s=0,
        latency_at_least_1s=0,
    )
    async_sync_runtime_issues(hass, entry.entry_id, health)
    async_sync_certificate_issue(
        hass,
        entry.entry_id,
        now + timedelta(days=30),
        now=now,
    )

    auth_flow = await async_create_fix_flow(hass, "authentication_rejected", None)
    auth_flow.hass = hass
    auth_result = await auth_flow.async_step_confirm({})
    with patch(
        "custom_components.samsung_ac_windfree.repairs.dt_util.utcnow",
        return_value=now,
    ):
        cert_flow = await async_create_fix_flow(hass, "certificate_expiring", None)
        cert_flow.hass = hass
        cert_result = await cert_flow.async_step_confirm({})

    assert auth_result["type"] is FlowResultType.ABORT
    assert cert_result["type"] is FlowResultType.ABORT
    entry.async_start_reauth.assert_not_called()
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "authentication_rejected") is None
    assert registry.async_get_issue(DOMAIN, "certificate_expiring") is None
