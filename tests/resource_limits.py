"""Linux pytest-process resource safeguards."""

from __future__ import annotations

import math
import os
import resource
from collections.abc import Mapping
from typing import Protocol

_ENVIRONMENT_VARIABLE = "PYTEST_RLIMIT_AS_GB"
_DEFAULT_LIMIT_GIB = 2.0
_BYTES_PER_GIB = 1024**3


class _ResourceModule(Protocol):
    RLIMIT_AS: int
    RLIM_INFINITY: int

    def getrlimit(self, resource_id: int) -> tuple[int, int]: ...

    def setrlimit(self, resource_id: int, limits: tuple[int, int]) -> None: ...


def apply_address_space_limit(
    environ: Mapping[str, str] = os.environ,
    resource_module: _ResourceModule = resource,
) -> int | None:
    """Apply the configured soft address-space ceiling and return its value."""

    raw_limit = environ.get(_ENVIRONMENT_VARIABLE, str(_DEFAULT_LIMIT_GIB))
    try:
        limit_gib = float(raw_limit)
    except ValueError as error:
        raise ValueError(
            f"{_ENVIRONMENT_VARIABLE} must be a finite non-negative number"
        ) from error
    if not math.isfinite(limit_gib) or limit_gib < 0:
        raise ValueError(
            f"{_ENVIRONMENT_VARIABLE} must be a finite non-negative number"
        )
    if limit_gib == 0:
        return None

    requested = int(limit_gib * _BYTES_PER_GIB)
    soft, hard = resource_module.getrlimit(resource_module.RLIMIT_AS)
    applied = (
        requested if hard == resource_module.RLIM_INFINITY else min(requested, hard)
    )
    if soft == resource_module.RLIM_INFINITY or soft > applied:
        resource_module.setrlimit(resource_module.RLIMIT_AS, (applied, hard))
    return applied
