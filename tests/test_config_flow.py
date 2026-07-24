from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_ac_windfree.const import DOMAIN
from custom_components.samsung_ac_windfree.models import (
    BootstrapError,
    CapabilityMismatch,
    Credentials,
    DeviceIdentity,
    UnsupportedDevice,
)
from custom_components.samsung_ac_windfree.transport import TransportError

HOST = "ac.example.test"
DEVICE_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def validated_setup(credentials: Credentials):
    from custom_components.samsung_ac_windfree.config_flow import ValidatedSetup

    return ValidatedSetup(
        host=HOST,
        port=49154,
        identity=DeviceIdentity(
            device_id=DEVICE_ID,
            model="AR60F12C1AWNEU",
            device_type="oic.d.airconditioner",
            firmware="TP1X_DA-AC-RAC-01001.1",
            platform="TizenRT 4.0",
        ),
        credentials=credentials,
    )


def _entry_data(validated_setup) -> dict[str, object]:
    credentials = validated_setup.credentials
    identity = validated_setup.identity
    return {
        "host": validated_setup.host,
        "port": validated_setup.port,
        "device_id": identity.device_id,
        "model": identity.model,
        "firmware": identity.firmware,
        "platform": identity.platform,
        "client_key_pem": credentials.client_key_pem,
        "client_chain_pem": credentials.client_chain_pem,
        "not_before": credentials.not_before,
        "not_after": credentials.not_after,
    }


def _aggregate_device_tree(
    resource_representations: dict[str, dict[str, object]],
) -> dict[str, object]:
    return {
        **resource_representations["/device/0"],
        **{
            path: value
            for path, value in resource_representations.items()
            if path not in {"/oic/d", "/oic/p", "/device/0"}
        },
    }


def _config_flow_traceback_locals(error: BaseException) -> str:
    values: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if (
            frame.f_globals.get("__name__")
            == "custom_components.samsung_ac_windfree.config_flow"
        ):
            values.extend(repr(value) for value in frame.f_locals.values())
        traceback = traceback.tb_next
    return "\n".join(values)


async def _finish_progress(hass, result):
    await hass.async_block_till_done()
    progress = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(progress) == 1
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def test_user_flow_requires_only_host(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    assert result["type"] is FlowResultType.FORM
    assert {marker.schema for marker in result["data_schema"].schema} == {"host"}


async def test_success_uses_progress_then_creates_secret_entry(
    hass, validated_setup
) -> None:
    validate = AsyncMock(return_value=validated_setup)
    with (
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_validate_setup",
            validate,
        ),
        patch(
            "custom_components.samsung_ac_windfree.async_setup_entry",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": HOST},
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        result = await _finish_progress(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == _entry_data(validated_setup)
    assert result["result"].unique_id == DEVICE_ID
    assert set(validate.call_args.args) == {hass, HOST}


@pytest.mark.parametrize(
    ("error", "error_key"),
    [
        (BootstrapError("bootstrap_unavailable: unavailable"), "bootstrap_unavailable"),
        (BootstrapError("bootstrap_pin_mismatch: changed"), "bootstrap_pin_mismatch"),
        (BootstrapError("invalid_clock: invalid"), "invalid_clock"),
        (TransportError("transport_discovery_failed"), "cannot_connect"),
        (UnsupportedDevice("unsupported_device"), "unsupported_device"),
        (CapabilityMismatch("capability_mismatch"), "capability_mismatch"),
        (TimeoutError(), "setup_timeout"),
    ],
)
async def test_flow_maps_only_sanitized_validation_errors(
    hass, error, error_key
) -> None:
    validate = AsyncMock(side_effect=error)
    with patch(
        "custom_components.samsung_ac_windfree.config_flow.async_validate_setup",
        validate,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": HOST},
        )
        result = await _finish_progress(hass, result)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error_key}
    assert HOST not in repr(result["errors"])


async def test_bootstrap_flow_issue_transitions_and_success_recovery(
    hass,
    validated_setup,
) -> None:
    registry = ir.async_get(hass)
    validate = AsyncMock(
        side_effect=[
            BootstrapError("bootstrap_pin_mismatch: changed"),
            BootstrapError("bootstrap_unavailable: unavailable"),
            validated_setup,
        ]
    )

    with (
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_validate_setup",
            validate,
        ),
        patch(
            "custom_components.samsung_ac_windfree.async_setup_entry",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": HOST},
        )
        result = await _finish_progress(hass, result)
        assert registry.async_get_issue(DOMAIN, "bootstrap_pin_changed") is not None
        assert registry.async_get_issue(DOMAIN, "bootstrap_unavailable") is None
        hass.config_entries.flow.async_abort(result["flow_id"])

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": HOST},
        )
        result = await _finish_progress(hass, result)
        assert registry.async_get_issue(DOMAIN, "bootstrap_pin_changed") is None
        assert registry.async_get_issue(DOMAIN, "bootstrap_unavailable") is not None
        hass.config_entries.flow.async_abort(result["flow_id"])

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": HOST},
        )
        await _finish_progress(hass, result)

    assert registry.async_get_issue(DOMAIN, "bootstrap_pin_changed") is None
    assert registry.async_get_issue(DOMAIN, "bootstrap_unavailable") is None


@pytest.mark.parametrize(
    ("bootstrap_error", "expected_issue"),
    [
        (TimeoutError(), "bootstrap_unavailable"),
        (RuntimeError("dependency unavailable"), "bootstrap_unavailable"),
        (
            BootstrapError("bootstrap_pin_mismatch: changed"),
            "bootstrap_pin_changed",
        ),
    ],
)
async def test_actual_bootstrap_failures_update_repairs(
    hass,
    bootstrap_error,
    expected_issue,
) -> None:
    registry = ir.async_get(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        "bootstrap_pin_changed",
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="bootstrap_pin_changed",
    )
    with (
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_resolve_host",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow."
            "async_bootstrap_credentials",
            new=AsyncMock(side_effect=bootstrap_error),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": HOST},
        )
        await _finish_progress(hass, result)

    assert registry.async_get_issue(DOMAIN, expected_issue) is not None
    other = (
        "bootstrap_unavailable"
        if expected_issue == "bootstrap_pin_changed"
        else "bootstrap_pin_changed"
    )
    assert registry.async_get_issue(DOMAIN, other) is None


@pytest.mark.parametrize("later_failure", ["sweep", "read"])
async def test_verified_bootstrap_clears_issues_before_later_failure(
    hass,
    credentials,
    later_failure,
) -> None:
    registry = ir.async_get(hass)
    for issue_id in ("bootstrap_pin_changed", "bootstrap_unavailable"):
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key=issue_id,
        )
    transport = AsyncMock()
    if later_failure == "read":
        transport.async_get.side_effect = TimeoutError
        discover = AsyncMock(return_value=(49154, transport))
    else:
        discover = AsyncMock(side_effect=TimeoutError)

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
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": HOST},
        )
        await _finish_progress(hass, result)

    assert registry.async_get_issue(DOMAIN, "bootstrap_pin_changed") is None
    assert registry.async_get_issue(DOMAIN, "bootstrap_unavailable") is None
    if later_failure == "read":
        transport.async_close.assert_awaited_once_with()


async def test_failure_before_bootstrap_preserves_existing_issue(hass) -> None:
    registry = ir.async_get(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        "bootstrap_pin_changed",
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="bootstrap_pin_changed",
    )
    bootstrap = AsyncMock()

    with (
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_resolve_host",
            new=AsyncMock(side_effect=OSError("dns unavailable")),
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow."
            "async_bootstrap_credentials",
            new=bootstrap,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": HOST},
        )
        await _finish_progress(hass, result)

    bootstrap.assert_not_awaited()
    assert registry.async_get_issue(DOMAIN, "bootstrap_pin_changed") is not None
    assert registry.async_get_issue(DOMAIN, "bootstrap_unavailable") is None


async def test_overall_timeout_before_bootstrap_preserves_issue(hass) -> None:
    registry = ir.async_get(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        "bootstrap_unavailable",
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="bootstrap_unavailable",
    )
    bootstrap = AsyncMock()

    async def blocked_resolve(*_args):
        await asyncio.Event().wait()

    with (
        patch(
            "custom_components.samsung_ac_windfree.config_flow.SETUP_TIMEOUT",
            0.001,
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_resolve_host",
            blocked_resolve,
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow."
            "async_bootstrap_credentials",
            new=bootstrap,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": HOST},
        )
        await _finish_progress(hass, result)

    bootstrap.assert_not_awaited()
    assert registry.async_get_issue(DOMAIN, "bootstrap_unavailable") is not None


async def test_bootstrap_cancellation_preserves_existing_issue(hass) -> None:
    registry = ir.async_get(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        "bootstrap_pin_changed",
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="bootstrap_pin_changed",
    )
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def blocked_bootstrap(_hass):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    with (
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_resolve_host",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow."
            "async_bootstrap_credentials",
            blocked_bootstrap,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": HOST},
        )
        await started.wait()
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()

    assert cleaned.is_set()
    assert registry.async_get_issue(DOMAIN, "bootstrap_pin_changed") is not None
    assert registry.async_get_issue(DOMAIN, "bootstrap_unavailable") is None


async def test_duplicate_device_aborts_after_local_validation(
    hass, validated_setup
) -> None:
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data=_entry_data(validated_setup),
    )
    existing.add_to_hass(hass)

    with patch(
        "custom_components.samsung_ac_windfree.config_flow.async_validate_setup",
        new=AsyncMock(return_value=validated_setup),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": HOST},
        )
        result = await _finish_progress(hass, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_flow_removal_cancels_progress_and_runs_validation_cleanup(hass) -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def validate(_hass, _host):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    with patch(
        "custom_components.samsung_ac_windfree.config_flow.async_validate_setup",
        validate,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": HOST},
        )
        await started.wait()
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()

    assert cleaned.is_set()
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


async def test_reconfigure_requires_same_identity_and_preserves_entry(
    hass, validated_setup
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data=_entry_data(validated_setup),
    )
    entry.add_to_hass(hass)
    replacement = type(validated_setup)(
        host="replacement.example.test",
        port=49155,
        identity=DeviceIdentity(
            device_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            model=validated_setup.identity.model,
            device_type=validated_setup.identity.device_type,
            firmware=validated_setup.identity.firmware,
            platform=validated_setup.identity.platform,
        ),
        credentials=validated_setup.credentials,
    )
    original = dict(entry.data)

    with patch(
        "custom_components.samsung_ac_windfree.config_flow.async_validate_setup",
        new=AsyncMock(return_value=replacement),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        assert {marker.schema for marker in result["data_schema"].schema} == {"host"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": replacement.host}
        )
        result = await _finish_progress(hass, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert dict(entry.data) == original


async def test_reconfigure_atomically_updates_same_device(
    hass, validated_setup
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data=_entry_data(validated_setup),
    )
    entry.add_to_hass(hass)
    replacement = type(validated_setup)(
        host="replacement.example.test",
        port=49155,
        identity=validated_setup.identity,
        credentials=validated_setup.credentials,
    )

    with (
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_validate_setup",
            new=AsyncMock(return_value=replacement),
        ),
        patch.object(
            hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": replacement.host}
        )
        result = await _finish_progress(hass, result)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(entry.data) == _entry_data(replacement)


async def test_failed_reauth_preserves_old_credentials(hass, validated_setup) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data=_entry_data(validated_setup),
    )
    entry.add_to_hass(hass)
    original = dict(entry.data)

    with patch(
        "custom_components.samsung_ac_windfree.config_flow.async_validate_setup",
        new=AsyncMock(side_effect=BootstrapError("bootstrap_unavailable: unavailable")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["data_schema"].schema == {}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _finish_progress(hass, result)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "bootstrap_unavailable"}
    assert dict(entry.data) == original


async def test_successful_reauth_replaces_credentials_once(
    hass, validated_setup
) -> None:
    old = Credentials(
        "old-key",
        "old-chain",
        "2020-01-01T00:00:00+00:00",
        "2030-01-01T00:00:00+00:00",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data=_entry_data(
            type(validated_setup)(
                host=HOST,
                port=49154,
                identity=validated_setup.identity,
                credentials=old,
            )
        ),
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_validate_setup",
            new=AsyncMock(return_value=validated_setup),
        ),
        patch.object(
            hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _finish_progress(hass, result)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert dict(entry.data) == _entry_data(validated_setup)


async def test_validate_setup_budgets_phases_and_reuses_swept_transport(
    hass, credentials, resource_representations
) -> None:
    from custom_components.samsung_ac_windfree.config_flow import (
        ValidatedSetup,
        async_validate_setup,
    )

    transport = AsyncMock()
    device_tree = _aggregate_device_tree(resource_representations)
    transport.async_get.side_effect = lambda path: (
        device_tree if path == "/device/0" else resource_representations[path]
    )
    resolve = AsyncMock()
    bootstrap = AsyncMock(return_value=credentials)
    discover = AsyncMock(return_value=(49154, transport))

    with (
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_resolve_host",
            resolve,
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_bootstrap_credentials",
            bootstrap,
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_discover_transport",
            discover,
        ),
    ):
        result = await async_validate_setup(hass, HOST)

    assert isinstance(result, ValidatedSetup)
    assert result.port == 49154
    assert result.identity.device_id == "00000000-0000-4000-8000-000000000001"
    assert [call.args[0] for call in transport.async_get.await_args_list] == [
        "/oic/d",
        "/oic/p",
        "/device/0",
    ]
    transport.async_close.assert_awaited_once_with()
    discover.assert_awaited_once_with(hass, HOST, credentials)


def test_validation_phase_budgets_fit_inside_overall_timeout() -> None:
    from custom_components.samsung_ac_windfree.config_flow import (
        BOOTSTRAP_TIMEOUT,
        HOST_RESOLVE_TIMEOUT,
        IDENTITY_READ_TIMEOUT,
        SETUP_TIMEOUT,
        SWEEP_TIMEOUT,
    )

    assert (
        HOST_RESOLVE_TIMEOUT + BOOTSTRAP_TIMEOUT + SWEEP_TIMEOUT + IDENTITY_READ_TIMEOUT
        <= SETUP_TIMEOUT
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("older_model", "unsupported_device"),
        ("missing_required_resource", "capability_mismatch"),
    ],
)
async def test_validate_setup_rejects_non_exact_identity_or_contract(
    hass,
    credentials,
    resource_representations,
    mutation,
    expected,
) -> None:
    from custom_components.samsung_ac_windfree.config_flow import (
        SetupValidationError,
        async_validate_setup,
    )

    resources = copy.deepcopy(resource_representations)
    device_tree = _aggregate_device_tree(resources)
    if mutation == "older_model":
        resources["/oic/d"]["mnmo"] = "AR12-OLDER"
    else:
        device_tree.pop("/power/vs/0")
    transport = AsyncMock()
    transport.async_get.side_effect = lambda path: (
        device_tree if path == "/device/0" else resources[path]
    )

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
            new=AsyncMock(return_value=(49154, transport)),
        ),
    ):
        with pytest.raises(SetupValidationError, match=f"^{expected}$"):
            await async_validate_setup(hass, HOST)

    transport.async_close.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("async_resolve_host", "dns_timeout"),
        ("async_bootstrap_credentials", "fetch_timeout"),
        ("async_discover_transport", "sweep_timeout"),
    ],
)
async def test_validate_setup_has_independent_phase_timeouts(
    hass, credentials, phase, expected
) -> None:
    from custom_components.samsung_ac_windfree.config_flow import (
        SetupValidationError,
        async_validate_setup,
    )

    async def blocked(*_args):
        await asyncio.Event().wait()

    with (
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_resolve_host",
            blocked if phase == "async_resolve_host" else AsyncMock(),
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow."
            "async_bootstrap_credentials",
            blocked
            if phase == "async_bootstrap_credentials"
            else AsyncMock(return_value=credentials),
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow."
            "async_discover_transport",
            blocked
            if phase == "async_discover_transport"
            else AsyncMock(side_effect=AssertionError("phase order violated")),
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow."
            + {
                "async_resolve_host": "HOST_RESOLVE_TIMEOUT",
                "async_bootstrap_credentials": "BOOTSTRAP_TIMEOUT",
                "async_discover_transport": "SWEEP_TIMEOUT",
            }[phase],
            0.001,
        ),
    ):
        with pytest.raises(SetupValidationError, match=f"^{expected}$"):
            await async_validate_setup(hass, HOST)


async def test_validate_setup_read_timeout_closes_swept_transport(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree.config_flow import (
        SetupValidationError,
        async_validate_setup,
    )

    transport = AsyncMock()

    async def blocked_read(_path):
        await asyncio.Event().wait()

    transport.async_get.side_effect = blocked_read
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
            new=AsyncMock(return_value=(49154, transport)),
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow.IDENTITY_READ_TIMEOUT",
            0.001,
        ),
    ):
        with pytest.raises(SetupValidationError, match=r"^read_timeout$"):
            await async_validate_setup(hass, HOST)

    transport.async_close.assert_awaited_once_with()


async def test_validate_setup_overall_timeout_is_independent(hass) -> None:
    from custom_components.samsung_ac_windfree.config_flow import (
        SetupValidationError,
        async_validate_setup,
    )

    async def blocked(*_args):
        await asyncio.Event().wait()

    with (
        patch(
            "custom_components.samsung_ac_windfree.config_flow.async_resolve_host",
            blocked,
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow.HOST_RESOLVE_TIMEOUT",
            10,
        ),
        patch(
            "custom_components.samsung_ac_windfree.config_flow.SETUP_TIMEOUT",
            0.001,
        ),
    ):
        with pytest.raises(SetupValidationError, match=r"^setup_timeout$"):
            await async_validate_setup(hass, HOST)


async def test_validate_setup_cancellation_closes_transport(hass, credentials) -> None:
    from custom_components.samsung_ac_windfree.config_flow import async_validate_setup

    transport = AsyncMock()
    reading = asyncio.Event()

    async def read(_path):
        reading.set()
        await asyncio.Event().wait()

    transport.async_get.side_effect = read
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
            new=AsyncMock(return_value=(49154, transport)),
        ),
    ):
        task = hass.async_create_task(async_validate_setup(hass, HOST))
        await reading.wait()
        task.cancel("flow_removed")
        with pytest.raises(asyncio.CancelledError, match="flow_removed"):
            await task

    transport.async_close.assert_awaited_once_with()


async def test_public_validate_cancellation_scrubs_host_and_internal_context(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree.config_flow import async_validate_setup

    started = asyncio.Event()

    async def blocked(_hass, _host):
        started.set()
        await asyncio.Event().wait()

    with patch(
        "custom_components.samsung_ac_windfree.config_flow."
        "_async_validate_setup_outcome",
        blocked,
    ):
        task = hass.async_create_task(async_validate_setup(hass, HOST))
        await started.wait()
        task.cancel("public_validation_cancelled")
        with pytest.raises(
            asyncio.CancelledError, match="public_validation_cancelled"
        ) as caught:
            await task

    traceback_locals = _config_flow_traceback_locals(caught.value)
    assert HOST not in traceback_locals
    assert credentials.client_key_pem not in traceback_locals
    assert caught.value.__context__ is None


async def test_validate_setup_close_failure_is_sanitized(
    hass, credentials, resource_representations
) -> None:
    from custom_components.samsung_ac_windfree.config_flow import (
        SetupValidationError,
        async_validate_setup,
    )

    transport = AsyncMock()
    device_tree = _aggregate_device_tree(resource_representations)
    transport.async_get.side_effect = lambda path: (
        device_tree if path == "/device/0" else resource_representations[path]
    )
    transport.async_close.side_effect = TransportError("transport_close_failed")

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
            new=AsyncMock(return_value=(49154, transport)),
        ),
    ):
        with pytest.raises(SetupValidationError, match=r"^cannot_connect$") as caught:
            await async_validate_setup(hass, HOST)

    assert HOST not in repr(caught.value)
    assert credentials.client_key_pem not in repr(caught.value)


async def test_validate_setup_cancellation_during_close_finishes_cleanup(
    hass, credentials, resource_representations
) -> None:
    from custom_components.samsung_ac_windfree.config_flow import async_validate_setup

    transport = AsyncMock()
    close_started = asyncio.Event()
    close_release = asyncio.Event()
    close_finished = asyncio.Event()
    device_tree = _aggregate_device_tree(resource_representations)
    transport.async_get.side_effect = lambda path: (
        device_tree if path == "/device/0" else resource_representations[path]
    )

    async def close():
        close_started.set()
        try:
            await close_release.wait()
        finally:
            close_finished.set()

    transport.async_close.side_effect = close
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
            new=AsyncMock(return_value=(49154, transport)),
        ),
    ):
        task = hass.async_create_task(async_validate_setup(hass, HOST))
        await close_started.wait()
        task.cancel("cancel_during_close")
        await asyncio.sleep(0)
        assert not close_finished.is_set()
        close_release.set()
        with pytest.raises(asyncio.CancelledError, match="cancel_during_close"):
            await task

    assert close_finished.is_set()


def test_public_validation_error_excludes_host_and_secrets_from_repr_and_traceback(
    hass, credentials
) -> None:
    from custom_components.samsung_ac_windfree.config_flow import SetupValidationError

    error = SetupValidationError("cannot_connect")
    assert HOST not in repr(error)
    assert credentials.client_key_pem not in repr(error)
    assert credentials.client_chain_pem not in repr(error)
    assert error.args == ("cannot_connect",)
