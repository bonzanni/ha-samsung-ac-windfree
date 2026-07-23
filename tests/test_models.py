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
