"""Fix flow for locally stored WindFree certificate expiry."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import CERT_REPAIR_WINDOW, DOMAIN

_CERTIFICATE_EXPIRING = "certificate_expiring"
_AUTHENTICATION_REJECTED = "authentication_rejected"
_BOOTSTRAP_PIN_CHANGED = "bootstrap_pin_changed"
_BOOTSTRAP_UNAVAILABLE = "bootstrap_unavailable"
_RESOURCE_CONTRACT_CHANGED = "resource_contract_changed"
_UNSUPPORTED_IDENTITY = "unsupported_identity_after_update"
_PORT_RANGE_EXHAUSTED = "port_range_exhausted"


class RuntimeHealth(Protocol):
    """Minimal scalar health contract consumed by Repairs."""

    authentication_rejected: bool
    resource_contract_changed: bool
    unsupported_identity_after_update: bool
    port_range_exhausted: bool


def _sync_issue(
    hass: HomeAssistant,
    issue_id: str,
    active: bool,
    *,
    fixable: bool,
    severity: ir.IssueSeverity,
) -> None:
    if not active:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=fixable,
        is_persistent=True,
        severity=severity,
        translation_key=issue_id,
    )


def async_sync_runtime_issues(
    hass: HomeAssistant,
    health: RuntimeHealth,
) -> None:
    """Synchronize fixed runtime issue transitions without device I/O."""

    _sync_issue(
        hass,
        _AUTHENTICATION_REJECTED,
        health.authentication_rejected,
        fixable=True,
        severity=ir.IssueSeverity.ERROR,
    )
    _sync_issue(
        hass,
        _RESOURCE_CONTRACT_CHANGED,
        health.resource_contract_changed,
        fixable=False,
        severity=ir.IssueSeverity.ERROR,
    )
    _sync_issue(
        hass,
        _UNSUPPORTED_IDENTITY,
        health.unsupported_identity_after_update,
        fixable=False,
        severity=ir.IssueSeverity.ERROR,
    )
    _sync_issue(
        hass,
        _PORT_RANGE_EXHAUSTED,
        health.port_range_exhausted,
        fixable=False,
        severity=ir.IssueSeverity.ERROR,
    )


def async_sync_bootstrap_issue(
    hass: HomeAssistant,
    error_key: str | None,
) -> None:
    """Synchronize mutually exclusive one-time bootstrap failures."""

    _sync_issue(
        hass,
        _BOOTSTRAP_PIN_CHANGED,
        error_key == "bootstrap_pin_mismatch",
        fixable=False,
        severity=ir.IssueSeverity.ERROR,
    )
    _sync_issue(
        hass,
        _BOOTSTRAP_UNAVAILABLE,
        error_key == "bootstrap_unavailable",
        fixable=False,
        severity=ir.IssueSeverity.WARNING,
    )


def async_sync_certificate_issue(
    hass: HomeAssistant,
    expires: datetime | None,
    *,
    now: datetime,
) -> None:
    """Synchronize the 90-day local certificate renewal boundary."""

    active = (
        isinstance(expires, datetime)
        and expires.tzinfo is not None
        and now.tzinfo is not None
        and expires - now <= CERT_REPAIR_WINDOW
    )
    _sync_issue(
        hass,
        _CERTIFICATE_EXPIRING,
        active,
        fixable=True,
        severity=ir.IssueSeverity.WARNING,
    )


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


class AuthenticationRejectedRepairFlow(RepairsFlow):
    """Confirm local credential replacement for configured entries."""

    async def async_step_init(
        self,
        user_input: dict[str, str] | None = None,
    ) -> RepairsFlowResult:
        """Show explicit confirmation before reauthentication."""

        del user_input
        return await self.async_step_confirm()

    async def async_step_confirm(
        self,
        user_input: dict[str, str] | None = None,
    ) -> RepairsFlowResult:
        """Start reauthentication without exposing entry identifiers."""

        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
            )
        entries = list(self.hass.config_entries.async_entries(DOMAIN))
        if not entries:
            ir.async_delete_issue(self.hass, DOMAIN, _AUTHENTICATION_REJECTED)
            return self.async_abort(reason="issue_resolved")
        for entry in entries:
            entry.async_start_reauth(self.hass)
        entries.clear()
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
    if issue_id == _AUTHENTICATION_REJECTED:
        return AuthenticationRejectedRepairFlow()
    return _UnknownRepairFlow()
