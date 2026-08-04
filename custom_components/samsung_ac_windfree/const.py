from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType

from homeassistant.const import Platform

from .models import HvacMode, PresetMode

DOMAIN = "samsung_ac_windfree"
PLATFORMS = (
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.SWITCH,
)

SUPPORTED_MODEL = "AR60F12C1AWNEU"
SUPPORTED_DEVICE_TYPE = "oic.d.airconditioner"
SUPPORTED_FIRMWARE = "TP1X_DA-AC-RAC-01001_0000"
SUPPORTED_UNIT_FINGERPRINT_SHA256 = (
    "3a9cffedec67d951674c61307dfd80891cd7879f435bd733da55a3e09088e1f8"
)
SUPPORTED_PLATFORM = "TizenRT 4.0"
SUPPORTED_PRODUCT_VERSION = "SYSTEM 2.0"
# Telemetry only, never an identity gate: Samsung bumps this build string on
# routine OTAs and no read or write path depends on it. Its value records the
# newest platform firmware the live capability matrix was actually run against,
# so diagnostics can show tested-versus-observed.
TESTED_PLATFORM_FIRMWARE = "ARA-KR-TP1-25-ARXX00_11260401"
COMPATIBILITY = MappingProxyType(
    {
        "always_allowed": ("power", "hvac_mode", "display_light", "auto_clean"),
        "by_mode": MappingProxyType(
            {
                HvacMode.AUTO.value: (),
                HvacMode.COOL.value: ("temperature", "fan", "swing", "preset"),
                HvacMode.DRY.value: ("preset",),
                HvacMode.FAN.value: (),
                HvacMode.HEAT.value: ("temperature",),
            }
        ),
    }
)
PRESETS_BY_MODE = MappingProxyType(
    {
        HvacMode.COOL: tuple(
            preset for preset in PresetMode if preset is not PresetMode.DRY_COMFORT
        ),
        HvacMode.DRY: (PresetMode.NONE, PresetMode.DRY_COMFORT),
    }
)
PROBE_PORTS = tuple(range(49152, 49161))

HOST_RESOLVE_TIMEOUT = 5.0
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
