from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class WindFreeError(Exception):
    """Base sanitized integration error."""


class AuthenticationRejected(WindFreeError):
    """Credentials were repeatably rejected."""


class UnsupportedDevice(WindFreeError):
    """Identity does not match the exact supported product."""


class CapabilityMismatch(WindFreeError):
    """Required resource contract changed."""


class CommandRejected(WindFreeError):
    """The device did not retain a requested state."""


class UpdateSource(StrEnum):
    NONE = "none"
    POLL = "poll"
    OBSERVE = "observe"
    RECONCILE = "reconcile"
    COMMAND = "command"


class HvacMode(StrEnum):
    AUTO = "Auto"
    COOL = "Cool"
    DRY = "Dry"
    FAN = "Fan"
    HEAT = "Heat"


class FanMode(StrEnum):
    AUTO = "0"
    LOW = "1"
    MEDIUM = "2"
    HIGH = "3"
    TURBO = "4"


class SwingMode(StrEnum):
    FIXED = "Fix"
    VERTICAL = "Up_And_Low"
    HORIZONTAL = "Left_And_Right"
    BOTH = "All"


class PresetMode(StrEnum):
    NONE = "Off"
    QUIET = "Quiet"
    SMART = "Smart"
    BOOST = "Speed"
    WINDFREE = "Nano"
    WINDFREE_SLEEP = "NanoSleep"
    SLEEP = "Sleep"
    DRY_COMFORT = "DryComfort"


@dataclass(frozen=True, slots=True)
class Credentials:
    client_key_pem: str = field(repr=False)
    client_chain_pem: str = field(repr=False)
    not_before: str
    not_after: str


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    device_id: str
    model: str
    device_type: str
    firmware: str
    platform: str


@dataclass(frozen=True, slots=True)
class ClimateState:
    power: bool = False
    mode: HvacMode = HvacMode.COOL
    current_temperature: float | None = None
    target_temperature: float = 26.0
    humidity: int | None = None
    fan_mode: FanMode = FanMode.AUTO
    swing_mode: SwingMode = SwingMode.FIXED
    preset_mode: PresetMode = PresetMode.NONE

    def __post_init__(self) -> None:
        if not 16.0 <= self.target_temperature <= 30.0:
            raise ValueError("target temperature must be between 16 and 30")
        if self.humidity is not None and not 1 <= self.humidity <= 100:
            raise ValueError("humidity must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class FilterState:
    used: int | None = None
    capacity: int | None = None
    status: str | None = None
    attention: bool = False


@dataclass(frozen=True, slots=True)
class EnergyState:
    cumulative_kwh: float | None = None


@dataclass(frozen=True, slots=True)
class AlarmState:
    problem: bool = False
    active_code: str | None = None
    filter_alarm: bool = False


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    writable_paths: frozenset[str] = frozenset()
    mode_controls: Mapping[HvacMode, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "writable_paths", frozenset(self.writable_paths))
        object.__setattr__(
            self,
            "mode_controls",
            MappingProxyType(
                {
                    mode: frozenset(controls)
                    for mode, controls in self.mode_controls.items()
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class WindFreeData:
    available: bool
    identity: DeviceIdentity | None
    climate: ClimateState
    filter: FilterState
    energy: EnergyState
    alarms: AlarmState
    auto_clean: bool | None
    display_light: bool | None
    current_limit_enabled: bool | None
    current_limit_level: int | None
    contract: CapabilityContract
    update_source: UpdateSource
    generation: int
    failure_count: int

    @classmethod
    def empty(cls) -> WindFreeData:
        return cls(
            available=False,
            identity=None,
            climate=ClimateState(),
            filter=FilterState(),
            energy=EnergyState(),
            alarms=AlarmState(),
            auto_clean=None,
            display_light=None,
            current_limit_enabled=None,
            current_limit_level=None,
            contract=CapabilityContract(),
            update_source=UpdateSource.NONE,
            generation=0,
            failure_count=0,
        )
