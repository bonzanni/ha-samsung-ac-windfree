"""Exact resource contract for the supported Samsung WindFree model."""

from __future__ import annotations

import copy
import hashlib
import hmac
import math
from collections.abc import Iterator, Mapping, Sequence, Set
from dataclasses import dataclass, replace
from enum import StrEnum

from .const import (
    SUPPORTED_DEVICE_TYPE,
    SUPPORTED_FIRMWARE,
    SUPPORTED_MODEL,
    SUPPORTED_PLATFORM,
    SUPPORTED_PLATFORM_FIRMWARE,
    SUPPORTED_PRODUCT_VERSION,
    SUPPORTED_UNIT_FINGERPRINT_SHA256,
)
from .models import (
    AlarmState,
    CapabilityContract,
    CapabilityMismatch,
    ClimateState,
    DeviceIdentity,
    EnergyState,
    FanMode,
    FilterState,
    HvacMode,
    PresetMode,
    SwingMode,
    UnsupportedDevice,
    WindFreeData,
)

POWER_PATH = "/power/vs/0"
HVAC_MODE_PATH = "/mode/vs/0"
TEMPERATURE_PATH = "/temperatures/vs/0"
FAN_PATH = "/wind/strength/vs/0"
SWING_PATH = "/wind/direction/vs/0"
PRESET_PATH = "/mode/convenient/vs/0"
DISPLAY_LIGHT_PATH = "/light/vs/0"
AUTO_CLEAN_PATH = "/option/autoclean/vs/0"
HUMIDITY_PATH = "/humidity/vs/0"
FILTER_PATH = "/filter/airdustfilter/vs/0"
ENERGY_PATH = "/energy/consumption/vs/0"
ALARMS_PATH = "/alarms/vs/0"
CURRENT_LIMIT_PATH = "/electriccurrent/vs/0"

POWER_FIELD = "x.com.samsung.da.power"
MODES_FIELD = "x.com.samsung.da.modes"
SUPPORTED_MODES_FIELD = "x.com.samsung.da.supportedModes"
TEMPERATURE_ITEMS_FIELD = "x.com.samsung.da.items"
TEMPERATURE_ID_FIELD = "x.com.samsung.da.id"
TEMPERATURE_CURRENT_FIELD = "x.com.samsung.da.current"
TEMPERATURE_DESIRED_FIELD = "x.com.samsung.da.desired"
TEMPERATURE_MINIMUM_FIELD = "x.com.samsung.da.minimum"
TEMPERATURE_MAXIMUM_FIELD = "x.com.samsung.da.maximum"
TEMPERATURE_INCREMENT_FIELD = "x.com.samsung.da.increment"
TEMPERATURE_UNIT_FIELD = "x.com.samsung.da.unit"
SETTING_STATUS_FIELD = "x.com.samsung.da.settingStatus"
SUPPORTED_SETTING_STATUS_FIELD = "x.com.samsung.da.supportedSettingStatus"

_SAFE_WRITABLE_PATHS = frozenset(
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
_ALWAYS_ALLOWED = frozenset(
    {
        "power",
        "hvac_mode",
        "display_light",
        "auto_clean",
    }
)
_MODE_CONTROLS = frozenset({"temperature", "fan", "swing", "preset"})
_ALL_HVAC_MODES = frozenset(HvacMode)
_AUTO_ALIASES = frozenset({"Auto", "AI Auto"})


class _FrozenMapping(
    tuple[tuple[object, object], ...],
    Mapping[object, object],
):
    """Inherently immutable tuple of pairs with mapping semantics."""

    __slots__ = ()

    def __new__(
        cls,
        items: Iterator[tuple[object, object]],
    ) -> _FrozenMapping:
        return tuple.__new__(cls, tuple(items))

    def __getitem__(self, key: object) -> object:
        for candidate, value in tuple.__iter__(self):
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[object]:
        return (key for key, _value in tuple.__iter__(self))

    def __contains__(self, key: object) -> bool:
        return any(candidate == key for candidate, _value in tuple.__iter__(self))

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Mapping)
            and all(
                key in other and value == other[key]
                for key, value in tuple.__iter__(self)
            )
            and len(self) == len(other)
        )

    def __repr__(self) -> str:
        return repr(dict(tuple.__iter__(self)))

    def __copy__(self) -> _FrozenMapping:
        return self

    def __deepcopy__(self, _memo: object) -> _FrozenMapping:
        return self


class _FrozenSequence(tuple[object, ...]):
    """Inherently immutable tuple with list-compatible equality."""

    __slots__ = ()
    __hash__ = tuple.__hash__

    def __new__(cls, items: Iterator[object]) -> _FrozenSequence:
        return tuple.__new__(cls, tuple(items))

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Sequence)
            and not isinstance(other, (str, bytes, bytearray))
            and len(self) == len(other)
            and all(left == right for left, right in zip(self, other, strict=True))
        )

    def __repr__(self) -> str:
        return repr(list(self))

    def append(self, _item: object) -> None:
        raise TypeError("command payload is immutable")

    def __copy__(self) -> _FrozenSequence:
        return self

    def __deepcopy__(self, _memo: object) -> _FrozenSequence:
        return self


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    )


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenMapping((key, _freeze(item)) for key, item in value.items())
    if isinstance(value, bytearray):
        return bytes(value)
    if _is_sequence(value):
        return _FrozenSequence(_freeze(item) for item in value)
    if isinstance(value, Set):
        return frozenset(_freeze(item) for item in value)
    return value


def _mutable_copy(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable_copy(item) for key, item in value.items()}
    if _is_sequence(value):
        return [_mutable_copy(item) for item in value]
    if isinstance(value, Set):
        return frozenset(_mutable_copy(item) for item in value)
    return copy.deepcopy(value)


class CommandKind(StrEnum):
    """Safe, live-verified write surfaces."""

    POWER = "power"
    HVAC_MODE = "hvac_mode"
    TEMPERATURE = "temperature"
    FAN = "fan"
    SWING = "swing"
    PRESET = "preset"
    DISPLAY_LIGHT = "display_light"
    AUTO_CLEAN = "auto_clean"


@dataclass(frozen=True, slots=True)
class DeviceCommand:
    """One exact device request and its authoritative verification contract."""

    kind: CommandKind
    path: str
    payload: Mapping[str, object]
    requested: object
    related_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))


def _unsupported() -> UnsupportedDevice:
    return UnsupportedDevice("unsupported_device")


def _capability_mismatch() -> CapabilityMismatch:
    return CapabilityMismatch("capability_mismatch")


def _is_string(value: object) -> bool:
    return isinstance(value, str)


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    if not all(isinstance(key, str) for key in value):
        return None
    return value


def _resource(
    resources: Mapping[str, Mapping[str, object]],
    path: str,
) -> Mapping[str, object]:
    candidate = resources.get(path)
    return candidate if _mapping(candidate) is not None else {}


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except OverflowError, ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _positive_int(value: object) -> int | None:
    parsed = _non_negative_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _parse_enum[EnumT: (HvacMode, FanMode, SwingMode, PresetMode)](
    enum_type: type[EnumT],
    value: object,
    fallback: EnumT,
) -> EnumT:
    if not isinstance(value, str):
        return fallback
    try:
        return enum_type(value)
    except ValueError:
        return fallback


def _mode_value(raw: object, fallback: HvacMode) -> HvacMode:
    if not _is_sequence(raw) or len(raw) != 1:
        return fallback
    value = raw[0]
    if value in _AUTO_ALIASES:
        return HvacMode.AUTO
    return _parse_enum(HvacMode, value, fallback)


def _on_off(value: object) -> bool | None:
    if value == "On":
        return True
    if value == "Off":
        return False
    return None


def parse_identity(
    oic_d: Mapping[str, object],
    oic_p: Mapping[str, object],
    device_0: Mapping[str, object],
) -> DeviceIdentity:
    """Parse and enforce the exact four-gate product identity."""

    device_id = oic_d.get("di")
    device_types = oic_d.get("rt")
    product_version = oic_p.get("mnpv")
    platform = oic_p.get("mnos")
    platform_firmware = oic_p.get("mnfv")
    information = _mapping(device_0.get("/information/vs/0"))
    firmware = (
        information.get("x.com.samsung.da.description")
        if information is not None
        else None
    )
    model_number = (
        information.get("x.com.samsung.da.modelNum")
        if information is not None
        else None
    )
    model_firmware, separator, model_discriminator = (
        model_number.partition("|") if isinstance(model_number, str) else ("", "", "")
    )

    if (
        not _is_string(device_id)
        or not device_id
        or not _is_sequence(device_types)
        or not all(isinstance(item, str) for item in device_types)
        or SUPPORTED_DEVICE_TYPE not in device_types
        or product_version != SUPPORTED_PRODUCT_VERSION
        or platform != SUPPORTED_PLATFORM
        or platform_firmware != SUPPORTED_PLATFORM_FIRMWARE
        or firmware != SUPPORTED_FIRMWARE
        or separator != "|"
        or model_firmware != firmware
        or not model_discriminator
        or not hmac.compare_digest(
            hashlib.sha256(model_number.encode("utf-8")).hexdigest(),
            SUPPORTED_UNIT_FINGERPRINT_SHA256,
        )
    ):
        raise _unsupported()

    return DeviceIdentity(
        device_id=device_id,
        model=SUPPORTED_MODEL,
        device_type=SUPPORTED_DEVICE_TYPE,
        firmware=firmware,
        platform=platform,
    )


def parse_humidity(raw: object) -> int | None:
    """Parse the direct percentage field, treating zero as unset."""

    parsed = _non_negative_int(raw)
    if parsed is None or not 1 <= parsed <= 100:
        return None
    return parsed


def _temperature_item(resource: Mapping[str, object]) -> Mapping[str, object] | None:
    items = resource.get(TEMPERATURE_ITEMS_FIELD)
    if not _is_sequence(items):
        return None
    for item in items:
        mapped = _mapping(item)
        if mapped is not None and mapped.get(TEMPERATURE_ID_FIELD) == "0":
            return mapped
    return None


def _parse_temperature(
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    parsed = _finite_float(value)
    if parsed is None:
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def _whole_temperature(value: object) -> float | None:
    parsed = _parse_temperature(value, minimum=16, maximum=30)
    if parsed is None or isinstance(value, bool) or not parsed.is_integer():
        return None
    return parsed


def _parse_filter(resource: Mapping[str, object]) -> FilterState:
    capacity = _positive_int(resource.get("x.com.samsung.da.filterCapacity"))
    used = _non_negative_int(resource.get("x.com.samsung.da.filterUsage"))
    if capacity is None or used is None:
        capacity = None
        used = None
    raw_status = resource.get("x.com.samsung.da.filterStatus")
    status = raw_status.casefold() if isinstance(raw_status, str) else None
    attention = status in {"wash", "replace"}
    return FilterState(
        used=used,
        capacity=capacity,
        status=status,
        attention=attention,
    )


def _parse_energy(resource: Mapping[str, object]) -> EnergyState:
    if (
        resource.get("x.com.samsung.da.cumulativeUnit") != "Wh"
        or resource.get("x.com.samsung.da.cumulativePowerType") != "total"
    ):
        return EnergyState()
    watt_hours = _finite_float(resource.get("x.com.samsung.da.cumulativePower"))
    if watt_hours is None or watt_hours < 0:
        return EnergyState()
    kilowatt_hours = watt_hours / 1000
    if not math.isfinite(kilowatt_hours):
        return EnergyState()
    return EnergyState(cumulative_kwh=kilowatt_hours)


def _parse_alarms(resource: Mapping[str, object]) -> AlarmState | None:
    items = resource.get(TEMPERATURE_ITEMS_FIELD)
    if not _is_sequence(items):
        return None

    device_code: str | None = None
    filter_code: str | None = None
    for item in items:
        mapped = _mapping(item)
        if mapped is None:
            return None
        state = mapped.get("x.com.samsung.da.state")
        code = mapped.get("x.com.samsung.da.code")
        alarm_type = mapped.get("x.com.samsung.da.alarmType")
        if (
            not isinstance(state, str)
            or not isinstance(code, str)
            or not code
            or not isinstance(alarm_type, str)
            or state not in {"Active", "Deleted"}
            or alarm_type not in {"Device", "Filter"}
        ):
            return None
        if state == "Deleted":
            continue
        if alarm_type == "Filter" and filter_code is None:
            filter_code = code
        elif alarm_type == "Device" and device_code is None:
            device_code = code

    return AlarmState(
        problem=device_code is not None,
        active_code=device_code or filter_code,
        filter_alarm=filter_code is not None,
    )


def parse_device_state(
    resources: Mapping[str, Mapping[str, object]],
    previous: WindFreeData,
) -> WindFreeData:
    """Parse sanitized resource mappings into a new immutable state snapshot."""

    power = _on_off(_resource(resources, POWER_PATH).get(POWER_FIELD))
    mode = _mode_value(
        _resource(resources, HVAC_MODE_PATH).get(MODES_FIELD),
        previous.climate.mode,
    )
    temperature_item = _temperature_item(_resource(resources, TEMPERATURE_PATH))
    current_temperature = (
        _parse_temperature(temperature_item.get(TEMPERATURE_CURRENT_FIELD))
        if temperature_item is not None
        else None
    )
    parsed_target = (
        _parse_temperature(
            temperature_item.get(TEMPERATURE_DESIRED_FIELD),
            minimum=16,
            maximum=30,
        )
        if temperature_item is not None
        else None
    )
    target_temperature = (
        parsed_target
        if parsed_target is not None
        else previous.climate.target_temperature
    )
    humidity = parse_humidity(
        _resource(resources, HUMIDITY_PATH).get("x.com.samsung.da.fivepercentHumidity")
    )
    fan_mode = _parse_enum(
        FanMode,
        _resource(resources, FAN_PATH).get(MODES_FIELD),
        previous.climate.fan_mode,
    )
    swing_mode = _parse_enum(
        SwingMode,
        _resource(resources, SWING_PATH).get(MODES_FIELD),
        previous.climate.swing_mode,
    )
    preset_mode = _parse_enum(
        PresetMode,
        _resource(resources, PRESET_PATH).get(MODES_FIELD),
        previous.climate.preset_mode,
    )

    alarm_resource = _mapping(resources.get(ALARMS_PATH))
    parsed_alarms = (
        _parse_alarms(alarm_resource) if alarm_resource is not None else None
    )
    alarms = parsed_alarms if parsed_alarms is not None else previous.alarms
    filter_state = _parse_filter(_resource(resources, FILTER_PATH))
    if alarms.filter_alarm and not filter_state.attention:
        filter_state = replace(filter_state, attention=True)

    current_limit = _resource(resources, CURRENT_LIMIT_PATH)
    current_limit_enabled = _on_off(current_limit.get(SETTING_STATUS_FIELD))
    current_limit_level = _non_negative_int(current_limit.get("x.com.samsung.da.level"))

    return replace(
        previous,
        climate=ClimateState(
            power=power if power is not None else previous.climate.power,
            mode=mode,
            current_temperature=current_temperature,
            target_temperature=target_temperature,
            humidity=humidity,
            fan_mode=fan_mode,
            swing_mode=swing_mode,
            preset_mode=preset_mode,
        ),
        filter=filter_state,
        energy=_parse_energy(_resource(resources, ENERGY_PATH)),
        alarms=alarms,
        auto_clean=_on_off(
            _resource(resources, AUTO_CLEAN_PATH).get(SETTING_STATUS_FIELD)
        ),
        display_light=_on_off(_resource(resources, DISPLAY_LIGHT_PATH).get("mode")),
        current_limit_enabled=current_limit_enabled,
        current_limit_level=current_limit_level,
    )


def _string_set(value: object) -> frozenset[str] | None:
    if not _is_sequence(value) or not all(isinstance(item, str) for item in value):
        return None
    return frozenset(value)


def _numeric_equals(value: object, expected: float) -> bool:
    parsed = _finite_float(value)
    return parsed is not None and parsed == expected


def _valid_hvac_mode_value(value: object) -> bool:
    return (
        _is_sequence(value)
        and len(value) == 1
        and value[0] in _AUTO_ALIASES | frozenset(mode.value for mode in HvacMode)
    )


def _valid_enum_value[EnumT: (FanMode, SwingMode, PresetMode)](
    value: object,
    enum_type: type[EnumT],
) -> bool:
    return isinstance(value, str) and value in frozenset(
        member.value for member in enum_type
    )


def _identity_is_supported(identity: DeviceIdentity) -> bool:
    return (
        identity.model == SUPPORTED_MODEL
        and identity.device_type == SUPPORTED_DEVICE_TYPE
        and identity.firmware == SUPPORTED_FIRMWARE
        and identity.platform == SUPPORTED_PLATFORM
    )


def _validate_live_resources(
    resources: Mapping[str, Mapping[str, object]],
) -> bool:
    power = _resource(resources, POWER_PATH)
    hvac_mode = _resource(resources, HVAC_MODE_PATH)
    temperature = _temperature_item(_resource(resources, TEMPERATURE_PATH))
    fan = _resource(resources, FAN_PATH)
    swing = _resource(resources, SWING_PATH)
    preset = _resource(resources, PRESET_PATH)
    light = _resource(resources, DISPLAY_LIGHT_PATH)
    auto_clean = _resource(resources, AUTO_CLEAN_PATH)

    if power.get(POWER_FIELD) not in {"On", "Off"}:
        return False
    if _string_set(hvac_mode.get(SUPPORTED_MODES_FIELD)) != frozenset(
        mode.value for mode in HvacMode
    ):
        return False
    if not _valid_hvac_mode_value(hvac_mode.get(MODES_FIELD)):
        return False
    if temperature is None:
        return False
    if not (
        _numeric_equals(temperature.get(TEMPERATURE_MINIMUM_FIELD), 16)
        and _numeric_equals(temperature.get(TEMPERATURE_MAXIMUM_FIELD), 30)
        and _numeric_equals(temperature.get(TEMPERATURE_INCREMENT_FIELD), 1)
        and temperature.get(TEMPERATURE_UNIT_FIELD) == "Celsius"
        and _whole_temperature(temperature.get(TEMPERATURE_DESIRED_FIELD)) is not None
    ):
        return False
    if _string_set(fan.get(SUPPORTED_MODES_FIELD)) != frozenset(
        mode.value for mode in FanMode
    ):
        return False
    if not _valid_enum_value(fan.get(MODES_FIELD), FanMode):
        return False
    if _string_set(swing.get(SUPPORTED_MODES_FIELD)) != frozenset(
        mode.value for mode in SwingMode
    ):
        return False
    if not _valid_enum_value(swing.get(MODES_FIELD), SwingMode):
        return False
    if _string_set(preset.get(SUPPORTED_MODES_FIELD)) != frozenset(
        mode.value for mode in PresetMode
    ):
        return False
    if not _valid_enum_value(preset.get(MODES_FIELD), PresetMode):
        return False
    if _string_set(light.get("supportedModes")) != frozenset({"On", "Off"}):
        return False
    if _on_off(light.get("mode")) is None:
        return False
    if _on_off(auto_clean.get(SETTING_STATUS_FIELD)) is None:
        return False
    return _string_set(auto_clean.get(SUPPORTED_SETTING_STATUS_FIELD)) == frozenset(
        {"On", "Off"}
    )


def _validate_compatibility(
    compatibility: Mapping[str, object],
) -> Mapping[HvacMode, frozenset[str]] | None:
    if _string_set(compatibility.get("always_allowed")) != _ALWAYS_ALLOWED:
        return None
    by_mode = _mapping(compatibility.get("by_mode"))
    if by_mode is None or frozenset(by_mode) != frozenset(
        mode.value for mode in HvacMode
    ):
        return None

    result: dict[HvacMode, frozenset[str]] = {}
    for mode in _ALL_HVAC_MODES:
        controls = _string_set(by_mode.get(mode.value))
        if controls is None or not controls <= _MODE_CONTROLS:
            return None
        result[mode] = controls
    return result


def validate_contract(
    identity: DeviceIdentity,
    resources: Mapping[str, Mapping[str, object]],
    compatibility: Mapping[str, object],
) -> CapabilityContract:
    """Validate the exact supported identity and live safe-write resources."""

    mode_controls = _validate_compatibility(compatibility)
    if (
        not _identity_is_supported(identity)
        or not _validate_live_resources(resources)
        or mode_controls is None
    ):
        raise _capability_mismatch()
    return CapabilityContract(
        writable_paths=_SAFE_WRITABLE_PATHS,
        mode_controls=mode_controls,
    )


def _require_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("invalid command value")
    return value


def _require_enum[EnumT: (HvacMode, FanMode, SwingMode, PresetMode)](
    value: object, enum_type: type[EnumT]
) -> EnumT:
    if not isinstance(value, enum_type):
        raise ValueError("invalid command value")
    return value


def _require_temperature(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid command value")
    parsed = _finite_float(value)
    if parsed is None or not 16 <= parsed <= 30 or not parsed.is_integer():
        raise ValueError("invalid command value")
    return parsed


def _temperature_payload(
    fresh_aggregate: Mapping[str, object] | None,
    desired: float,
) -> Mapping[str, object]:
    if fresh_aggregate is None or _mapping(fresh_aggregate) is None:
        raise ValueError("fresh aggregate is required")
    try:
        payload = _mutable_copy(fresh_aggregate)
    except Exception:
        raise ValueError("fresh aggregate is invalid") from None
    items = payload.get(TEMPERATURE_ITEMS_FIELD)
    if not _is_sequence(items):
        raise ValueError("fresh aggregate is invalid")
    for item in items:
        mapped = _mapping(item)
        if mapped is not None and mapped.get(TEMPERATURE_ID_FIELD) == "0":
            if (
                not isinstance(item, dict)
                or _whole_temperature(mapped.get(TEMPERATURE_DESIRED_FIELD)) is None
            ):
                raise ValueError("fresh aggregate is invalid")
            item[TEMPERATURE_DESIRED_FIELD] = f"{desired:.1f}"
            return payload
    raise ValueError("fresh aggregate is invalid")


def build_command(
    kind: CommandKind,
    value: object,
    *,
    fresh_aggregate: Mapping[str, object] | None = None,
) -> DeviceCommand:
    """Build one of the eight exact safe live request shapes."""

    if kind is CommandKind.POWER:
        requested = _require_bool(value)
        return DeviceCommand(
            kind=kind,
            path=POWER_PATH,
            payload={POWER_FIELD: "On" if requested else "Off"},
            requested=requested,
            related_paths=(POWER_PATH,),
        )
    if kind is CommandKind.HVAC_MODE:
        requested = _require_enum(value, HvacMode)
        return DeviceCommand(
            kind=kind,
            path=HVAC_MODE_PATH,
            payload={MODES_FIELD: [requested.value]},
            requested=requested,
            related_paths=(
                HVAC_MODE_PATH,
                TEMPERATURE_PATH,
                FAN_PATH,
                SWING_PATH,
                PRESET_PATH,
            ),
        )
    if kind is CommandKind.TEMPERATURE:
        requested = _require_temperature(value)
        return DeviceCommand(
            kind=kind,
            path=TEMPERATURE_PATH,
            payload=_temperature_payload(fresh_aggregate, requested),
            requested=requested,
            related_paths=(TEMPERATURE_PATH,),
        )
    if kind is CommandKind.FAN:
        requested = _require_enum(value, FanMode)
        return DeviceCommand(
            kind=kind,
            path=FAN_PATH,
            payload={MODES_FIELD: requested.value},
            requested=requested,
            related_paths=(FAN_PATH, PRESET_PATH),
        )
    if kind is CommandKind.SWING:
        requested = _require_enum(value, SwingMode)
        return DeviceCommand(
            kind=kind,
            path=SWING_PATH,
            payload={MODES_FIELD: requested.value},
            requested=requested,
            related_paths=(SWING_PATH, PRESET_PATH),
        )
    if kind is CommandKind.PRESET:
        requested = _require_enum(value, PresetMode)
        return DeviceCommand(
            kind=kind,
            path=PRESET_PATH,
            payload={MODES_FIELD: requested.value},
            requested=requested,
            related_paths=(
                PRESET_PATH,
                TEMPERATURE_PATH,
                FAN_PATH,
                SWING_PATH,
            ),
        )
    if kind is CommandKind.DISPLAY_LIGHT:
        requested = _require_bool(value)
        return DeviceCommand(
            kind=kind,
            path=DISPLAY_LIGHT_PATH,
            payload={"mode": "On" if requested else "Off"},
            requested=requested,
            related_paths=(DISPLAY_LIGHT_PATH,),
        )
    if kind is CommandKind.AUTO_CLEAN:
        requested = _require_bool(value)
        return DeviceCommand(
            kind=kind,
            path=AUTO_CLEAN_PATH,
            payload={
                SETTING_STATUS_FIELD: "On" if requested else "Off",
            },
            requested=requested,
            related_paths=(AUTO_CLEAN_PATH,),
        )
    raise ValueError("invalid command value")


def _verify_temperature(
    command: DeviceCommand,
    resource: Mapping[str, object],
) -> bool:
    item = _temperature_item(resource)
    expected = _finite_float(command.requested)
    actual = (
        _finite_float(item.get(TEMPERATURE_DESIRED_FIELD)) if item is not None else None
    )
    return expected is not None and actual is not None and expected == actual


def verify_command(
    command: DeviceCommand,
    resources: Mapping[str, Mapping[str, object]],
) -> bool:
    """Verify a requested value against authoritative resource state."""

    resource = _resource(resources, command.path)
    if command.kind is CommandKind.POWER:
        return _on_off(resource.get(POWER_FIELD)) is command.requested
    if command.kind is CommandKind.HVAC_MODE:
        raw = resource.get(MODES_FIELD)
        if not _is_sequence(raw) or len(raw) != 1:
            return False
        actual = raw[0]
        if command.requested is HvacMode.AUTO:
            return actual in _AUTO_ALIASES
        return actual == command.requested.value
    if command.kind is CommandKind.TEMPERATURE:
        return _verify_temperature(command, resource)
    if command.kind in {
        CommandKind.FAN,
        CommandKind.SWING,
        CommandKind.PRESET,
    }:
        return resource.get(MODES_FIELD) == command.requested.value
    if command.kind is CommandKind.DISPLAY_LIGHT:
        return _on_off(resource.get("mode")) is command.requested
    if command.kind is CommandKind.AUTO_CLEAN:
        return _on_off(resource.get(SETTING_STATUS_FIELD)) is command.requested
    return False
