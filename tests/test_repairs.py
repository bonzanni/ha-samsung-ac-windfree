from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.repairs import repairs_flow_manager
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
