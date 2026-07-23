from __future__ import annotations

import json
from pathlib import Path

from custom_components.samsung_ac_windfree.const import (
    DOMAIN,
    PROBE_PORTS,
    SUPPORTED_DEVICE_TYPE,
    SUPPORTED_FIRMWARE_PREFIX,
    SUPPORTED_MODEL,
    SUPPORTED_PLATFORM,
)


def test_fixed_product_contract() -> None:
    assert DOMAIN == "samsung_ac_windfree"
    assert SUPPORTED_MODEL == "AR60F12C1AWNEU"
    assert SUPPORTED_DEVICE_TYPE == "oic.d.airconditioner"
    assert SUPPORTED_FIRMWARE_PREFIX == "TP1X_DA-AC-RAC-01001"
    assert SUPPORTED_PLATFORM == "TizenRT 4.0"
    assert PROBE_PORTS == tuple(range(49152, 49161))


def test_manifest_pins_isolated_requirements() -> None:
    manifest = json.loads(
        Path("custom_components/samsung_ac_windfree/manifest.json").read_text()
    )
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "local_push"
    assert manifest["integration_type"] == "device"
    assert manifest["requirements"] == [
        "smartthings-local==0.1.0",
        "cbor2==6.1.3",
    ]
