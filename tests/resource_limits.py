"""Best-effort pytest-process resource safeguards."""

from __future__ import annotations

import importlib
import math
import os
from collections.abc import Mapping
from typing import Protocol, cast

_ENVIRONMENT_VARIABLE = "PYTEST_RLIMIT_AS_GB"
_DEFAULT_LIMIT_GIB = 2.0
_BYTES_PER_GIB = 1024**3


class _ResourceModule(Protocol):
    RLIMIT_AS: int
    RLIM_INFINITY: int

    def getrlimit(self, resource_id: int) -> tuple[int, int]: ...

    def setrlimit(self, resource_id: int, limits: tuple[int, int]) -> None: ...


def _load_resource_module() -> _ResourceModule | None:
    """Return the optional Unix resource module."""

    try:
        module = importlib.import_module("resource")
    except ImportError:
        return None
    if not all(
        hasattr(module, attribute)
        for attribute in ("RLIMIT_AS", "RLIM_INFINITY", "getrlimit", "setrlimit")
    ):
        return None
    return cast("_ResourceModule", module)


def apply_address_space_limit(
    environ: Mapping[str, str] = os.environ,
    resource_module: _ResourceModule | None = None,
) -> int | None:
    """Apply a supported address-space ceiling and return the effective limit."""

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

    resource_module = resource_module or _load_resource_module()
    if resource_module is None:
        return None
    requested = int(limit_gib * _BYTES_PER_GIB)
    soft, hard = resource_module.getrlimit(resource_module.RLIMIT_AS)
    ceiling = (
        requested if hard == resource_module.RLIM_INFINITY else min(requested, hard)
    )
    effective = ceiling if soft == resource_module.RLIM_INFINITY else min(soft, ceiling)
    if soft == resource_module.RLIM_INFINITY or soft > ceiling:
        resource_module.setrlimit(resource_module.RLIMIT_AS, (ceiling, hard))
    return effective
