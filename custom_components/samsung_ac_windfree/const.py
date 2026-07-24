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
SUPPORTED_PRODUCT_VERSION = "SYSTEM 2.0"
SUPPORTED_PLATFORM_FIRMWARE = "ARA-KR-TP1-25-ARXX00_11260401"
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
    "REMOVED_BUNDLE_URL"
)
BUNDLE_SHA256 = "REMOVED_BUNDLE_SHA256"
REMOVED_SIGNING_DIGEST_NAME = "REMOVED_SIGNING_DIGEST"
SAMSUNG_IDENTITY_HOST = "REMOVED_IDENTITY_HOST"
SAMSUNG_IDENTITY_LEAF_SHA256 = (
    "REMOVED_IDENTITY_LEAF_DIGEST"
)
SAMSUNG_IDENTITY_SPKI_SHA256 = (
    "REMOVED_IDENTITY_SPKI_DIGEST"
)
