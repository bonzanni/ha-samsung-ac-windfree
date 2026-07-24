from __future__ import annotations

import copy
import json
import math
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from custom_components.samsung_ac_windfree.device import (
    AUTO_CLEAN_PATH,
    DISPLAY_LIGHT_PATH,
    FAN_PATH,
    HVAC_MODE_PATH,
    POWER_PATH,
    PRESET_PATH,
    SWING_PATH,
    TEMPERATURE_PATH,
    CommandKind,
    DeviceCommand,
    build_command,
    parse_device_state,
    parse_humidity,
    parse_identity,
    validate_contract,
    verify_command,
)
from custom_components.samsung_ac_windfree.models import (
    CapabilityMismatch,
    FanMode,
    HvacMode,
    PresetMode,
    SwingMode,
    UnsupportedDevice,
    WindFreeData,
)


def fixture(name: str) -> dict[str, object]:
    return json.loads(Path(f"tests/fixtures/{name}").read_text())


def identity_parts() -> tuple[dict[str, object], ...]:
    payload = fixture("device_identity.json")
    return payload["oic_d"], payload["oic_p"], payload["device_0"]  # type: ignore[return-value]


def state_resources() -> dict[str, dict[str, object]]:
    return fixture("device_state.json")  # type: ignore[return-value]


def compatibility() -> dict[str, object]:
    return fixture("mode_compatibility.json")


def test_identity_parses_exact_supported_contract() -> None:
    identity = parse_identity(*identity_parts())

    assert identity.device_id == "00000000-0000-4000-8000-000000000001"
    assert identity.model == "AR60F12C1AWNEU"
    assert identity.device_type == "oic.d.airconditioner"
    assert identity.firmware == "TP1X_DA-AC-RAC-01001_001"
    assert identity.platform == "TizenRT 4.0"


@pytest.mark.parametrize(
    ("part", "field", "replacement"),
    [
        ("oic_d", "mnmo", "AR60F12C1AWOTHER"),
        ("oic_d", "rt", ["oic.d.light"]),
        ("oic_p", "mnpv", "TizenRT 3.0"),
        (
            "device_0",
            "/information/vs/0",
            {"x.com.samsung.da.description": "OTHER_001"},
        ),
    ],
)
def test_identity_rejects_each_exact_product_gate(
    part: str,
    field: str,
    replacement: object,
) -> None:
    payload = fixture("device_identity.json")
    payload[part][field] = replacement  # type: ignore[index]

    with pytest.raises(UnsupportedDevice, match="unsupported_device"):
        parse_identity(payload["oic_d"], payload["oic_p"], payload["device_0"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("part", "field", "replacement"),
    [
        ("oic_d", "di", ""),
        ("oic_d", "mnmo", True),
        ("oic_d", "rt", "oic.d.airconditioner"),
        ("oic_p", "mnpv", None),
        ("device_0", "/information/vs/0", []),
    ],
)
def test_identity_rejects_malformed_required_fields(
    part: str,
    field: str,
    replacement: object,
) -> None:
    payload = fixture("device_identity.json")
    payload[part][field] = replacement  # type: ignore[index]

    with pytest.raises(UnsupportedDevice, match="unsupported_device"):
        parse_identity(payload["oic_d"], payload["oic_p"], payload["device_0"])  # type: ignore[arg-type]


def test_identity_ignores_unknown_fields_without_copying_them() -> None:
    oic_d, oic_p, device_0 = identity_parts()
    oic_d["secret-looking-unknown"] = {"nested": "ignored"}

    assert parse_identity(oic_d, oic_p, device_0) == parse_identity(*identity_parts())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("36", 36),
        (36, 36),
        ("1", 1),
        (100, 100),
        ("0", None),
        (0, None),
        ("101", None),
        (-1, None),
        ("36.0", None),
        ("not-a-number", None),
        (True, None),
        (None, None),
    ],
)
def test_humidity_is_direct_integer_percentage_with_zero_sentinel(
    raw: object,
    expected: int | None,
) -> None:
    assert parse_humidity(raw) == expected


def test_humidity_rejects_oversized_decimal_string_without_raising() -> None:
    assert parse_humidity("9" * 5000) is None


def test_device_state_parses_every_live_mapping() -> None:
    identity = parse_identity(*identity_parts())
    previous = replace(WindFreeData.empty(), identity=identity, generation=7)

    parsed = parse_device_state(state_resources(), previous)

    assert parsed is not previous
    assert parsed.identity is identity
    assert parsed.generation == 7
    assert parsed.climate.power is False
    assert parsed.climate.mode is HvacMode.COOL
    assert parsed.climate.current_temperature == 26.0
    assert parsed.climate.target_temperature == 26.0
    assert parsed.climate.humidity == 36
    assert parsed.climate.fan_mode is FanMode.AUTO
    assert parsed.climate.swing_mode is SwingMode.FIXED
    assert parsed.climate.preset_mode is PresetMode.NONE
    assert parsed.display_light is True
    assert parsed.auto_clean is True
    assert parsed.filter.used == 42
    assert parsed.filter.capacity == 500
    assert parsed.filter.status == "normal"
    assert parsed.filter.attention is False
    assert parsed.energy.cumulative_kwh == 12.345
    assert parsed.alarms.problem is False
    assert parsed.alarms.active_code is None
    assert parsed.alarms.filter_alarm is False
    assert parsed.current_limit_enabled is False
    assert parsed.current_limit_level == 3


def test_device_state_parses_auto_alias_as_auto() -> None:
    resources = state_resources()
    resources[HVAC_MODE_PATH]["x.com.samsung.da.modes"] = ["AI Auto"]

    parsed = parse_device_state(resources, WindFreeData.empty())

    assert parsed.climate.mode is HvacMode.AUTO


@pytest.mark.parametrize(
    "raw",
    ["0", 0, "1", 1, "1000.5", 1000.5, "1e6"],
)
def test_energy_accepts_finite_non_negative_wh_and_converts_to_kwh(
    raw: object,
) -> None:
    resources = state_resources()
    resources["/energy/consumption/vs/0"]["x.com.samsung.da.cumulativePower"] = raw

    parsed = parse_device_state(resources, WindFreeData.empty())

    assert parsed.energy.cumulative_kwh == float(raw) / 1000


@pytest.mark.parametrize(
    "raw",
    [
        -1,
        "-0.1",
        math.inf,
        -math.inf,
        math.nan,
        "nan",
        "inf",
        True,
        10**1000,
        object(),
    ],
)
def test_energy_rejects_negative_non_finite_boolean_and_malformed_wh(
    raw: object,
) -> None:
    resources = state_resources()
    resources["/energy/consumption/vs/0"]["x.com.samsung.da.cumulativePower"] = raw

    parsed = parse_device_state(resources, WindFreeData.empty())

    assert parsed.energy.cumulative_kwh is None


def test_energy_requires_wh_total_contract() -> None:
    resources = state_resources()
    energy = resources["/energy/consumption/vs/0"]
    energy["x.com.samsung.da.cumulativeUnit"] = "kWh"

    assert (
        parse_device_state(resources, WindFreeData.empty()).energy.cumulative_kwh
        is None
    )


@pytest.mark.parametrize("capacity", [0, "0", -1, "-1", True, "bad"])
def test_filter_zero_or_invalid_capacity_is_guarded(capacity: object) -> None:
    resources = state_resources()
    resources["/filter/airdustfilter/vs/0"]["x.com.samsung.da.filterCapacity"] = (
        capacity
    )

    parsed = parse_device_state(resources, WindFreeData.empty())

    assert parsed.filter.capacity is None
    assert parsed.filter.used is None


@pytest.mark.parametrize("status", ["wash", "replace", "WASH"])
def test_filter_status_marks_attention(status: str) -> None:
    resources = state_resources()
    resources["/filter/airdustfilter/vs/0"]["x.com.samsung.da.filterStatus"] = status

    assert parse_device_state(resources, WindFreeData.empty()).filter.attention


def test_alarm_parser_distinguishes_filter_and_device_alarms() -> None:
    resources = state_resources()
    resources["/alarms/vs/0"]["x.com.samsung.da.items"] = [
        {
            "x.com.samsung.da.alarmType": "Filter",
            "x.com.samsung.da.code": "Filter_Wash",
            "x.com.samsung.da.state": "Active",
            "opaque": "ignored",
        },
        {
            "x.com.samsung.da.alarmType": "Device",
            "x.com.samsung.da.code": "ErrorCode_E101",
            "x.com.samsung.da.state": "Active",
        },
        {
            "x.com.samsung.da.alarmType": "Device",
            "x.com.samsung.da.code": "ErrorCode_OLD",
            "x.com.samsung.da.state": "Deleted",
        },
    ]

    parsed = parse_device_state(resources, WindFreeData.empty())

    assert parsed.alarms.filter_alarm is True
    assert parsed.alarms.problem is True
    assert parsed.alarms.active_code == "ErrorCode_E101"
    assert parsed.filter.attention is True


def test_filter_alarm_does_not_become_device_problem() -> None:
    resources = state_resources()
    resources["/alarms/vs/0"]["x.com.samsung.da.items"] = [
        {
            "x.com.samsung.da.alarmType": "Filter",
            "x.com.samsung.da.code": "Filter_Replace",
            "x.com.samsung.da.state": "Active",
        }
    ]

    parsed = parse_device_state(resources, WindFreeData.empty())

    assert parsed.alarms.filter_alarm is True
    assert parsed.alarms.problem is False
    assert parsed.alarms.active_code == "Filter_Replace"


@pytest.mark.parametrize(
    ("status", "level", "enabled", "expected_level"),
    [
        ("On", "3", True, 3),
        ("Off", "9", False, 9),
        ("Unknown", "12", None, 12),
        ("On", "-1", True, None),
        ("On", True, True, None),
        ("On", "3.0", True, None),
    ],
)
def test_current_limit_is_read_only_opaque_state_without_units(
    status: object,
    level: object,
    enabled: bool | None,
    expected_level: int | None,
) -> None:
    resources = state_resources()
    resource = resources["/electriccurrent/vs/0"]
    resource["x.com.samsung.da.settingStatus"] = status
    resource["x.com.samsung.da.level"] = level

    parsed = parse_device_state(resources, WindFreeData.empty())

    assert parsed.current_limit_enabled is enabled
    assert parsed.current_limit_level == expected_level


def test_state_parser_rejects_oversized_decimal_fields_without_raising() -> None:
    resources = state_resources()
    oversized = "9" * 5000
    resources["/filter/airdustfilter/vs/0"].update(
        {
            "x.com.samsung.da.filterCapacity": oversized,
            "x.com.samsung.da.filterUsage": oversized,
        }
    )
    resources["/electriccurrent/vs/0"]["x.com.samsung.da.level"] = oversized

    parsed = parse_device_state(resources, WindFreeData.empty())

    assert parsed.filter.capacity is None
    assert parsed.filter.used is None
    assert parsed.current_limit_level is None


def test_malformed_optional_resources_degrade_without_raising() -> None:
    resources = {path: {} for path in state_resources()}
    previous = replace(
        WindFreeData.empty(),
        climate=replace(
            WindFreeData.empty().climate,
            mode=HvacMode.HEAT,
            target_temperature=23.0,
        ),
    )

    parsed = parse_device_state(resources, previous)

    assert parsed.climate.mode is HvacMode.HEAT
    assert parsed.climate.target_temperature == 23.0
    assert parsed.climate.current_temperature is None
    assert parsed.climate.humidity is None
    assert parsed.filter.used is None
    assert parsed.energy.cumulative_kwh is None
    assert parsed.current_limit_level is None


@pytest.mark.parametrize(
    "alarm_resource",
    [
        {},
        {"x.com.samsung.da.items": "bad"},
        {"x.com.samsung.da.items": [{}]},
    ],
)
def test_malformed_alarm_resource_preserves_previous_alarm_state(
    alarm_resource: dict[str, object],
) -> None:
    resources = state_resources()
    resources["/alarms/vs/0"] = alarm_resource
    previous = replace(
        WindFreeData.empty(),
        alarms=replace(
            WindFreeData.empty().alarms,
            problem=True,
            active_code="previous-code",
            filter_alarm=True,
        ),
    )

    parsed = parse_device_state(resources, previous)

    assert parsed.alarms is previous.alarms
    assert parsed.filter.attention is True


def test_missing_alarm_resource_preserves_previous_alarm_state() -> None:
    resources = state_resources()
    del resources["/alarms/vs/0"]
    previous = replace(
        WindFreeData.empty(),
        alarms=replace(
            WindFreeData.empty().alarms,
            problem=True,
            active_code="previous-code",
        ),
    )

    assert parse_device_state(resources, previous).alarms is previous.alarms


def test_valid_empty_alarm_items_clear_previous_alarm_state() -> None:
    resources = state_resources()
    resources["/alarms/vs/0"]["x.com.samsung.da.items"] = []
    previous = replace(
        WindFreeData.empty(),
        alarms=replace(
            WindFreeData.empty().alarms,
            problem=True,
            active_code="previous-code",
            filter_alarm=True,
        ),
    )

    assert parse_device_state(resources, previous).alarms == type(previous.alarms)()


def test_validate_contract_accepts_exact_live_safe_write_contract() -> None:
    identity = parse_identity(*identity_parts())

    contract = validate_contract(identity, state_resources(), compatibility())

    assert contract.writable_paths == frozenset(
        {
            POWER_PATH,
            HVAC_MODE_PATH,
            TEMPERATURE_PATH,
            FAN_PATH,
            SWING_PATH,
            PRESET_PATH,
            DISPLAY_LIGHT_PATH,
            AUTO_CLEAN_PATH,
        }
    )
    assert contract.mode_controls == {
        HvacMode.AUTO: frozenset(),
        HvacMode.COOL: frozenset({"temperature", "fan", "swing", "preset"}),
        HvacMode.DRY: frozenset(),
        HvacMode.FAN: frozenset(),
        HvacMode.HEAT: frozenset(),
    }


@pytest.mark.parametrize(
    ("path", "field"),
    [
        (POWER_PATH, "x.com.samsung.da.power"),
        (HVAC_MODE_PATH, "x.com.samsung.da.supportedModes"),
        (TEMPERATURE_PATH, "x.com.samsung.da.items"),
        (FAN_PATH, "x.com.samsung.da.supportedModes"),
        (SWING_PATH, "x.com.samsung.da.supportedModes"),
        (PRESET_PATH, "x.com.samsung.da.supportedModes"),
        (DISPLAY_LIGHT_PATH, "supportedModes"),
        (AUTO_CLEAN_PATH, "x.com.samsung.da.supportedSettingStatus"),
    ],
)
def test_validate_contract_fails_closed_on_safe_write_resource_drift(
    path: str,
    field: str,
) -> None:
    resources = state_resources()
    del resources[path][field]

    with pytest.raises(CapabilityMismatch, match="capability_mismatch"):
        validate_contract(parse_identity(*identity_parts()), resources, compatibility())


@pytest.mark.parametrize(
    ("path", "field", "malformed"),
    [
        (HVAC_MODE_PATH, "x.com.samsung.da.modes", None),
        (HVAC_MODE_PATH, "x.com.samsung.da.modes", "Cool"),
        (HVAC_MODE_PATH, "x.com.samsung.da.modes", ["Unknown"]),
        (FAN_PATH, "x.com.samsung.da.modes", None),
        (FAN_PATH, "x.com.samsung.da.modes", ["0"]),
        (FAN_PATH, "x.com.samsung.da.modes", "5"),
        (SWING_PATH, "x.com.samsung.da.modes", None),
        (SWING_PATH, "x.com.samsung.da.modes", ["Fix"]),
        (SWING_PATH, "x.com.samsung.da.modes", "Unknown"),
        (PRESET_PATH, "x.com.samsung.da.modes", None),
        (PRESET_PATH, "x.com.samsung.da.modes", ["Off"]),
        (PRESET_PATH, "x.com.samsung.da.modes", "Unknown"),
        (DISPLAY_LIGHT_PATH, "mode", None),
        (DISPLAY_LIGHT_PATH, "mode", True),
        (DISPLAY_LIGHT_PATH, "mode", "Unknown"),
        (AUTO_CLEAN_PATH, "x.com.samsung.da.settingStatus", None),
        (AUTO_CLEAN_PATH, "x.com.samsung.da.settingStatus", True),
        (AUTO_CLEAN_PATH, "x.com.samsung.da.settingStatus", "Unknown"),
    ],
)
def test_validate_contract_requires_authoritative_readback_shape(
    path: str,
    field: str,
    malformed: object,
) -> None:
    resources = state_resources()
    if malformed is None:
        del resources[path][field]
    else:
        resources[path][field] = malformed

    with pytest.raises(CapabilityMismatch, match="capability_mismatch"):
        validate_contract(parse_identity(*identity_parts()), resources, compatibility())


def test_validate_contract_rejects_non_whole_temperature_readback() -> None:
    resources = state_resources()
    resources[TEMPERATURE_PATH]["x.com.samsung.da.items"][0][  # type: ignore[index]
        "x.com.samsung.da.desired"
    ] = "26.5"

    with pytest.raises(CapabilityMismatch, match="capability_mismatch"):
        validate_contract(parse_identity(*identity_parts()), resources, compatibility())


@pytest.mark.parametrize(
    "mutation",
    [
        {"always_allowed": ["power", "current_limit"]},
        {"always_allowed": ["power", "hvac_mode", "display_light"]},
        {
            "always_allowed": [
                "power",
                "hvac_mode",
                "display_light",
                "auto_clean",
            ],
            "by_mode": {"Cool": ["temperature", "purification"]},
        },
    ],
)
def test_validate_contract_rejects_unknown_or_incomplete_compatibility(
    mutation: dict[str, object],
) -> None:
    candidate = compatibility()
    candidate.update(mutation)

    with pytest.raises(CapabilityMismatch, match="capability_mismatch"):
        validate_contract(
            parse_identity(*identity_parts()), state_resources(), candidate
        )


@pytest.mark.parametrize(
    ("kind", "value", "path", "payload"),
    [
        (
            CommandKind.POWER,
            True,
            "/power/vs/0",
            {"x.com.samsung.da.power": "On"},
        ),
        (
            CommandKind.POWER,
            False,
            "/power/vs/0",
            {"x.com.samsung.da.power": "Off"},
        ),
        (
            CommandKind.HVAC_MODE,
            HvacMode.AUTO,
            "/mode/vs/0",
            {"x.com.samsung.da.modes": ["Auto"]},
        ),
        (
            CommandKind.HVAC_MODE,
            HvacMode.HEAT,
            "/mode/vs/0",
            {"x.com.samsung.da.modes": ["Heat"]},
        ),
        (
            CommandKind.FAN,
            FanMode.TURBO,
            "/wind/strength/vs/0",
            {"x.com.samsung.da.modes": "4"},
        ),
        (
            CommandKind.SWING,
            SwingMode.BOTH,
            "/wind/direction/vs/0",
            {"x.com.samsung.da.modes": "All"},
        ),
        (
            CommandKind.PRESET,
            PresetMode.WINDFREE,
            "/mode/convenient/vs/0",
            {"x.com.samsung.da.modes": "Nano"},
        ),
        (
            CommandKind.DISPLAY_LIGHT,
            False,
            "/light/vs/0",
            {"mode": "Off"},
        ),
        (
            CommandKind.AUTO_CLEAN,
            True,
            "/option/autoclean/vs/0",
            {"x.com.samsung.da.settingStatus": "On"},
        ),
    ],
)
def test_build_command_uses_exact_live_paths_and_payloads(
    kind: CommandKind,
    value: object,
    path: str,
    payload: dict[str, object],
) -> None:
    command = build_command(kind, value)

    assert command.kind is kind
    assert command.path == path
    assert command.payload == payload
    assert command.requested == value
    assert verify_command(command, {path: copy.deepcopy(payload)})


def test_temperature_builder_requires_fresh_aggregate() -> None:
    with pytest.raises(ValueError, match="fresh aggregate"):
        build_command(CommandKind.TEMPERATURE, 27.0)


def test_temperature_builder_copies_fresh_aggregate_and_only_changes_desired() -> None:
    aggregate = copy.deepcopy(state_resources()[TEMPERATURE_PATH])
    aggregate["unknown"] = {"preserve": [1, 2, 3]}
    aggregate["x.com.samsung.da.items"].append(  # type: ignore[union-attr]
        {
            "x.com.samsung.da.id": "1",
            "x.com.samsung.da.current": "22.0",
            "x.com.samsung.da.desired": "22.0",
        }
    )
    original = copy.deepcopy(aggregate)

    command = build_command(
        CommandKind.TEMPERATURE,
        27.0,
        fresh_aggregate=aggregate,
    )

    expected = copy.deepcopy(original)
    expected["x.com.samsung.da.items"][0][  # type: ignore[index]
        "x.com.samsung.da.desired"
    ] = "27.0"
    assert command.path == TEMPERATURE_PATH
    assert command.payload == expected
    assert aggregate == original
    assert command.payload is not aggregate
    assert (
        command.payload["x.com.samsung.da.items"]
        is not aggregate["x.com.samsung.da.items"]
    )


@pytest.mark.parametrize(
    "existing",
    [None, True, "nan", "31.0", "26.5", object()],
)
def test_temperature_builder_requires_valid_existing_desired_field(
    existing: object,
) -> None:
    aggregate = copy.deepcopy(state_resources()[TEMPERATURE_PATH])
    item = aggregate["x.com.samsung.da.items"][0]  # type: ignore[index]
    if existing is None:
        del item["x.com.samsung.da.desired"]
    else:
        item["x.com.samsung.da.desired"] = existing

    with pytest.raises(ValueError, match="fresh aggregate"):
        build_command(
            CommandKind.TEMPERATURE,
            27,
            fresh_aggregate=aggregate,
        )


@pytest.mark.parametrize(
    "aggregate",
    [
        {},
        {"x.com.samsung.da.items": "not-a-list"},
        {"x.com.samsung.da.items": []},
        {
            "x.com.samsung.da.items": [
                {"x.com.samsung.da.id": "1", "x.com.samsung.da.desired": "20"}
            ]
        },
    ],
)
def test_temperature_builder_rejects_malformed_fresh_aggregate(
    aggregate: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="fresh aggregate"):
        build_command(
            CommandKind.TEMPERATURE,
            27.0,
            fresh_aggregate=aggregate,
        )


def test_temperature_builder_accepts_immutable_nested_mapping() -> None:
    aggregate = state_resources()[TEMPERATURE_PATH]
    aggregate["x.com.samsung.da.items"] = [
        MappingProxyType(
            {
                "x.com.samsung.da.id": "0",
                "x.com.samsung.da.desired": "26.0",
            }
        )
    ]

    command = build_command(
        CommandKind.TEMPERATURE,
        27,
        fresh_aggregate=aggregate,
    )

    assert verify_command(command, {TEMPERATURE_PATH: command.payload})


@pytest.mark.parametrize(
    "value",
    [
        15,
        31,
        26.5,
        True,
        math.nan,
        math.inf,
        10**1000,
        "27",
        "2.7e1",
    ],
)
def test_temperature_builder_rejects_out_of_contract_values(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="command value"):
        build_command(
            CommandKind.TEMPERATURE,
            value,
            fresh_aggregate=state_resources()[TEMPERATURE_PATH],
        )


@pytest.mark.parametrize("value", [16, 27, 30.0])
def test_temperature_builder_normalizes_requested_to_float(value: object) -> None:
    command = build_command(
        CommandKind.TEMPERATURE,
        value,
        fresh_aggregate=state_resources()[TEMPERATURE_PATH],
    )

    assert type(command.requested) is float
    assert command.requested == float(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (CommandKind.POWER, 1),
        (CommandKind.HVAC_MODE, "AI Auto"),
        (CommandKind.FAN, "5"),
        (CommandKind.SWING, "Vertical"),
        (CommandKind.PRESET, "Purification"),
        (CommandKind.DISPLAY_LIGHT, "On"),
        (CommandKind.AUTO_CLEAN, None),
    ],
)
def test_builder_rejects_untyped_or_unverified_values(
    kind: CommandKind,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="command value"):
        build_command(kind, value)


def test_auto_verification_accepts_auto_and_ai_auto_equivalence() -> None:
    command = build_command(CommandKind.HVAC_MODE, HvacMode.AUTO)

    for returned in ("Auto", "AI Auto"):
        assert verify_command(
            command,
            {
                HVAC_MODE_PATH: {
                    "x.com.samsung.da.modes": [returned],
                }
            },
        )

    assert not verify_command(
        command,
        {HVAC_MODE_PATH: {"x.com.samsung.da.modes": ["Cool"]}},
    )


def test_command_payload_is_recursively_immutable() -> None:
    aggregate = copy.deepcopy(state_resources()[TEMPERATURE_PATH])
    command = build_command(
        CommandKind.TEMPERATURE,
        27,
        fresh_aggregate=aggregate,
    )

    with pytest.raises(TypeError):
        command.payload["new"] = "value"  # type: ignore[index]
    items = command.payload["x.com.samsung.da.items"]
    with pytest.raises(TypeError):
        items.append({})  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        items[0]["x.com.samsung.da.desired"] = "28.0"  # type: ignore[index]

    expected = copy.deepcopy(aggregate)
    expected["x.com.samsung.da.items"][0][  # type: ignore[index]
        "x.com.samsung.da.desired"
    ] = "27.0"
    assert command.payload == expected


def test_hvac_payload_nested_modes_are_immutable() -> None:
    command = build_command(CommandKind.HVAC_MODE, HvacMode.COOL)
    modes = command.payload["x.com.samsung.da.modes"]

    with pytest.raises(TypeError):
        modes.append("Heat")  # type: ignore[union-attr]
    assert command.payload == {"x.com.samsung.da.modes": ["Cool"]}


def test_command_payload_has_no_mutable_builtin_base_bypass() -> None:
    command = build_command(CommandKind.HVAC_MODE, HvacMode.COOL)
    modes = command.payload["x.com.samsung.da.modes"]

    assert not isinstance(command.payload, dict)
    assert not isinstance(modes, list)
    with pytest.raises(TypeError):
        dict.__setitem__(command.payload, "new", "value")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        list.append(modes, "Heat")  # type: ignore[arg-type]


def test_frozen_mapping_membership_and_equality_use_mapping_semantics() -> None:
    first = build_command(CommandKind.HVAC_MODE, HvacMode.COOL).payload
    second = build_command(CommandKind.HVAC_MODE, HvacMode.COOL).payload
    plain = {"x.com.samsung.da.modes": ["Cool"]}

    assert "x.com.samsung.da.modes" in first
    assert "missing" not in first
    assert [] not in first
    assert first == first
    assert first == second
    assert second == first
    assert first == plain
    assert plain == first


def test_frozen_nested_mapping_equality_is_symmetric() -> None:
    aggregate = copy.deepcopy(state_resources()[TEMPERATURE_PATH])
    aggregate["unknown"] = {"nested": {"value": ["preserved"]}}
    first = build_command(
        CommandKind.TEMPERATURE,
        27,
        fresh_aggregate=aggregate,
    ).payload
    second = build_command(
        CommandKind.TEMPERATURE,
        27,
        fresh_aggregate=aggregate,
    ).payload

    assert first == second
    assert second == first
    assert first["unknown"] == second["unknown"]
    assert second["unknown"] == first["unknown"]


def test_command_payload_internal_storage_cannot_be_reassigned() -> None:
    command = build_command(CommandKind.HVAC_MODE, HvacMode.COOL)
    modes = command.payload["x.com.samsung.da.modes"]

    with pytest.raises(AttributeError):
        command.payload._data = {}  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        modes._items = ()  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        object.__setattr__(command.payload, "_data", {})
    with pytest.raises(AttributeError):
        object.__setattr__(modes, "_items", ())


def test_nested_set_is_frozen_and_preserved_by_equality() -> None:
    aggregate = copy.deepcopy(state_resources()[TEMPERATURE_PATH])
    aggregate["unknown"] = {"preserved", "values"}
    command = build_command(
        CommandKind.TEMPERATURE,
        27,
        fresh_aggregate=aggregate,
    )
    frozen = command.payload["unknown"]

    assert frozen == {"preserved", "values"}
    with pytest.raises(AttributeError):
        frozen.add("mutation")  # type: ignore[union-attr]


def test_nested_bytearray_is_frozen_to_unaliased_bytes() -> None:
    aggregate = copy.deepcopy(state_resources()[TEMPERATURE_PATH])
    caller_value = bytearray(b"\x01\x02")
    aggregate["unknown"] = {"bytes": caller_value}
    command = build_command(
        CommandKind.TEMPERATURE,
        27,
        fresh_aggregate=aggregate,
    )
    frozen = command.payload["unknown"]["bytes"]  # type: ignore[index]

    assert type(frozen) is bytes
    assert frozen == b"\x01\x02"
    caller_value[0] = 0xFF
    assert frozen == b"\x01\x02"
    assert copy.deepcopy(command.payload) is command.payload


def test_immutable_command_payload_is_safe_to_deepcopy() -> None:
    command = build_command(CommandKind.HVAC_MODE, HvacMode.COOL)

    assert copy.deepcopy(command.payload) is command.payload


def test_deepcopied_immutable_payloads_verify_for_all_commands() -> None:
    commands = [
        build_command(CommandKind.POWER, True),
        build_command(CommandKind.HVAC_MODE, HvacMode.AUTO),
        build_command(
            CommandKind.TEMPERATURE,
            27,
            fresh_aggregate=state_resources()[TEMPERATURE_PATH],
        ),
        build_command(CommandKind.FAN, FanMode.HIGH),
        build_command(CommandKind.SWING, SwingMode.BOTH),
        build_command(CommandKind.PRESET, PresetMode.QUIET),
        build_command(CommandKind.DISPLAY_LIGHT, False),
        build_command(CommandKind.AUTO_CLEAN, True),
    ]

    for command in commands:
        copied = copy.deepcopy(command.payload)
        assert copied is command.payload
        assert verify_command(command, {command.path: copied})


def test_frozen_protocol_sequences_parse_and_validate_like_cbor_lists() -> None:
    resources = state_resources()
    resources[HVAC_MODE_PATH]["x.com.samsung.da.modes"] = ["Auto"]
    resources[TEMPERATURE_PATH]["x.com.samsung.da.items"][0][  # type: ignore[index]
        "x.com.samsung.da.desired"
    ] = "27.0"
    resources["/alarms/vs/0"]["x.com.samsung.da.items"] = [
        {
            "x.com.samsung.da.alarmType": "Device",
            "x.com.samsung.da.code": "ErrorCode_E101",
            "x.com.samsung.da.state": "Active",
        }
    ]
    frozen = DeviceCommand(
        CommandKind.POWER,
        "/synthetic",
        resources,
        None,
        (),
    ).payload

    parsed = parse_device_state(frozen, WindFreeData.empty())  # type: ignore[arg-type]

    assert parsed.climate.mode is HvacMode.AUTO
    assert parsed.climate.target_temperature == 27.0
    assert parsed.alarms.problem is True
    validate_contract(
        parse_identity(*identity_parts()),
        frozen,  # type: ignore[arg-type]
        compatibility(),
    )


def test_generators_are_not_accepted_as_protocol_sequences() -> None:
    resources = state_resources()
    resources[HVAC_MODE_PATH]["x.com.samsung.da.modes"] = (item for item in ["Auto"])
    previous = replace(
        WindFreeData.empty(),
        climate=replace(WindFreeData.empty().climate, mode=HvacMode.HEAT),
    )

    parsed = parse_device_state(resources, previous)

    assert parsed.climate.mode is HvacMode.HEAT
    with pytest.raises(CapabilityMismatch, match="capability_mismatch"):
        validate_contract(
            parse_identity(*identity_parts()),
            resources,
            compatibility(),
        )


def test_temperature_rmw_accepts_deepcopied_immutable_aggregate() -> None:
    first = build_command(
        CommandKind.TEMPERATURE,
        27,
        fresh_aggregate=state_resources()[TEMPERATURE_PATH],
    )

    second = build_command(
        CommandKind.TEMPERATURE,
        28,
        fresh_aggregate=copy.deepcopy(first.payload),
    )

    assert verify_command(second, {TEMPERATURE_PATH: second.payload})
    assert verify_command(first, {TEMPERATURE_PATH: first.payload})


def test_temperature_verification_accepts_numeric_equivalence() -> None:
    command = build_command(
        CommandKind.TEMPERATURE,
        27,
        fresh_aggregate=state_resources()[TEMPERATURE_PATH],
    )

    resources = state_resources()
    resources[TEMPERATURE_PATH]["x.com.samsung.da.items"][0][  # type: ignore[index]
        "x.com.samsung.da.desired"
    ] = 27
    assert verify_command(command, resources)


@pytest.mark.parametrize(
    "resources",
    [
        {},
        {POWER_PATH: {}},
        {POWER_PATH: {"x.com.samsung.da.power": True}},
        {POWER_PATH: {"x.com.samsung.da.power": "Off"}},
    ],
)
def test_verification_fails_closed_on_missing_malformed_or_coerced_state(
    resources: dict[str, dict[str, object]],
) -> None:
    assert not verify_command(
        build_command(CommandKind.POWER, True),
        resources,
    )


def test_command_surface_excludes_unverified_controls() -> None:
    assert {kind.value for kind in CommandKind} == {
        "power",
        "hvac_mode",
        "temperature",
        "fan",
        "swing",
        "preset",
        "display_light",
        "auto_clean",
    }
    excluded = {
        "purification",
        "mute",
        "instant_watts",
        "freeze_wash",
        "timer",
        "self_diagnosis",
        "current_limit",
        "motion",
        "welcome_cooling",
    }
    assert excluded.isdisjoint({kind.value for kind in CommandKind})
