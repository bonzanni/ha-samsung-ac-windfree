from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from homeassistant.data_entry_flow import FlowResultType
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


async def test_unknown_repair_aborts_without_action(hass) -> None:
    from custom_components.samsung_ac_windfree.repairs import async_create_fix_flow

    flow = await async_create_fix_flow(hass, "unknown", None)
    flow.hass = hass
    result = await flow.async_step_init()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown_issue"
