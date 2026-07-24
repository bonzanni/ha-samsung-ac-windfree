"""Fix flow for locally stored WindFree certificate expiry."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import CERT_REPAIR_WINDOW, DOMAIN

_CERTIFICATE_EXPIRING = "certificate_expiring"


def _is_expiring(data: Mapping[str, object], now: datetime) -> bool:
    value = data.get("not_after")
    if not isinstance(value, str):
        return False
    try:
        expires = datetime.fromisoformat(value)
    except ValueError:
        return False
    return expires.tzinfo is not None and expires - now <= CERT_REPAIR_WINDOW


class _UnknownRepairFlow(RepairsFlow):
    async def async_step_init(
        self,
        user_input: dict[str, str] | None = None,
    ) -> RepairsFlowResult:
        """Abort an unsupported or stale issue."""

        del user_input
        return self.async_abort(reason="unknown_issue")


class CertificateExpiryRepairFlow(RepairsFlow):
    """Confirm renewal without placing entry identifiers in the issue."""

    async def async_step_init(
        self,
        user_input: dict[str, str] | None = None,
    ) -> RepairsFlowResult:
        """Show the explicit renewal confirmation."""

        del user_input
        return await self.async_step_confirm()

    async def async_step_confirm(
        self,
        user_input: dict[str, str] | None = None,
    ) -> RepairsFlowResult:
        """Start reauth for every entry represented by the private issue."""

        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
            )
        now = dt_util.utcnow()
        entries = [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if _is_expiring(entry.data, now)
        ]
        if not entries:
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                _CERTIFICATE_EXPIRING,
            )
            return self.async_abort(reason="issue_resolved")
        for entry in entries:
            entry.async_start_reauth(self.hass)
        entries = []
        del entries
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create only the certificate-renewal fix flow."""

    del hass, data
    if issue_id == _CERTIFICATE_EXPIRING:
        return CertificateExpiryRepairFlow()
    return _UnknownRepairFlow()
