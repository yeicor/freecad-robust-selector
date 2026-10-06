"""Backwards-compatible alias for robust_selector."""
from __future__ import annotations

from robust_selector import (  # noqa: F401
    Selector,
    Step,
    FeatureRef,
    selector,
    resolve,
    apply,
    create,
    bind,
    __version__,
    __all__,
)
