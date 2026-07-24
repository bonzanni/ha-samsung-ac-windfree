"""Fix flow for locally stored WindFree certificate expiry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
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
_REPAIR_STATE_DATA = f"{DOMAIN}_private_repair_state"


class RuntimeHealth(Protocol):
    """Minimal scalar health contract consumed by Repairs."""

    authentication_rejected: bool
    resource_contract_changed: bool
    unsupported_identity_after_update: bool
    port_range_exhausted: bool


@dataclass(frozen=True, slots=True)
class _EntryRepairState:
    authentication_rejected: bool | None = None
    resource_contract_changed: bool | None = None
    unsupported_identity_after_update: bool | None = None
    port_range_exhausted: bool | None = None
    certificate_expiring: bool | None = None


@dataclass(slots=True)
class _RepairStateStore:
    entries: dict[str, _EntryRepairState]
    runtime_pending: set[str]
    certificate_pending: set[str]


def _store(hass: HomeAssistant) -> _RepairStateStore:
    existing = hass.data.get(_REPAIR_STATE_DATA)
    if isinstance(existing, _RepairStateStore):
        return existing
    pending = {
        entry.entry_id
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.disabled_by is None
    }
    created = _RepairStateStore({}, set(pending), set(pending))
    hass.data[_REPAIR_STATE_DATA] = created
    return created


def _relevant_entry_ids(
    hass: HomeAssistant,
    store: _RepairStateStore,
) -> set[str]:
    del hass
    return set(store.entries) | store.runtime_pending | store.certificate_pending


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


def _sync_aggregate_issue(
    hass: HomeAssistant,
    store: _RepairStateStore,
    issue_id: str,
    field: str,
    *,
    fixable: bool,
    severity: ir.IssueSeverity,
) -> None:
    relevant = _relevant_entry_ids(hass, store)
    values = [
        getattr(store.entries.get(entry_id, _EntryRepairState()), field)
        for entry_id in relevant
    ]
    pending = (
        store.certificate_pending
        if field == "certificate_expiring"
        else store.runtime_pending
    )
    if any(value is True for value in values):
        active: bool | None = True
    elif not relevant:
        active = False
    elif relevant & pending or not all(value is False for value in values):
        active = None
    else:
        active = False
    if active is not None:
        _sync_issue(
            hass,
            issue_id,
            active,
            fixable=fixable,
            severity=severity,
        )


def _sync_entry_aggregates(
    hass: HomeAssistant,
    store: _RepairStateStore,
) -> None:
    for issue_id, field, fixable, severity in (
        (
            _AUTHENTICATION_REJECTED,
            "authentication_rejected",
            True,
            ir.IssueSeverity.ERROR,
        ),
        (
            _RESOURCE_CONTRACT_CHANGED,
            "resource_contract_changed",
            False,
            ir.IssueSeverity.ERROR,
        ),
        (
            _UNSUPPORTED_IDENTITY,
            "unsupported_identity_after_update",
            False,
            ir.IssueSeverity.ERROR,
        ),
        (
            _PORT_RANGE_EXHAUSTED,
            "port_range_exhausted",
            False,
            ir.IssueSeverity.ERROR,
        ),
        (
            _CERTIFICATE_EXPIRING,
            "certificate_expiring",
            True,
            ir.IssueSeverity.WARNING,
        ),
    ):
        _sync_aggregate_issue(
            hass,
            store,
            issue_id,
            field,
            fixable=fixable,
            severity=severity,
        )


def async_sync_runtime_issues(
    hass: HomeAssistant,
    entry_id: str,
    health: RuntimeHealth,
) -> None:
    """Synchronize fixed runtime issue transitions without device I/O."""

    store = _store(hass)
    store.runtime_pending.discard(entry_id)
    prior = store.entries.get(entry_id, _EntryRepairState())
    store.entries[entry_id] = replace(
        prior,
        authentication_rejected=bool(health.authentication_rejected),
        resource_contract_changed=bool(health.resource_contract_changed),
        unsupported_identity_after_update=bool(
            health.unsupported_identity_after_update
        ),
        port_range_exhausted=bool(health.port_range_exhausted),
    )
    _sync_entry_aggregates(hass, store)


def async_handle_entry_unload(
    hass: HomeAssistant,
    entry_id: str,
    *,
    enabled: bool,
) -> None:
    """Preserve enabled reload state, or purge a disabled entry."""

    store = _store(hass)
    if enabled:
        store.entries.setdefault(entry_id, _EntryRepairState())
        store.runtime_pending.add(entry_id)
        store.certificate_pending.add(entry_id)
    else:
        store.entries.pop(entry_id, None)
        store.runtime_pending.discard(entry_id)
        store.certificate_pending.discard(entry_id)
    _sync_entry_aggregates(hass, store)


def async_purge_entry_issues(
    hass: HomeAssistant,
    entry_id: str,
) -> None:
    """Permanently remove all private repair state for one entry."""

    store = _store(hass)
    store.entries.pop(entry_id, None)
    store.runtime_pending.discard(entry_id)
    store.certificate_pending.discard(entry_id)
    _sync_entry_aggregates(hass, store)


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
    entry_id: str,
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
    store = _store(hass)
    store.certificate_pending.discard(entry_id)
    prior = store.entries.get(entry_id, _EntryRepairState())
    store.entries[entry_id] = replace(
        prior,
        certificate_expiring=active,
    )
    _sync_entry_aggregates(hass, store)


def _affected_entry_ids(
    hass: HomeAssistant,
    field: str,
) -> set[str]:
    store = _store(hass)
    relevant = _relevant_entry_ids(hass, store)
    return {
        entry_id
        for entry_id in relevant
        if getattr(store.entries.get(entry_id, _EntryRepairState()), field) is True
    }


def _enabled_configured_entries(hass: HomeAssistant) -> list:
    store = _store(hass)
    enabled = []
    changed = False
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.disabled_by is None:
            enabled.append(entry)
            continue
        if (
            entry.entry_id in store.entries
            or entry.entry_id in store.runtime_pending
            or entry.entry_id in store.certificate_pending
        ):
            store.entries.pop(entry.entry_id, None)
            store.runtime_pending.discard(entry.entry_id)
            store.certificate_pending.discard(entry.entry_id)
            changed = True
    if changed:
        _sync_entry_aggregates(hass, store)
    return enabled


def _certificate_expiration(data: Mapping[str, object]) -> datetime | None:
    value = data.get("not_after")
    if not isinstance(value, str):
        return None
    try:
        expires = datetime.fromisoformat(value)
    except ValueError:
        return None
    return expires if expires.tzinfo is not None else None


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
        configured = _enabled_configured_entries(self.hass)
        for entry in configured:
            async_sync_certificate_issue(
                self.hass,
                entry.entry_id,
                _certificate_expiration(entry.data),
                now=now,
            )
        affected = _affected_entry_ids(self.hass, "certificate_expiring")
        entries = [entry for entry in configured if entry.entry_id in affected]
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
        configured = _enabled_configured_entries(self.hass)
        affected = _affected_entry_ids(self.hass, "authentication_rejected")
        entries = [entry for entry in configured if entry.entry_id in affected]
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
