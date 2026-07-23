# Samsung WindFree Local Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a HACS-installable Home Assistant custom integration that
configures the exact Samsung `AR60F12C1AWNEU` from its host alone and controls
it locally over authenticated OCF/CoAP-DTLS without SmartThings at runtime.

**Architecture:** A pinned, one-time bootstrap creates per-installation
credentials, after which a narrow adapter around
`smartthings_local.protocol.dtls_session.DtlsCoapSession` owns all wire I/O.
A connection-generation supervisor and coordinator maintain one immutable
state cache, tiered polling, OBSERVE updates, verified writes, capability
drift detection, and availability. Home Assistant entities read coordinator
memory only.

**Tech Stack:** Python 3.14.2+, Home Assistant 2026.5.4+,
`smartthings-local==0.1.0`,
`cbor2==6.1.3`, Home Assistant's pyOpenSSL/cryptography stack, pytest,
pytest-homeassistant-custom-component, Ruff, hassfest, and HACS validation.

## Global Constraints

- Domain: `samsung_ac_windfree`.
- Exact supported consumer model: `AR60F12C1AWNEU`.
- Required OCF device type: `oic.d.airconditioner`.
- Required firmware prefix: `TP1X_DA-AC-RAC-01001`.
- Required platform: Tizen Lite / TizenRT 4.0.
- Probe only UDP ports `49152` through `49160`; live target was `49154`.
- Configuration asks for host/IP only.
- Ordinary setup/startup after successful bootstrap performs no internet I/O.
- Bootstrap pins the REMOVED_IDENTITY bundle, Samsung leaf, and Samsung SPKI values from
  the approved design; never weaken or replace a pin silently.
- Never persist the universal REMOVED_IDENTITY private key; persist only the generated
  per-installation key, leaf, and required public intermediates.
- Use in-memory PEM transport loading; never write transport credential temp
  files.
- Only the exact model and firmware combination is supported; do not add older
  Samsung protocols or fallback mappings.
- One DTLS session per config entry, at most two logical requests per second,
  and one serialized foreground command.
- A CoAP `2.04 Changed` is acknowledgement only; publish success after
  authoritative read-back or a matching current-generation OBSERVE update.
- Entity properties perform no I/O.
- All logs, exceptions, Repairs issues, and diagnostics redact host, IP, UUID,
  serial, MAC, SSID, certificates, keys, raw payloads, and config-entry IDs.
- Tests use synthetic sanitized fixtures; live identifiers and credentials
  remain outside Git.
- TDD is mandatory: observe the named test fail before adding production code.
- Each task ends with its own reviewable commit.
- Immediately before every task commit, run
  `.venv/bin/ruff format custom_components tests` and
  `.venv/bin/ruff check custom_components tests` after the focused pytest
  command. Both Ruff commands must exit `0`; formatting fixes are part of the
  same task.

## File and Responsibility Map

```text
custom_components/samsung_ac_windfree/
  __init__.py          Config-entry setup, unload, reload, migration
  binary_sensor.py     Problem, filter attention, current-limit enabled
  bootstrap.py         Pinned downloads, certificate validation/minting
  climate.py           Climate entity and HA-to-domain command mapping
  config_flow.py       User, progress, reconfigure, reauth flows
  const.py             Domain, pins, paths, timeouts, model contract
  coordinator.py       Immutable state, scheduler, OBSERVE, commands, Repairs
  device.py            Resource parsers and request builders
  diagnostics.py       Zero-I/O allowlisted diagnostics
  entity.py            Shared coordinator entity and DeviceInfo
  manifest.json        HA metadata and exact isolated requirements
  models.py            Immutable data, enums, credentials, errors
  quality_scale.yaml   Explicit quality-rule status
  sensor.py            Filter, energy, alarm, current-limit sensors
  strings.json         English config/entity/Repairs/action strings
  switch.py            Auto-clean and display-light switches
  transport.py         Executor-safe DTLS/CoAP adapter and supervisor
  translations/en.json English translation mirror
tests/
  conftest.py
  fixtures/
    device_identity.json
    device_state.json
    mode_compatibility.json
  test_binary_sensor.py
  test_bootstrap.py
  test_climate.py
  test_config_flow.py
  test_coordinator.py
  test_device.py
  test_diagnostics.py
  test_dependency_contract.py
  test_init.py
  test_models.py
  test_sensor.py
  test_switch.py
  test_transport.py
.github/workflows/ci.yml
.gitignore
CHANGELOG.md
LICENSE
README.md
hacs.json
pyproject.toml
requirements_test.txt
requirements_test_min.txt
```

The public interfaces below are fixed for the plan. If implementation proves
one impossible, stop that task and amend this plan before allowing later tasks
to invent another name.

## Shared Test Harness Contract

Each task extends `tests/conftest.py` only with the fixtures assigned here:

- `credentials: Credentials` returns a valid ephemeral RSA key plus SHA-1-signed
  synthetic client chain and validity dates; it is loadable by real pyOpenSSL.
- `bootstrap_inputs: BootstrapInputs` and `bootstrap_pins: BootstrapPins`
  generate an ephemeral four-certificate chain and matching fingerprints.
- `resource_representations: dict[str, dict[str, object]]` combines the
  `/oic/d`, `/oic/p`, `/device/0`, and state-path representations from
  `device_identity.json` and `device_state.json`.
- `encoded_resources: dict[str, bytes]` applies `cbor2.dumps` to each
  representation for low-level session mocks.
- `ManualClock` exposes `monotonic() -> float`,
  `async sleep(delay: float) -> None`, and `advance(delay: float) -> None`.
  `sleep` records the delay, advances the monotonic value by that delay, yields
  to the event loop once with `await asyncio.sleep(0)`, and returns; `advance`
  changes the same value synchronously for tests that have no active sleeper.
- `FakeSession` implements the real dependency's blocking
  `connect/start_reader/get/post/subscribe/close/join` method shapes and records
  calls. Its GET values are `(69, encoded_resources[path])`; POST returns
  `(68, b"")`.
- `TransportFactoryStub.__call__` is an `AsyncMock` returning `current`.
  `TransportFactoryStub.current` is a stateful `AsyncMock` transport:
  `async_get` deep-copies the representation for its path from
  `resource_representations`, while `async_post` replaces that path's
  representation with a deep copy of the posted mapping before returning.
  This echo behavior makes the normal authoritative read-back succeed; tests
  override it explicitly to exercise rejection.
  `TransportFactoryStub.discover` is an `AsyncMock` returning a replacement
  `(port, transport)` pair, and `TransportFactoryStub.reconnect` is an
  `AsyncMock` returning a replacement transport on the existing port; every
  replacement uses the same stateful GET/POST helper.
- `transport_factory` is the pytest fixture that returns the shared
  `TransportFactoryStub`.
- `coordinator` constructs `WindFreeCoordinator` with `ManualClock.monotonic`,
  `ManualClock.sleep`, `TransportFactoryStub`, synthetic credentials, and the
  compatibility fixture, then seeds it with the exact identity/state fixtures.
- `command_mock: AsyncMock` replaces `coordinator.async_command` in platform
  setup so command assertions do not depend on a real transport round trip.
- `validated_setup` contains the synthetic host, port `49154`, exact supported
  identity, and synthetic credentials.
- `config_entry` is `MockConfigEntry` version `1` with `validated_setup` data
  and the synthetic device UUID as unique ID.
- `setup_integration` adds `config_entry`, assigns the shared coordinator to
  `config_entry.runtime_data`, patches coordinator construction to return that
  coordinator, calls `async_setup`, and blocks until every forwarded platform
  finishes. It lets `coordinator.async_start` run against the stateful stub,
  proving setup parses identity and state before platforms load; only after
  startup does it replace `coordinator.async_command` with `command_mock`.
- `climate_entity` and `energy_entity` depend on `setup_integration` and return
  the resulting entity IDs.

Use this exact protocol for injected factories:

```python
class TransportFactory(Protocol):
    async def __call__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        credentials: Credentials,
        generation: int,
    ) -> WindFreeTransport: ...

    async def discover(
        self,
        hass: HomeAssistant,
        host: str,
        credentials: Credentials,
        generation: int,
    ) -> tuple[int, WindFreeTransport]: ...

    async def reconnect(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        credentials: Credentials,
        generation: int,
    ) -> WindFreeTransport: ...
```

`WindFreeCoordinator.__init__` accepts:

```python
monotonic: Callable[[], float] = time.monotonic,
sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
transport_factory: TransportFactory | None = None,
```

The constructor assigns `DefaultTransportFactory()` only when
`transport_factory is None`, in its body, avoiding a Ruff B008 call in the
default expression.

No test uses wall-clock sleeps, production addresses, raw production CBOR, or
real credentials.

---

### Task 1: Repository Scaffold and Test Harness

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `requirements_test.txt`
- Create: `requirements_test_min.txt`
- Create: `hacs.json`
- Create: `custom_components/samsung_ac_windfree/__init__.py`
- Create: `custom_components/samsung_ac_windfree/const.py`
- Create: `custom_components/samsung_ac_windfree/manifest.json`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_init.py`

**Interfaces:**
- Produces: `DOMAIN`, `PLATFORMS`, model/pin/timeout constants, and an importable
  custom integration.
- Consumes: none.

- [ ] **Step 1: Add the failing manifest and constant tests**

```python
# tests/test_init.py
from __future__ import annotations

import json
from pathlib import Path

from custom_components.samsung_ac_windfree.const import (
    DOMAIN,
    PROBE_PORTS,
    SUPPORTED_FIRMWARE_PREFIX,
    SUPPORTED_MODEL,
)


def test_fixed_product_contract() -> None:
    assert DOMAIN == "samsung_ac_windfree"
    assert SUPPORTED_MODEL == "AR60F12C1AWNEU"
    assert SUPPORTED_FIRMWARE_PREFIX == "TP1X_DA-AC-RAC-01001"
    assert PROBE_PORTS == tuple(range(49152, 49161))


def test_manifest_pins_isolated_requirements() -> None:
    manifest = json.loads(
        Path(
            "custom_components/samsung_ac_windfree/manifest.json"
        ).read_text()
    )
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "local_push"
    assert manifest["integration_type"] == "device"
    assert manifest["requirements"] == [
        "smartthings-local==0.1.0",
        "cbor2==6.1.3",
    ]
```

- [ ] **Step 2: Create the test environment and confirm the scaffold is absent**

Run: `python3 -m venv .venv`

Run:
`.venv/bin/pip install pytest-homeassistant-custom-component==0.13.347`

Run: `.venv/bin/pytest tests/test_init.py -q`

Expected: collection fails with a `ModuleNotFoundError` for
`custom_components`.

- [ ] **Step 3: Create the minimal package and metadata**

```python
# custom_components/samsung_ac_windfree/const.py
from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "samsung_ac_windfree"
PLATFORMS = (
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.SWITCH,
)

SUPPORTED_MODEL = "AR60F12C1AWNEU"
SUPPORTED_DEVICE_TYPE = "oic.d.airconditioner"
SUPPORTED_FIRMWARE_PREFIX = "TP1X_DA-AC-RAC-01001"
SUPPORTED_PLATFORM = "TizenRT 4.0"
PROBE_PORTS = tuple(range(49152, 49161))

HOST_RESOLVE_TIMEOUT = 5.0
HTTPS_TIMEOUT = 15.0
PROBE_HANDSHAKE_TIMEOUT = 6.0
RUNTIME_HANDSHAKE_TIMEOUT = 12.0
COAP_READ_TIMEOUT = 8.0
SETUP_TIMEOUT = 150.0
RATE_LIMIT_RPS = 2.0
COMMAND_OBSERVE_TIMEOUT = 2.0
RECONNECT_MIN_SECONDS = 2.0
RECONNECT_MAX_SECONDS = 60.0

HOT_INTERVAL = timedelta(seconds=5)
WARM_INTERVAL = timedelta(seconds=30)
COLD_INTERVAL = timedelta(minutes=5)
RECONCILE_INTERVAL = timedelta(minutes=5)
CERT_REPAIR_WINDOW = timedelta(days=90)

HOT_PATHS = (
    "/power/vs/0",
    "/mode/vs/0",
    "/temperatures/vs/0",
    "/wind/strength/vs/0",
    "/wind/direction/vs/0",
    "/mode/convenient/vs/0",
)
WARM_PATHS = (
    "/humidity/vs/0",
    "/energy/consumption/vs/0",
    "/alarms/vs/0",
    "/light/vs/0",
    "/option/autoclean/vs/0",
)
COLD_PATHS = (
    "/filter/airdustfilter/vs/0",
    "/electriccurrent/vs/0",
)
RECONCILE_PATHS = ("/oic/d", "/oic/p", "/device/0")

BUNDLE_URL = (
    "https://REMOVED_SOURCE_HOST/REMOVED_SOURCE_OWNER/"
    "REMOVED_SOURCE/main/cert.pem"
)
BUNDLE_SHA256 = (
    "REMOVED_BUNDLE_SHA256"
)
REMOVED_SIGNING_DIGEST_NAME = (
    "REMOVED_SIGNING_DIGEST"
)
SAMSUNG_IDENTITY_HOST = "REMOVED_IDENTITY_HOST"
SAMSUNG_IDENTITY_LEAF_SHA256 = (
    "REMOVED_IDENTITY_LEAF_DIGEST"
)
SAMSUNG_IDENTITY_SPKI_SHA256 = (
    "REMOVED_IDENTITY_SPKI_DIGEST"
)
```

```python
# custom_components/samsung_ac_windfree/__init__.py
"""Local integration for the exact Samsung WindFree AC model."""

from __future__ import annotations
```

```json
{
  "domain": "samsung_ac_windfree",
  "name": "Samsung WindFree AC",
  "codeowners": ["@bonzanni"],
  "config_flow": true,
  "dependencies": [],
  "documentation": "https://github.com/bonzanni/ha-samsung-ac-windfree",
  "integration_type": "device",
  "iot_class": "local_push",
  "issue_tracker": "https://github.com/bonzanni/ha-samsung-ac-windfree/issues",
  "loggers": ["smartthings_local"],
  "requirements": [
    "smartthings-local==0.1.0",
    "cbor2==6.1.3"
  ],
  "version": "0.1.0"
}
```

```python
# tests/conftest.py
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
```

Create `tests/__init__.py` as an empty file. Set `hacs.json` to:

```json
{
  "name": "Samsung WindFree AC",
  "homeassistant": "2026.5.4",
  "render_readme": true
}
```

Set `pyproject.toml` to:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.coverage.run]
source = ["custom_components/samsung_ac_windfree"]

[tool.coverage.report]
fail_under = 95
show_missing = true

[tool.ruff]
target-version = "py314"
line-length = 88

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "I", "UP", "B", "ASYNC", "RUF"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "PLR2004", "SLF001"]
```

Set `requirements_test.txt` to:

```text
pytest-homeassistant-custom-component==0.13.347
ruff==0.15.21
smartthings-local==0.1.0
cbor2==6.1.3
```

Set `requirements_test_min.txt` to the independently verified mapping where
PHACC `0.13.333` requires Home Assistant `2026.5.4`:

```text
pytest-homeassistant-custom-component==0.13.333
smartthings-local==0.1.0
cbor2==6.1.3
```

Set `.gitignore` to:

```text
.coverage
.pytest_cache/
.ruff_cache/
.venv/
__pycache__/
htmlcov/
*.py[cod]
```

- [ ] **Step 4: Install the declared requirements and run scaffold tests**

Run: `.venv/bin/pip install -r requirements_test.txt`

Run: `.venv/bin/pytest tests/test_init.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Run Ruff and commit the scaffold**

Run: `.venv/bin/ruff format custom_components tests`

Run: `.venv/bin/ruff check custom_components tests`

Expected: both commands exit `0`.

```bash
git add .gitignore pyproject.toml requirements_test.txt \
  requirements_test_min.txt hacs.json \
  custom_components tests
git commit -m "chore: scaffold Samsung WindFree integration"
```

---

### Task 2: Immutable Models, Mappings, and Sanitized Fixtures

**Files:**
- Create: `custom_components/samsung_ac_windfree/models.py`
- Create: `tests/test_models.py`
- Create: `tests/fixtures/device_identity.json`
- Create: `tests/fixtures/device_state.json`
- Create: `tests/fixtures/mode_compatibility.json`

**Interfaces:**
- Produces: `Credentials`, `DeviceIdentity`, `ClimateState`, `FilterState`,
  `EnergyState`, `AlarmState`, `CapabilityContract`, `WindFreeData`,
  `UpdateSource`, and domain exception types.
- Consumes: constants from Task 1.

- [ ] **Step 1: Add failing immutable-model and redaction tests**

```python
# tests/test_models.py
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from custom_components.samsung_ac_windfree.models import (
    ClimateState,
    Credentials,
    HvacMode,
    UpdateSource,
    WindFreeData,
)


def test_credentials_are_immutable_and_exclude_universal_key() -> None:
    credentials = Credentials(
        client_key_pem="client-key",
        client_chain_pem="leaf-and-public-chain",
        not_before="2026-07-23T00:00:00+00:00",
        not_after="2036-07-23T00:00:00+00:00",
    )
    assert not hasattr(credentials, "universal_key_pem")
    with pytest.raises(FrozenInstanceError):
        credentials.client_key_pem = "replacement"  # type: ignore[misc]


def test_windfree_data_is_immutable() -> None:
    data = WindFreeData.empty()
    assert data.available is False
    assert data.update_source is UpdateSource.NONE
    assert data.climate.mode is HvacMode.COOL
    with pytest.raises(FrozenInstanceError):
        data.available = True  # type: ignore[misc]


def test_climate_state_rejects_invalid_temperature() -> None:
    with pytest.raises(ValueError, match="target temperature"):
        ClimateState(target_temperature=31.0)
```

- [ ] **Step 2: Run the model tests and observe missing types**

Run: `.venv/bin/pytest tests/test_models.py -q`

Expected: collection fails because `models.py` is absent.

- [ ] **Step 3: Implement the domain types**

```python
# custom_components/samsung_ac_windfree/models.py
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class WindFreeError(Exception):
    """Base sanitized integration error."""


class BootstrapError(WindFreeError):
    """Pinned bootstrap failed."""


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
    client_key_pem: str
    client_chain_pem: str
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
```

Create the three fixture files with synthetic identifiers only.
`device_identity.json` is:

```json
{
  "oic_d": {
    "di": "00000000-0000-4000-8000-000000000001",
    "dmv": "ocf.res.1.3.0",
    "mnmo": "AR60F12C1AWNEU",
    "rt": ["oic.d.airconditioner"]
  },
  "oic_p": {
    "mnpv": "TizenRT 4.0"
  },
  "device_0": {
    "/information/vs/0": {
      "x.com.samsung.da.description": "TP1X_DA-AC-RAC-01001_001"
    }
  }
}
```

`device_state.json` is:

```json
{
  "/power/vs/0": {
    "x.com.samsung.da.power": "Off"
  },
  "/mode/vs/0": {
    "x.com.samsung.da.modes": ["Cool"],
    "x.com.samsung.da.supportedModes": [
      "Auto", "Cool", "Dry", "Fan", "Heat"
    ]
  },
  "/temperatures/vs/0": {
    "x.com.samsung.da.items": [{
      "x.com.samsung.da.id": "0",
      "x.com.samsung.da.current": "26.0",
      "x.com.samsung.da.desired": "26.0",
      "x.com.samsung.da.minimum": "16",
      "x.com.samsung.da.maximum": "30",
      "x.com.samsung.da.increment": "1",
      "x.com.samsung.da.unit": "Celsius"
    }]
  },
  "/wind/strength/vs/0": {
    "x.com.samsung.da.modes": "0",
    "x.com.samsung.da.supportedModes": ["0", "1", "2", "3", "4"]
  },
  "/wind/direction/vs/0": {
    "x.com.samsung.da.modes": "Fix",
    "x.com.samsung.da.supportedModes": [
      "Fix", "Up_And_Low", "Left_And_Right", "All"
    ]
  },
  "/mode/convenient/vs/0": {
    "x.com.samsung.da.modes": "Off",
    "x.com.samsung.da.supportedModes": [
      "Off", "Sleep", "Quiet", "Smart", "Speed",
      "Nano", "NanoSleep", "DryComfort"
    ]
  },
  "/light/vs/0": {
    "mode": "On",
    "supportedModes": ["On", "Off"]
  },
  "/option/autoclean/vs/0": {
    "x.com.samsung.da.settingStatus": "On",
    "x.com.samsung.da.supportedSettingStatus": ["On", "Off"]
  },
  "/humidity/vs/0": {
    "x.com.samsung.da.humidity": "0",
    "x.com.samsung.da.fivepercentHumidity": "36"
  },
  "/filter/airdustfilter/vs/0": {
    "x.com.samsung.da.filterCapacity": "500",
    "x.com.samsung.da.filterUsage": "42",
    "x.com.samsung.da.filterDesiredUsage": "500",
    "x.com.samsung.da.filterStatus": "normal"
  },
  "/energy/consumption/vs/0": {
    "x.com.samsung.da.cumulativePower": "12345",
    "x.com.samsung.da.cumulativeUnit": "Wh",
    "x.com.samsung.da.cumulativePowerType": "total"
  },
  "/alarms/vs/0": {
    "x.com.samsung.da.items": [{
      "x.com.samsung.da.alarmType": "Device",
      "x.com.samsung.da.code": "ErrorCode_OFF",
      "x.com.samsung.da.state": "Deleted"
    }]
  },
  "/electriccurrent/vs/0": {
    "x.com.samsung.da.settingStatus": "Off",
    "x.com.samsung.da.level": "3"
  }
}
```

`mode_compatibility.json` starts conservatively with only the combinations
already proven during research:

```json
{
  "always_allowed": [
    "power", "hvac_mode", "display_light", "auto_clean"
  ],
  "by_mode": {
    "Auto": [],
    "Cool": ["temperature", "fan", "swing", "preset"],
    "Dry": [],
    "Fan": [],
    "Heat": []
  }
}
```

Task 12 expands this fixture only with combinations that pass the authorized
reversible live matrix. Until then, the coordinator rejects every absent
combination without device I/O. Matrix lookup uses the device's remembered
non-Off HVAC mode even while power is Off. The four `always_allowed` commands
are never mode-gated.

- [ ] **Step 4: Run the model tests**

Run: `.venv/bin/pytest tests/test_models.py -q`

Expected: `3 passed`.

- [ ] **Step 5: Commit immutable models**

```bash
git add custom_components/samsung_ac_windfree/models.py tests
git commit -m "feat: add immutable WindFree domain models"
```

---

### Task 3: Pinned Certificate Bootstrap

**Files:**
- Create: `custom_components/samsung_ac_windfree/bootstrap.py`
- Create: `tests/test_bootstrap.py`

**Interfaces:**
- Consumes: pin/timeouts from `const.py`; `Credentials`, `BootstrapError`.
- Produces:
  `async_bootstrap_credentials(hass: HomeAssistant) -> Credentials`,
  `validate_bundle(data: bytes) -> BundleMaterial`, and
  `validate_identity_certificate(der: bytes) -> str`.

- [ ] **Step 1: Add failing pin, time, key-pair, and non-persistence tests**

```python
# tests/test_bootstrap.py
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from custom_components.samsung_ac_windfree.bootstrap import (
    BootstrapInputs,
    BootstrapPins,
    PRODUCTION_PINS,
    create_credentials,
)
from custom_components.samsung_ac_windfree.models import BootstrapError


def test_wrong_bundle_digest_fails_before_parsing(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
) -> None:
    with pytest.raises(BootstrapError, match="bootstrap material changed"):
        create_credentials(
            replace(
                bootstrap_inputs,
                bundle_bytes=b"not-the-pinned-bundle",
            ),
            pins=replace(bootstrap_pins, bundle_sha256="00" * 32),
        )


def test_clock_is_checked_but_server_date_anchors_validity(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
) -> None:
    inputs = replace(
        bootstrap_inputs,
        server_date=datetime(2026, 7, 23, tzinfo=UTC),
        local_now=datetime(2026, 7, 24, tzinfo=UTC),
    )
    credentials = create_credentials(
        inputs,
        pins=bootstrap_pins,
    )
    assert credentials.not_before == "2026-07-22T23:55:00+00:00"
    assert credentials.not_after == "2036-07-23T00:00:00+00:00"


def test_clock_outside_24_hours_is_rejected(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
) -> None:
    inputs = replace(
        bootstrap_inputs,
        server_date=datetime(2026, 7, 23, tzinfo=UTC),
        local_now=datetime(2026, 7, 25, tzinfo=UTC),
    )
    with pytest.raises(BootstrapError, match="system clock"):
        create_credentials(
            inputs,
            pins=bootstrap_pins,
        )


def test_result_contains_no_universal_private_key(
    bootstrap_inputs: BootstrapInputs,
    bootstrap_pins: BootstrapPins,
) -> None:
    credentials = create_credentials(
        bootstrap_inputs,
        pins=bootstrap_pins,
    )
    assert "UNIVERSAL TEST KEY" not in credentials.client_key_pem
    assert "UNIVERSAL TEST KEY" not in credentials.client_chain_pem


def test_production_pins_are_the_approved_constants() -> None:
    assert PRODUCTION_PINS == BootstrapPins.from_constants()
```

The `bootstrap_inputs` and `bootstrap_pins` fixtures in `tests/conftest.py`
generate an ephemeral four-certificate RSA chain, a matching universal test
key, and a pinned synthetic identity leaf at test time. They return
`BootstrapInputs` and the exact SHA-256 values for those synthetic objects; no
Samsung certificate, shared UUID, or universal production key is committed.

In this same red step, add parametrized tests for malformed PEM, extra/missing
blocks, wrong RSA modulus, invalid chain signature, wrong signing fingerprint,
wrong identity leaf/SPKI, issuer/CN/OU mismatch, missing/malformed HTTP Date,
invalid SHA-1 leaf extensions, and absence of the universal key from every
serialized return value.

- [ ] **Step 2: Run the bootstrap tests and observe the missing implementation**

Run: `.venv/bin/pytest tests/test_bootstrap.py -q`

Expected: collection fails because `bootstrap.py` is absent.

- [ ] **Step 3: Implement pure validation and credential creation**

Implement these exact production types and signatures:

```python
@dataclass(frozen=True, slots=True)
class BundleMaterial:
    signing_key: rsa.RSAPrivateKey
    signing_certificate: x509.Certificate
    public_chain: tuple[x509.Certificate, ...]


@dataclass(frozen=True, slots=True)
class BootstrapInputs:
    bundle_bytes: bytes
    identity_der: bytes
    server_date: datetime
    local_now: datetime


@dataclass(frozen=True, slots=True)
class BootstrapPins:
    bundle_sha256: str
    signing_sha256: str
    identity_leaf_sha256: str
    identity_spki_sha256: str

    @classmethod
    def from_constants(cls) -> BootstrapPins:
        return cls(
            bundle_sha256=BUNDLE_SHA256,
            signing_sha256=REMOVED_SIGNING_DIGEST_NAME,
            identity_leaf_sha256=SAMSUNG_IDENTITY_LEAF_SHA256,
            identity_spki_sha256=SAMSUNG_IDENTITY_SPKI_SHA256,
        )


PRODUCTION_PINS = BootstrapPins.from_constants()


def validate_bundle(
    data: bytes,
    *,
    pins: BootstrapPins = PRODUCTION_PINS,
) -> BundleMaterial:
    """Validate exact bytes, one key, four certs, key pair, chain, and REMOVED_IDENTITY."""


def validate_identity_certificate(
    der: bytes,
    *,
    pins: BootstrapPins = PRODUCTION_PINS,
) -> str:
    """Validate leaf/SPKI/issuer/CN and return UUID from the subject OU."""


def create_credentials(
    inputs: BootstrapInputs,
    *,
    pins: BootstrapPins = PRODUCTION_PINS,
) -> Credentials:
    """Mint RSA-2048/SHA-1 leaf anchored to authenticated server Date."""
```

Use `cryptography` loaders, explicit SHA-256 byte comparisons via
`hmac.compare_digest`, RSA public-number comparison, and signature verification
for each public chain edge, including the root self-signature. RSA verification
must use the certificate's declared hash and `signature_algorithm_parameters`;
accept only `PKCS1v15` or `PSS` padding and reject missing or unsupported
parameters. Require exact DN
attributes for the four-certificate chain: `REMOVED_IDENTITY` (including
`emailAddress=REMOVED_IDENTITY`) -> `RemoteAccessCA(CE)` -> `CECA` ->
`ROOTCA`, all under `C=KR, O=Samsung Electronics`. Require the identity issuer
attributes `C=KR, O=Samsung Electronics, OU=OCF Server SubCA,
CN=Samsung Electronics OCF Server SubCA`; its subject must additionally have
`C=KR, O=Samsung Electronics, CN=*.REMOVED_HOST.com` and exactly one valid
`OU=uuid:<UUID>`. Accept canonical hyphenated UUID text in either case and
return its canonical lowercase form, while rejecting braces, missing hyphens,
duplicate OU values, or extra subject attributes. Reject extra PEM blocks.

Build the leaf with non-critical `BasicConstraints(ca=False)`; non-critical
`KeyUsage` containing only digital-signature and key-encipherment; non-critical
EKU containing client auth, server auth, and `1.3.6.1.4.1.51414.0.1.2`; and the
non-critical `1.3.6.1.4.1.51414.1.3` extension whose raw DER UTF8String value is
`b"\x0c\x10samsung.role.hub"`. Use subject `C=KR, O=Samsung Electronics,
OU=uuid:<UUID>, CN=urn:uuid:<UUID>` and SAN URI values `urn:uuid:<UUID>`,
`uri:uuid:<UUID>`, `uuid:<UUID>`, plus DNS `<UUID>`. Set
`not_before = server_date - 5 minutes`, and
`not_after = server_date.replace(year=server_date.year + 10)`. Handle a
February 29 anchor by clamping to February 28 in the target year. Serialize only
the generated key, leaf, and required public chain.

`cryptography` 48 loaders and builders perform validation and certificate
construction, but that release intentionally refuses SHA-1 certificate signing.
Build an in-memory provisional certificate containing the final TBS
subject/issuer, validity, public key, and extensions, load its DER into Home
Assistant's existing pyOpenSSL stack, and call pyOpenSSL signing with the REMOVED_IDENTITY
key and SHA-1 so it replaces the signature algorithm and value over that TBS
content. Reload and verify the final SHA-1 certificate with `cryptography`. Do
not compare raw TBS bytes because the inner signature AlgorithmIdentifier
legitimately changes. Instead fail closed unless the final and provisional
certificates have identical version, serial, subject, issuer, UTC validity
bounds, public-key SPKI bytes, and complete ordered extension
OID/critical/value tuples. Require the sole transition from provisional
SHA256-with-RSA/PKCS1v15 to final SHA1-with-RSA/PKCS1v15, including declared
parameters. A minimal bounded DER reader must reject indefinite, non-minimal,
truncated, out-of-bounds, or trailing encodings; parse the outer Certificate
SEQUENCE, TBS SEQUENCE, optional version, serial, inner signature
AlgorithmIdentifier, and outer signature AlgorithmIdentifier; and require both
raw AlgorithmIdentifiers to equal the canonical DER for
`sha1WithRSAEncryption` (`1.2.840.113549.1.1.5`) with explicit NULL parameters.
This rejects a valid outer RSA/SHA-1 signature over a TBS structure that still
declares SHA-256. Retain the cryptography outer OID/hash/PKCS1 checks, then
verify the final signature with the REMOVED_IDENTITY public key. Do not add a DER dependency
or use temporary files, shell commands, or subprocess OpenSSL. Tests must mutate
every protected profile category at the conversion boundary, compare every
intended TBS field after re-signing, prove canonical inner/outer algorithms,
prove the SHA-1 signature and chain validity, and prove that credential minting
writes no files.

`create_credentials` composes `validate_bundle(inputs.bundle_bytes,
pins=pins)`, `validate_identity_certificate(inputs.identity_der, pins=pins)`,
clock validation, and leaf minting in that order. Production callers never pass
the `pins` argument; only synthetic tests inject `bootstrap_pins`.

- [ ] **Step 4: Implement bounded asynchronous fetches**

Add:

```python
async def async_fetch_bundle(
    session: aiohttp.ClientSession,
) -> tuple[bytes, datetime]:
    """Fetch via normal PKI, require HTTP Date, size <= 64 KiB, and status 200."""


async def async_fetch_identity_der(
    hass: HomeAssistant,
) -> bytes:
    """Fetch untrusted TLS leaf in executor; validation happens before use."""


async def async_bootstrap_credentials(
    hass: HomeAssistant,
) -> Credentials:
    """Run both bounded fetches and CPU certificate work in the executor."""
```

The bundle fetch uses `asyncio.timeout(HTTPS_TIMEOUT)`, calls the fixed URL with
redirects disabled, and requires the response URL to equal that same parsed
HTTPS URL and expected host before trusting its status, Date, or body. Cross-host,
HTTPS-to-HTTP, and same-host redirects all fail closed. The identity fetch uses
a socket timeout of `HTTPS_TIMEOUT`, SNI, `CERT_NONE`, and returns only DER.
Neither helper logs URL response bodies, certificate details, UUID, or host
addresses. Translate all failures into the fixed sanitized categories:
`bootstrap_unavailable`, `bootstrap_pin_mismatch`, `invalid_clock`, and
`bootstrap_invalid_material`. Raise sanitized errors only after leaving the
handling `except` block, with no cause or context retaining external errors,
addresses, URLs, payloads, or key objects. Sensitive internal work must unwind
through result-or-fixed-category helpers. Before a public boundary raises a
fresh `BootstrapError`, it must explicitly delete or replace bundle bytes,
identity DER, universal signing-key objects, bootstrap inputs, sessions, and
other sensitive intermediates in that public frame. Tests walk only production
bootstrap traceback frames and require the exact injected bytes, objects,
content, and representations to be absent; caller/test frames are outside this
guarantee. Preserve the original `CancelledError` object unchanged and scrub
sensitive production locals before re-raising cancellation.
Apply that same boundary to both fetch helpers: partial streamed bundle chunks,
response/session objects, parsed URLs and dates, Home Assistant/executor job
objects, returned DER, and raw failures must unwind inside private
result-or-fixed-category helpers. Direct tests must cover oversize bodies,
stream failures after a private-key-like chunk, invalid status/URL/Date, and
cancellation after a partial chunk, and a parametrized audit must exercise every
public bootstrap function on ordinary failure plus every async public function
on cancellation.

- [ ] **Step 5: Run focused and full bootstrap tests**

Run: `.venv/bin/pytest tests/test_bootstrap.py -q`

Expected: all bootstrap cases pass, including malformed PEM, wrong block count,
wrong modulus, invalid chain, leaf pin, SPKI pin, issuer/CN/OU, missing Date,
clock skew, SHA-1 leaf properties, and no universal-key persistence.

- [ ] **Step 6: Commit bootstrap**

```bash
git add custom_components/samsung_ac_windfree/bootstrap.py \
  tests/test_bootstrap.py tests/conftest.py
git commit -m "feat: add pinned WindFree certificate bootstrap"
```

---

### Task 4: Executor-Safe DTLS Transport

**Files:**
- Create: `custom_components/samsung_ac_windfree/transport.py`
- Create: `tests/test_dependency_contract.py`
- Create: `tests/test_transport.py`

**Interfaces:**
- Consumes: `Credentials`, port/rate/timeout constants.
- Produces:
  `WindFreeTransport.async_connect()`,
  `async_get(path)`,
  `async_post(path, payload)`,
  `async_observe(paths, callback)`,
  `async_close()`, and
  `async_discover_transport(...)`.

- [ ] **Step 1: Add failing lifecycle, pacing, callback, and redaction tests**

```python
# tests/test_transport.py
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import cbor2
import pytest

from custom_components.samsung_ac_windfree.transport import WindFreeTransport


async def test_get_runs_blocking_session_in_executor(
    hass, credentials
) -> None:
    session = MagicMock()
    session.get.return_value = (69, cbor2.dumps({"value": "payload"}))
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_: session,
    )
    await transport.async_connect()
    assert await transport.async_get("/oic/d") == {"value": "payload"}
    session.get.assert_called_once_with(("oic", "d"))


async def test_old_generation_notification_is_ignored(
    hass, credentials
) -> None:
    received: list[tuple[str, dict[str, object]]] = []
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        generation=2,
        session_factory=MagicMock,
    )
    callback = transport.threadsafe_callback(
        generation=1,
        target=lambda path, body: received.append((path, body)),
    )
    callback("/power/vs/0", cbor2.dumps({"value": "old"}))
    await asyncio.sleep(0)
    assert received == []


async def test_error_does_not_expose_host_or_payload(
    hass, credentials
) -> None:
    session = MagicMock()
    session.get.side_effect = RuntimeError(
        "failed at 192.0.2.10 with secret-payload"
    )
    transport = WindFreeTransport(
        hass,
        host="192.0.2.10",
        port=49154,
        credentials=credentials,
        session_factory=lambda **_: session,
    )
    await transport.async_connect()
    with pytest.raises(Exception) as err:
        await transport.async_get("/oic/d")
    assert "192.0.2.10" not in str(err.value)
    assert "secret-payload" not in str(err.value)
```

In this same red step, add tests for real in-memory PEM constructor arguments,
two-RPS delegation, Block2 GET passthrough, OBSERVE registration/deregistration,
all allowed fatal-alert codes, unknown-alert transience, sequential nine-port
sweep, successful-session reuse, close/join deadlines, and no callback after
close.

- [ ] **Step 2: Run the tests and observe the missing transport**

Run: `.venv/bin/pytest tests/test_transport.py -q`

Expected: collection fails because `transport.py` is absent.

- [ ] **Step 3: Implement the transport adapter**

Implement:

```python
Representation = Mapping[str, object]
NotificationCallback = Callable[[int, str, Representation], None]
SessionFactory = Callable[..., DtlsCoapSession]


class WindFreeTransport:
    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        credentials: Credentials,
        *,
        generation: int = 0,
        handshake_timeout: float = RUNTIME_HANDSHAKE_TIMEOUT,
        session_factory: SessionFactory = DtlsCoapSession,
    ) -> None: ...

    async def async_connect(self) -> None: ...
    async def async_get(self, path: str) -> Representation: ...
    async def async_post(
        self,
        path: str,
        payload: Representation,
    ) -> None: ...
    async def async_observe(
        self,
        paths: tuple[str, ...],
        callback: NotificationCallback,
    ) -> None: ...
    def threadsafe_callback(
        self,
        *,
        generation: int,
        target: Callable[[str, Representation], None],
    ) -> Callable[[str, bytes], None]: ...
    async def async_close(self) -> None: ...
```

Construct `DtlsCoapSession` with `cert_pem`, `key_pem`, the exact port, and
`rate_limit_rps=RATE_LIMIT_RPS`. Convert paths with
`tuple(segment for segment in path.split("/") if segment)`. Treat only CoAP
`2.05` as GET success and `2.04` as POST acknowledgement. Close and join with
bounded executor calls. Store the loop captured from Home Assistant and use
`loop.call_soon_threadsafe`; drop callbacks whose generation does not equal the
active generation or after close.

`async_observe` wraps the dependency's raw two-argument `(path, payload_bytes)`
callback with `threadsafe_callback`; only the wrapped event-loop callback adds
the transport generation and calls the three-argument `NotificationCallback`.

`WindFreeTransport` exclusively owns CBOR conversion: `async_get` and OBSERVE
decode dependency-returned bytes with `cbor2.loads`, while `async_post` encodes
the supplied representation with `cbor2.dumps`. Parser, coordinator, and entity
layers never exchange raw CBOR bytes.

- [ ] **Step 4: Implement exact-range discovery**

```python
async def async_discover_transport(
    hass: HomeAssistant,
    host: str,
    credentials: Credentials,
    *,
    ports: tuple[int, ...] = PROBE_PORTS,
) -> tuple[int, WindFreeTransport]:
    """Sequentially probe nine ports and return the first authenticated one."""
```

Each attempt uses `PROBE_HANDSHAKE_TIMEOUT`, closes before the next attempt, and
reuses the successful session. Exhaustion raises sanitized `ConnectionError`
without host or port payload details.

- [ ] **Step 5: Run transport tests**

Add real-dependency tests in `tests/test_dependency_contract.py`:

```python
from __future__ import annotations

import ssl

from OpenSSL import SSL
from smartthings_local.protocol.coap import (
    ACCEPT,
    BLOCK2,
    CF_CBOR,
    OBSERVE,
    OBSERVE_REGISTER,
    build_coap,
    parse_coap,
)
from smartthings_local.protocol.dtls_session import _load_pem_chain


def test_real_dependency_loads_sha1_chain_without_global_tls_change(
    credentials,
) -> None:
    before = ssl.create_default_context().security_level
    ctx = SSL.Context(SSL.DTLS_METHOD)
    ctx.set_cipher_list(
        b"ECDHE-ECDSA-AES128-GCM-SHA256:@SECLEVEL=0"
    )
    _load_pem_chain(
        ctx,
        credentials.client_chain_pem,
        credentials.client_key_pem,
    )
    assert ssl.create_default_context().security_level == before


def test_real_codec_preserves_observe_and_block2_options() -> None:
    packet = build_coap(
        0,
        1,
        0x1234,
        b"\x41",
        [
            (OBSERVE, OBSERVE_REGISTER),
            (ACCEPT, CF_CBOR),
            (BLOCK2, b""),
        ],
    )
    _, _, _, token, options, _ = parse_coap(packet)
    assert token == b"\x41"
    assert (OBSERVE, OBSERVE_REGISTER) in options
    assert (BLOCK2, b"") in options
```

Run:
`.venv/bin/pytest tests/test_transport.py
tests/test_dependency_contract.py -q`

Expected: lifecycle, in-memory PEM, two-RPS pacing delegation, Block2 passthrough,
generation isolation, sequential sweep, cleanup, fatal-alert extraction, and
redaction tests pass. The real dependency loads the synthetic SHA-1 chain and
its codec contract passes without changing a fresh stdlib TLS context.

- [ ] **Step 6: Commit transport**

```bash
git add custom_components/samsung_ac_windfree/transport.py \
  tests/test_transport.py tests/test_dependency_contract.py
git commit -m "feat: add local DTLS CoAP transport"
```

---

### Task 5: Resource Parsers and Verified Request Builders

**Files:**
- Create: `custom_components/samsung_ac_windfree/device.py`
- Create: `tests/test_device.py`

**Interfaces:**
- Consumes: immutable models and sanitized CBOR-decoded mappings.
- Produces:
  `parse_identity(...)`,
  `parse_device_state(...)`,
  `parse_humidity(...)`,
  `validate_contract(...)`,
  `build_command(...)`, and
  `verify_command(...)`.

- [ ] **Step 1: Add failing parser and exact-path tests**

```python
# tests/test_device.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.samsung_ac_windfree.device import (
    CommandKind,
    build_command,
    parse_humidity,
    parse_identity,
)
from custom_components.samsung_ac_windfree.models import UnsupportedDevice


def fixture(name: str) -> dict:
    return json.loads(Path(f"tests/fixtures/{name}").read_text())


def test_identity_requires_exact_consumer_model() -> None:
    payload = fixture("device_identity.json")
    payload["oic_d"]["mnmo"] = "AR60F12C1AWOTHER"
    with pytest.raises(UnsupportedDevice):
        parse_identity(
            payload["oic_d"],
            payload["oic_p"],
            payload["device_0"],
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("36", 36), ("0", None), (0, None), ("101", None), (True, None)],
)
def test_humidity_is_direct_percentage_with_zero_sentinel(
    raw, expected
) -> None:
    assert parse_humidity(raw) == expected


def test_power_uses_vendor_path() -> None:
    command = build_command(CommandKind.POWER, True)
    assert command.path == "/power/vs/0"
    assert command.payload == {"x.com.samsung.da.power": "On"}


def test_temperature_builder_requires_fresh_aggregate() -> None:
    with pytest.raises(ValueError, match="fresh aggregate"):
        build_command(CommandKind.TEMPERATURE, 27.0)
```

- [ ] **Step 2: Run the tests and observe missing parsers**

Run: `.venv/bin/pytest tests/test_device.py -q`

Expected: collection fails because `device.py` is absent.

- [ ] **Step 3: Implement typed parsers**

Implement exact path constants for the protocol table and:

```python
class CommandKind(StrEnum):
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
    kind: CommandKind
    path: str
    payload: Mapping[str, object]
    requested: object
    related_paths: tuple[str, ...]


def parse_identity(
    oic_d: Mapping[str, object],
    oic_p: Mapping[str, object],
    device_0: Mapping[str, object],
) -> DeviceIdentity: ...


def parse_device_state(
    resources: Mapping[str, Mapping[str, object]],
    previous: WindFreeData,
) -> WindFreeData: ...


def validate_contract(
    identity: DeviceIdentity,
    resources: Mapping[str, Mapping[str, object]],
    compatibility: Mapping[str, object],
) -> CapabilityContract: ...
```

Validate the four identity gates. Ignore unknown fields. Parse humidity zero as
unknown, Wh as finite non-negative kWh, filter ratio with zero-capacity guard,
alarm/filter distinctions, and opaque current-limit values without units.

- [ ] **Step 4: Implement request builders and equivalence verification**

```python
def build_command(
    kind: CommandKind,
    value: object,
    *,
    fresh_aggregate: Mapping[str, object] | None = None,
) -> DeviceCommand: ...


def verify_command(
    command: DeviceCommand,
    resources: Mapping[str, Mapping[str, object]],
) -> bool: ...
```

Use only the live paths and payloads from the approved table. Temperature must
copy the fresh aggregate and alter only the desired item. `AUTO` writes
Samsung `Auto`; verification accepts `Auto` or `AI Auto`. Never build commands
for purification, mute, instant watts, Freeze Wash, timers, self-diagnosis,
current-limit writes, motion, or welcome cooling.

- [ ] **Step 5: Run parser/request tests**

Run: `.venv/bin/pytest tests/test_device.py -q`

Expected: exact identity, all mappings, malformed data, energy reset input,
humidity sentinel, vendor power path, temperature preservation, Auto alias,
excluded controls, and contract-drift tests pass.

- [ ] **Step 6: Commit device contract**

```bash
git add custom_components/samsung_ac_windfree/device.py \
  tests/test_device.py tests/fixtures
git commit -m "feat: model exact WindFree resource contract"
```

---

### Task 6: Session Supervisor, Scheduler, and Coordinator

**Files:**
- Create: `custom_components/samsung_ac_windfree/coordinator.py`
- Create: `tests/test_coordinator.py`
- Modify: `custom_components/samsung_ac_windfree/transport.py`
- Modify: `tests/test_transport.py`

**Interfaces:**
- Consumes: transport, parser/builders, immutable models.
- Produces:
  `WindFreeCoordinator.async_start()`,
  `async_shutdown()`,
  `async_command(kind, value)`,
  `async_reconcile()`, and coordinator data.

- [ ] **Step 1: Add failing generation, scheduling, and write tests**

```python
# tests/test_coordinator.py
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.samsung_ac_windfree.device import CommandKind
from custom_components.samsung_ac_windfree.models import CommandRejected


async def test_three_failures_trigger_resweep(
    coordinator, transport_factory
) -> None:
    transport_factory.current.async_get.side_effect = TimeoutError
    await coordinator.async_run_hot_cycle()
    await coordinator.async_run_hot_cycle()
    await coordinator.async_run_hot_cycle()
    transport_factory.discover.assert_not_awaited()
    transport_factory.reconnect.side_effect = ConnectionError
    await coordinator.async_run_reconnect_attempt()
    await coordinator.async_run_reconnect_attempt()
    transport_factory.discover.assert_not_awaited()
    await coordinator.async_run_reconnect_attempt()
    transport_factory.discover.assert_awaited_once()


async def test_hot_failures_alone_never_resweep(
    coordinator, transport_factory
) -> None:
    transport_factory.current.async_get.side_effect = TimeoutError
    await coordinator.async_run_hot_cycle()
    await coordinator.async_run_hot_cycle()
    await coordinator.async_run_hot_cycle()
    transport_factory.discover.assert_not_awaited()


async def test_old_generation_observe_is_ignored(coordinator) -> None:
    before = coordinator.data
    coordinator.handle_observe(
        generation=coordinator.generation - 1,
        path="/power/vs/0",
        representation={"x.com.samsung.da.power": "On"},
    )
    assert coordinator.data is before


async def test_temperature_command_gets_fresh_aggregate_under_lock(
    coordinator,
) -> None:
    await coordinator.async_command(CommandKind.TEMPERATURE, 27.0)
    assert coordinator.transport.async_get.await_args_list[0].args == (
        "/temperatures/vs/0",
    )
    coordinator.transport.async_post.assert_awaited_once()


async def test_changed_without_matching_readback_is_rejected(
    coordinator,
) -> None:
    coordinator.transport.async_post = AsyncMock(return_value=None)
    coordinator.transport.async_get = AsyncMock(
        return_value={"x.com.samsung.da.power": "Off"}
    )
    with pytest.raises(CommandRejected):
        await coordinator.async_command(CommandKind.POWER, True)
```

- [ ] **Step 2: Run coordinator tests and observe the missing coordinator**

Run: `.venv/bin/pytest tests/test_coordinator.py -q`

Expected: collection fails because `coordinator.py` is absent.

- [ ] **Step 3: Implement coordinator lifecycle and immutable publication**

Subclass `DataUpdateCoordinator[WindFreeData]` and implement:

```python
class WindFreeCoordinator(DataUpdateCoordinator[WindFreeData]):
    def __init__(
        self,
        hass: HomeAssistant,
        *,
        host: str,
        port: int,
        credentials: Credentials,
        compatibility: Mapping[str, object],
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport_factory: TransportFactory | None = None,
    ) -> None: ...

    async def async_start(self) -> None: ...
    async def async_shutdown(self) -> None: ...
    async def async_reconcile(self) -> None: ...
    async def async_run_hot_cycle(self) -> None: ...
    async def async_run_reconnect_attempt(self) -> None: ...
    async def async_command(
        self,
        kind: CommandKind,
        value: object,
    ) -> None: ...
    def handle_observe(
        self,
        generation: int,
        path: str,
        representation: Mapping[str, object],
    ) -> None: ...

    @property
    def generation(self) -> int: ...

    @property
    def transport(self) -> WindFreeTransport: ...
```

Start one transport generation, seed `/oic/d`, `/oic/p`, and `/device/0`,
validate identity/contract, subscribe to hot/warm paths, and publish one new
immutable `WindFreeData`. Shutdown cancels scheduler/reconnect tasks, rejects
new commands, deregisters OBSERVE, closes the exact generation, and prevents
resurrection.

- [ ] **Step 4: Implement scheduler budget and OBSERVE merge**

Use a monotonic min-heap of resource deadlines. Stagger initial deadlines and
admit requests in order: command verification, overdue hot, warm, cold, then
full reconciliation. A `/device/0` Block2 operation is atomic once admitted.
OBSERVE updates current-generation cache and moves that resource deadline.
Every five minutes reconcile `/oic/d`, `/oic/p`, and `/device/0`. Track measured
hot-resource age and request-latency buckets.

- [ ] **Step 5: Implement reconnect and fatal-auth classification**

After three consecutive hot failures, mark unavailable, close the generation,
and reconnect with 2–60 second exponential backoff. After three stored-port
connection failures, call exact-range discovery. Require an allowed fatal DTLS
alert or allowed CoAP authorization code to repeat on a fresh generation before
raising `AuthenticationRejected`; all other errors remain transient.

If three fresh generations authenticate successfully but each dies within ten
seconds without an allowed fatal-auth signal, set the sanitized diagnostic
reason `possible_competing_session`; continue normal backoff and never start
reauth from that heuristic alone. Add a fake-clock test for this transition and
for clearing the reason after a stable generation.

- [ ] **Step 6: Implement serialized authoritative commands**

Under one `asyncio.Lock`: validate mode compatibility, fresh-GET aggregate
writes, POST, wait briefly for matching same-generation OBSERVE, GET if needed,
verify, refresh related paths, and only then publish. Implement Off-to-mode as
mode-write/read-back then power-write/read-back. `turn_on` powers the remembered
mode; `turn_off` changes power only. Disable affected writes on resource drift
and all writes on identity drift.

- [ ] **Step 7: Run coordinator and transport tests**

Run:
`.venv/bin/pytest tests/test_coordinator.py tests/test_transport.py -q`

Expected: scheduler arithmetic, priority, Block2 admission, generation
isolation, OBSERVE, retry, resweep, auth classification, exact reconciliation,
command verification, coercion, partial failure, cancellation, and unload tests
pass.

- [ ] **Step 8: Commit coordinator**

```bash
git add custom_components/samsung_ac_windfree/coordinator.py \
  custom_components/samsung_ac_windfree/transport.py \
  tests/test_coordinator.py tests/test_transport.py
git commit -m "feat: coordinate WindFree state and verified commands"
```

---

### Task 7: Config Flow, Reconfigure, Reauth, and Entry Lifecycle

**Files:**
- Create: `custom_components/samsung_ac_windfree/config_flow.py`
- Modify: `custom_components/samsung_ac_windfree/__init__.py`
- Create: `tests/test_config_flow.py`
- Expand: `tests/test_init.py`

**Interfaces:**
- Consumes: bootstrap, discovery, coordinator.
- Produces: host-only flow, config-entry schema, progress tasks, reconfigure,
  reauth, setup/unload/reload.

- [ ] **Step 1: Add failing host-only flow and lifecycle tests**

```python
# tests/test_config_flow.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.samsung_ac_windfree.const import DOMAIN


async def test_user_flow_requires_only_host(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    schema_keys = {
        marker.schema
        for marker in result["data_schema"].schema
    }
    assert schema_keys == {"host"}


async def test_success_uses_progress_then_creates_entry(
    hass, validated_setup
) -> None:
    with patch(
        "custom_components.samsung_ac_windfree.config_flow."
        "async_validate_setup",
        new=AsyncMock(return_value=validated_setup),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={"host": "ac.example.test"},
        )
        assert result["type"] is FlowResultType.PROGRESS
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"]
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["host"] == "ac.example.test"
    assert "client_key_pem" in result["data"]
```

In this same red step, add tests for DNS/fetch/sweep/read/overall timeouts,
progress cancellation cleanup, duplicate abort, exact-model and capability
rejection, reconfigure unique-ID mismatch, failed reauth preserving old
credentials, successful atomic reauth, offline startup proving bootstrap is not
called, certificate-expiry Repairs, expired-certificate reauth, setup/unload,
reload, and version-1 migration.

- [ ] **Step 2: Run config flow tests and observe missing flow**

Run: `.venv/bin/pytest tests/test_config_flow.py -q`

Expected: flow handler is missing.

- [ ] **Step 3: Implement bounded validation and progress flow**

Implement:

```python
@dataclass(frozen=True, slots=True)
class ValidatedSetup:
    host: str
    port: int
    identity: DeviceIdentity
    credentials: Credentials


async def async_validate_setup(
    hass: HomeAssistant,
    host: str,
) -> ValidatedSetup:
    """Resolve in 5 s, bootstrap, sweep, read identity, validate contract."""
```

Wrap the complete coroutine in `asyncio.timeout(SETUP_TIMEOUT)`. Use an
HA progress step and cancellation flag. Budget DNS 5, HTTPS 30, sweep 54, and
identity reads 24 seconds; reuse the successful swept session. Map only the
specified sanitized error keys.

Implement `ConfigFlow` user, progress completion, unique ID, duplicate abort,
reconfigure requiring the same device ID, and reauth requiring confirmation.
Reauth validates new credentials before one atomic
`async_update_entry`; failure leaves old data unchanged.

- [ ] **Step 4: Implement entry setup and unload**

```python
type WindFreeConfigEntry = ConfigEntry[WindFreeCoordinator]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WindFreeConfigEntry,
) -> bool: ...


async def async_unload_entry(
    hass: HomeAssistant,
    entry: WindFreeConfigEntry,
) -> bool: ...
```

Construct credentials and coordinator from entry data without bootstrap or
internet. On repeated fatal auth, initiate reauth. Forward `PLATFORMS`; unload
platforms then shut down coordinator. Add update listener for reload. Add
versioned migration support starting at config-entry version `1`.

Before constructing transport, parse the stored certificate validity dates.
Create the expiry Repairs issue at 90 days; an already expired certificate
starts reauth without attempting an infinite reconnect. This startup check uses
stored data only and performs no device or internet I/O.

- [ ] **Step 5: Run flow and lifecycle tests**

Run:
`.venv/bin/pytest tests/test_config_flow.py tests/test_init.py -q`

Expected: success, every timeout/error class, cancellation cleanup, duplicate,
exact-model failure, reconfigure identity mismatch, atomic reauth, offline
startup, setup, unload, reload, and migration tests pass.

- [ ] **Step 6: Commit setup flows**

```bash
git add custom_components/samsung_ac_windfree/config_flow.py \
  custom_components/samsung_ac_windfree/__init__.py \
  tests/test_config_flow.py tests/test_init.py
git commit -m "feat: add zero-input WindFree config flow"
```

---

### Task 8: Climate Entity

**Files:**
- Create: `custom_components/samsung_ac_windfree/entity.py`
- Create: `custom_components/samsung_ac_windfree/climate.py`
- Create: `tests/test_climate.py`

**Interfaces:**
- Consumes: coordinator data and `async_command`.
- Produces: one primary climate entity with all approved mappings.

- [ ] **Step 1: Add failing state and command tests**

```python
# tests/test_climate.py
from __future__ import annotations

from homeassistant.components.climate import HVACMode

from custom_components.samsung_ac_windfree.device import CommandKind
from custom_components.samsung_ac_windfree.models import HvacMode


async def test_climate_exposes_local_state(hass, climate_entity) -> None:
    state = hass.states.get(climate_entity)
    assert state.state == HVACMode.OFF
    assert state.attributes["current_temperature"] == 26
    assert state.attributes["temperature"] == 26
    assert state.attributes["current_humidity"] == 36
    assert state.attributes["fan_modes"] == [
        "auto", "low", "medium", "high", "turbo"
    ]
    assert state.attributes["swing_modes"] == [
        "fixed", "vertical", "horizontal", "both"
    ]


async def test_setting_heat_while_off_uses_one_logical_command(
    hass, climate_entity, command_mock
) -> None:
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": climate_entity, "hvac_mode": HVACMode.HEAT},
        blocking=True,
    )
    command_mock.assert_awaited_once_with(
        CommandKind.HVAC_MODE,
        HvacMode.HEAT,
    )
```

- [ ] **Step 2: Run climate tests and observe the missing platform**

Run: `.venv/bin/pytest tests/test_climate.py -q`

Expected: climate platform is absent.

- [ ] **Step 3: Implement shared entity and climate**

`WindFreeEntity` extends `CoordinatorEntity` and exposes DeviceInfo using exact
model, manufacturer Samsung, firmware, and platform. Never include UUID as
serial or identifier beyond the required HA device identifier tuple.

Unique IDs are fixed: the climate entity uses the bare OCF `device_id`; every
additional entity uses `f"{device_id}_{entity_key}"`. The same OCF `device_id`
is the HA device identifier value under the integration domain.

`WindFreeClimate` implements:

- `HVACMode.OFF/AUTO/COOL/DRY/FAN_ONLY/HEAT`
- target temperature `16–30 °C`, step `1`
- current temperature and direct current humidity
- fan `auto/low/medium/high/turbo`
- one combined swing property `fixed/vertical/horizontal/both`
- presets `none/quiet/smart/boost/windfree/windfree_sleep/sleep/dry_comfort`
- `TARGET_TEMPERATURE`, `FAN_MODE`, `SWING_MODE`, `PRESET_MODE`, `TURN_ON`,
  and `TURN_OFF` feature bits
- no `hvac_action`

Every async method delegates once to coordinator command methods. Properties
only read immutable data. Unsupported matrix combinations raise translated
`HomeAssistantError` without transport calls.

- [ ] **Step 4: Run climate tests**

Run: `.venv/bin/pytest tests/test_climate.py -q`

Expected: all mappings, features, Auto alias, power/mode sequencing delegation,
temperature limits, availability, coercion error, and no-I/O property tests
pass.

- [ ] **Step 5: Commit climate**

```bash
git add custom_components/samsung_ac_windfree/entity.py \
  custom_components/samsung_ac_windfree/climate.py \
  tests/test_climate.py
git commit -m "feat: expose WindFree climate controls"
```

---

### Task 9: Switches, Sensors, and Binary Sensors

**Files:**
- Create: `custom_components/samsung_ac_windfree/switch.py`
- Create: `custom_components/samsung_ac_windfree/sensor.py`
- Create: `custom_components/samsung_ac_windfree/binary_sensor.py`
- Create: `tests/test_switch.py`
- Create: `tests/test_sensor.py`
- Create: `tests/test_binary_sensor.py`

**Interfaces:**
- Consumes: immutable coordinator data and command methods.
- Produces: exactly the additional entities in the approved design.

- [ ] **Step 1: Add failing entity inventory and semantics tests**

```python
async def test_enabled_entity_inventory(
    hass, entity_registry, setup_integration
) -> None:
    enabled = {
        entry.unique_id
        for entry in entity_registry.entities.values()
        if not entry.disabled
    }
    assert enabled == {
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000001_auto_clean",
        "00000000-0000-4000-8000-000000000001_display_light",
        "00000000-0000-4000-8000-000000000001_filter_usage",
        "00000000-0000-4000-8000-000000000001_filter_status",
        "00000000-0000-4000-8000-000000000001_filter_attention",
        "00000000-0000-4000-8000-000000000001_energy_consumption",
        "00000000-0000-4000-8000-000000000001_problem",
        "00000000-0000-4000-8000-000000000001_active_alarm",
    }
    disabled = {
        entry.unique_id
        for entry in entity_registry.entities.values()
        if entry.disabled
    }
    assert disabled == {
        "00000000-0000-4000-8000-000000000001_current_limit_enabled",
        "00000000-0000-4000-8000-000000000001_current_limit_level",
    }


async def test_energy_is_total_increasing_kwh(hass, energy_entity) -> None:
    state = hass.states.get(energy_entity)
    assert state is not None
    assert state.state == "12.345"
    assert state.attributes["device_class"] == "energy"
    assert state.attributes["state_class"] == "total_increasing"
    assert state.attributes["unit_of_measurement"] == "kWh"
```

Place inventory tests in `tests/test_sensor.py`; add switch command delegation
and binary alarm/filter combination cases in their respective files.

- [ ] **Step 2: Run entity tests and observe missing platforms**

Run:
`.venv/bin/pytest tests/test_switch.py tests/test_sensor.py
tests/test_binary_sensor.py -q`

Expected: the three platforms are absent.

- [ ] **Step 3: Implement switches**

Create `auto_clean` and `display_light` switches enabled by default. Their
properties read coordinator booleans; turn-on/off delegates to
`CommandKind.AUTO_CLEAN` or `DISPLAY_LIGHT` and relies on authoritative
verification.

- [ ] **Step 4: Implement sensors**

Create:

- filter usage percent, guarded and clamped only after valid used/capacity
- filter status enum-like translated sensor
- cumulative kWh energy with `SensorDeviceClass.ENERGY`,
  `SensorStateClass.TOTAL_INCREASING`, and Wh-preserving precision
- active alarm diagnostic sensor
- current-limit level diagnostic sensor disabled by default

Publish a decreased non-negative energy value unchanged so HA statistics can
recognize reset. Invalid/negative/non-finite/overflow values are unavailable.

- [ ] **Step 5: Implement binary sensors**

Create filter attention, non-filter problem, and current-limit-enabled
diagnostic entities. Current-limit entities are disabled by default. Never
expose alarm timestamps or opaque payloads.

- [ ] **Step 6: Run all additional entity tests**

Run:
`.venv/bin/pytest tests/test_switch.py tests/test_sensor.py
tests/test_binary_sensor.py -q`

Expected: inventory, category/default-enable, state semantics, reset,
availability, commands, and no-I/O tests pass.

- [ ] **Step 7: Commit platforms**

```bash
git add custom_components/samsung_ac_windfree/switch.py \
  custom_components/samsung_ac_windfree/sensor.py \
  custom_components/samsung_ac_windfree/binary_sensor.py \
  tests/test_switch.py tests/test_sensor.py tests/test_binary_sensor.py
git commit -m "feat: add WindFree settings and diagnostic entities"
```

---

### Task 10: Repairs, Diagnostics, and Privacy

**Files:**
- Create: `custom_components/samsung_ac_windfree/diagnostics.py`
- Modify: `custom_components/samsung_ac_windfree/coordinator.py`
- Create: `tests/test_diagnostics.py`
- Expand: `tests/test_coordinator.py`

**Interfaces:**
- Consumes: coordinator health counters and config-entry metadata.
- Produces: zero-I/O diagnostics allowlist and Repairs lifecycle.

- [ ] **Step 1: Add failing adversarial privacy tests**

```python
# tests/test_diagnostics.py
from __future__ import annotations

from custom_components.samsung_ac_windfree.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_are_allowlisted_and_zero_io(
    hass, config_entry, coordinator, setup_integration
) -> None:
    coordinator.transport.async_get.reset_mock()
    result = await async_get_config_entry_diagnostics(hass, config_entry)
    assert set(result) == {
        "integration_version",
        "dependency_version",
        "supported_product",
        "connection",
        "updates",
        "resource_coverage",
        "certificate",
        "entity_support",
    }
    coordinator.transport.async_get.assert_not_called()


async def test_adversarial_secrets_do_not_escape(
    hass, config_entry, coordinator, setup_integration
) -> None:
    secrets = (
        "192.0.2.10",
        "00000000-0000-4000-8000-000000000001",
        "PRIVATE KEY",
        "AA:BB:CC:DD:EE:FF",
    )
    result = await async_get_config_entry_diagnostics(hass, config_entry)
    rendered = repr(result)
    assert all(secret not in rendered for secret in secrets)
```

- [ ] **Step 2: Run privacy tests and observe missing diagnostics**

Run: `.venv/bin/pytest tests/test_diagnostics.py -q`

Expected: diagnostics module is absent.

- [ ] **Step 3: Implement the explicit diagnostics allowlist**

Return only integration/dependency version, exact supported-product label,
generation and sanitized connection state, update source/age, poll/OBSERVE
health, failure/reconnect counts, latency buckets, resource coverage flags,
certificate dates/days-to-expiry, and entity support flags. Never copy config
entry data or arbitrary coordinator dictionaries.

- [ ] **Step 4: Implement Repairs issue lifecycle**

Use stable issue IDs for:

- `authentication_rejected`
- `certificate_expiring`
- `bootstrap_pin_changed`
- `bootstrap_unavailable`
- `resource_contract_changed`
- `unsupported_identity_after_update`
- `port_range_exhausted`

Create/delete issues on state transitions. Place no host, ID, certificate,
resource payload, or alarm code in placeholders. Expiry issue starts at 90
days. Resource drift disables only affected controls; identity drift disables
all writes.

- [ ] **Step 5: Run privacy and coordinator tests**

Run:
`.venv/bin/pytest tests/test_diagnostics.py tests/test_coordinator.py -q`

Expected: adversarial redaction, zero-I/O, expiry, drift, auth, recovery, and
issue-deletion tests pass.

- [ ] **Step 6: Commit diagnostics**

```bash
git add custom_components/samsung_ac_windfree/diagnostics.py \
  custom_components/samsung_ac_windfree/coordinator.py \
  tests/test_diagnostics.py tests/test_coordinator.py
git commit -m "feat: add private diagnostics and repairs"
```

---

### Task 11: Translations, Documentation, and Quality Metadata

**Files:**
- Create: `custom_components/samsung_ac_windfree/strings.json`
- Create: `custom_components/samsung_ac_windfree/translations/en.json`
- Create: `custom_components/samsung_ac_windfree/quality_scale.yaml`
- Create: `README.md`
- Create: `CHANGELOG.md`
- Create: `LICENSE`
- Expand: `tests/test_init.py`

**Interfaces:**
- Consumes: every user-visible key from Tasks 7–10.
- Produces: complete English UI, HACS documentation, Silver-rule checklist.

- [ ] **Step 1: Add failing metadata consistency tests**

```python
def test_translation_mirrors_strings() -> None:
    strings = json.loads(
        Path(
            "custom_components/samsung_ac_windfree/strings.json"
        ).read_text()
    )
    english = json.loads(
        Path(
            "custom_components/samsung_ac_windfree/translations/en.json"
        ).read_text()
    )
    assert english == strings


def test_readme_documents_security_and_scope() -> None:
    readme = Path("README.md").read_text()
    for phrase in (
        "AR60F12C1AWNEU",
        "fully local after setup",
        "unofficial certificate",
        "Home Assistant backups",
        "SmartThings is not required",
    ):
        assert phrase in readme
```

- [ ] **Step 2: Run metadata tests and observe absent documentation**

Run: `.venv/bin/pytest tests/test_init.py -q`

Expected: files are missing.

- [ ] **Step 3: Write strings and translations**

Define translated config-flow titles/descriptions/errors for every Task 7
category, entity names/states for Tasks 8–9, command errors, and all Repairs
issues from Task 10. Copy the exact JSON object to `translations/en.json`.
No UI text may reveal the shared UUID, pins, resource paths, or payloads.

- [ ] **Step 4: Write README, changelog, license, and quality checklist**

README sections must cover prerequisites, exact supported model/firmware, UI
setup/removal, one-time internet bootstrap, fully local restarts/runtime,
unofficial shared-identity warning, backup-key disclosure, entities and
presets, polling/OBSERVE behavior, limitations, troubleshooting, reauth,
certificate expiry, competing clients, and automation examples.

Add a maintainer-only "Bootstrap source and pin maintenance" section describing
how to move the unchanged digest-pinned bytes to a project-controlled HTTPS
mirror, verify `BUNDLE_SHA256`, update only `BUNDLE_URL`, run bootstrap tests,
and issue a release. It must forbid unpinned fallback mirrors and publishing the
maintainer archive in Git.

Use MIT license. Start changelog at `0.1.0` with all supported and explicitly
excluded features. Fill `quality_scale.yaml` with every Bronze/Silver rule
marked `done` or a specific justified exemption; do not claim an unimplemented
rule.

- [ ] **Step 5: Run metadata tests**

Run: `.venv/bin/pytest tests/test_init.py -q`

Expected: translation equality, documented warnings, manifest/version, and
quality metadata tests pass.

- [ ] **Step 6: Commit documentation**

```bash
git add custom_components/samsung_ac_windfree/strings.json \
  custom_components/samsung_ac_windfree/translations/en.json \
  custom_components/samsung_ac_windfree/quality_scale.yaml \
  README.md CHANGELOG.md LICENSE tests/test_init.py
git commit -m "docs: document Samsung WindFree local integration"
```

---

### Task 12: CI, Dependency Canaries, Full Verification, and Live Gate

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `CHANGELOG.md`
- Modify: `tests/fixtures/mode_compatibility.json`
- Modify only if a verified defect is found: integration/test files from prior
  tasks.

**Interfaces:**
- Consumes: complete integration.
- Produces: release-grade automated and authorized live evidence.

- [ ] **Step 1: Add the complete CI workflow**

Create jobs for:

1. Minimum pytest on x86-64 using `requirements_test_min.txt`, whose
   `pytest-homeassistant-custom-component==0.13.333` pins HA `2026.5.4`
2. Stable pytest on both `ubuntu-latest` x86-64 and
   `ubuntu-24.04-arm` arm64 using `requirements_test.txt`, whose
   `pytest-homeassistant-custom-component==0.13.347` pins HA `2026.7.3`
3. A non-blocking scheduled beta canary that creates an isolated environment,
   runs `pip install --pre --upgrade pytest-homeassistant-custom-component
   smartthings-local==0.1.0 cbor2==6.1.3`, records the resolved HA version, and
   runs the complete suite
4. Resolved dependency closure capture in every pytest leg
5. The real dependency contract test in every pytest leg
6. A QEMU/buildx dependency-install and import smoke matrix for
   `linux/amd64` and `linux/arm64`, the supported HA architecture set for this
   release
7. Ruff format/check
8. hassfest
9. HACS integration validation
10. Scheduled Samsung leaf/SPKI pin canary that reports mismatch without
   committing or accepting new material

Use Python 3.14, `actions/checkout@v4`, `actions/setup-python@v5`,
`home-assistant/actions/hassfest@master`, and `hacs/action@main`.

- [ ] **Step 2: Run the complete local suite with coverage**

Run:

```bash
.venv/bin/pytest tests/ -q \
  --cov=custom_components/samsung_ac_windfree \
  --cov-report=term-missing \
  --cov-fail-under=95
```

Expected: all tests pass and integration-module coverage is at least 95%.

- [ ] **Step 3: Run formatting, linting, and local validation**

Run: `.venv/bin/ruff format --check custom_components tests`

Expected: exit `0`.

Run: `.venv/bin/ruff check custom_components tests`

Expected: exit `0`.

Run the repository's hassfest container/action equivalent.

Expected: no manifest, translation, service, or quality-scale errors.

Run the HACS validation action locally or in the pushed CI branch.

Expected: integration validation passes; only a pre-approved brands exemption
may be present before a brands PR exists.

- [ ] **Step 4: Audit the resolved dependency closure**

Run:

```bash
.venv/bin/python -m pip freeze
.venv/bin/python -m pip check
```

Expected: `smartthings-local==0.1.0`, `cbor2==6.1.3`, no broken requirements,
and HA-owned pyOpenSSL/cryptography versions matching the selected HA
environment. Record hashes in the release artifact/check log, not in runtime
diagnostics.

- [ ] **Step 5: Retain the bootstrap recovery artifact outside Git**

Download the public bundle into the maintainer-controlled encrypted release
archive, compute SHA-256, and require exact equality with `BUNDLE_SHA256`.
Record only the digest and archive backup confirmation in the release check
log. Run `git status --short` and verify the bundle bytes and universal key are
not present anywhere in the worktree.

- [ ] **Step 6: Complete the authorized live compatibility matrix**

Using temporary credentials and probe files outside Git, capture original
power, mode, target, fan, swing, preset, light, and auto-clean. Under `try/finally`
verify target-temperature support, fan codes, airflow values, every preset
against every HVAC mode required by `mode_compatibility.json`; update only the
sanitized fixture to combinations that persist. Restore the exact captured
state in `finally` and perform authoritative final reads. Stop and report if any
original state cannot be restored.

- [ ] **Step 7: Complete the Home Assistant live smoke gate**

On the configured test HA instance:

1. Add by host only.
2. Verify exact-model acceptance and entity inventory.
3. Verify representative reversible writes.
4. Block internet access and verify five-second nominal hot polling.
5. Restart HA while internet remains blocked.
6. Verify OBSERVE when cloud reachability returns.
7. Simulate stored-port failure and verify exact-range resweep.
8. Unload/reload the entry.
9. Verify diagnostics contain no production identifiers.
10. Restore exact AC state and firewall state.

- [ ] **Step 8: Record release evidence and commit CI**

Add a `0.1.0` changelog verification note containing only counts and pass/fail
results—no host, UUID, serial, payload, certificate, or alarm details.

```bash
git add .github/workflows/ci.yml CHANGELOG.md \
  tests/fixtures/mode_compatibility.json
git commit -m "ci: verify WindFree integration release"
```

If the live matrix or a verified defect changed any additional tracked file,
stage that exact file in this commit and list it in the commit body.

- [ ] **Step 9: Run final clean-tree verification**

Run:

```bash
git status --short
.venv/bin/pytest tests/ -q \
  --cov=custom_components/samsung_ac_windfree \
  --cov-fail-under=95
.venv/bin/ruff format --check custom_components tests
.venv/bin/ruff check custom_components tests
```

Expected: clean status, all tests pass, coverage at least 95%, and both Ruff
commands exit `0`.

---

## Specification Coverage Map

| Approved design area | Implementation task |
| --- | --- |
| Exact model/firmware/device/platform gate | Tasks 2, 5, 7 |
| Pinned zero-input certificate bootstrap | Task 3 |
| Universal-key non-persistence and backup disclosure | Tasks 3, 10, 11 |
| PyPI and HA-owned dependency boundary | Tasks 1, 4, 12 |
| In-memory DTLS, CoAP, Block2, OBSERVE | Task 4 |
| Generation isolation, reconnect, resweep, fatal auth | Tasks 4, 6 |
| Tiered polling, budget, identity reconciliation | Task 6 |
| Fresh aggregate RMW and verified writes | Tasks 5, 6 |
| HVAC, temperature, fan, swing, presets | Tasks 5, 8, 12 |
| Display light and auto-clean | Tasks 5, 9 |
| Humidity, filter, energy, alarms, current limit | Tasks 5, 9 |
| Excluded unsafe/unverified capabilities | Tasks 5, 8, 9 |
| Host-only config, progress, reconfigure, reauth | Task 7 |
| Offline restart/runtime and unload lifecycle | Tasks 6, 7, 12 |
| Capability drift and Repairs | Tasks 6, 10 |
| Diagnostics privacy and no entity-property I/O | Tasks 8, 9, 10 |
| HACS, documentation, translations, quality scale | Tasks 1, 11, 12 |
| 95% coverage, Ruff, hassfest, HACS validation | Task 12 |
| Authorized live matrix and exact restoration | Task 12 |

## Mandatory Plan Review Gate

Before Task 1 implementation begins:

1. Run `git diff --check`.
2. Search this plan for placeholder language and unresolved interface names.
3. Compare every approved design section with the coverage map above.
4. Run an independent read-only Claude Fable review over the complete plan and
   approved design.
5. Amend every validated Critical or Important finding and every Minor finding
   that changes implementation behavior.
6. Repeat the Fable review until its verdict is `APPROVE`.
7. Record the approved plan commit and verdict below.

**Review record:** Claude Fable reviewed commit
`7903e902806cc097e0a76c428be7bc3ba6b75724` and returned `APPROVE` with no
required changes on 2026-07-23. The following commit changes only this review
record.
