"""Shared material-layer rules for a white substrate and colored surface solids."""

from __future__ import annotations

import math

import numpy as np


def surface_color_depth_mm(layer_height_mm: float, minimum_depth_mm: float = 0.24) -> float:
    """Return a layer-aligned color depth thick enough for a solid surface skin."""
    layer = float(layer_height_mm)
    minimum = float(minimum_depth_mm)
    if not math.isfinite(layer) or layer <= 0:
        raise ValueError("layer height must be finite and positive")
    if not math.isfinite(minimum) or minimum <= 0:
        raise ValueError("minimum surface color depth must be finite and positive")
    requested = max(minimum, 2.0 * layer)
    return round(math.ceil((requested - 1e-9) / layer) * layer, 10)


def white_substrate_top(support_surface_mm, aoi_mask, *, base_mm: float, color_depth_mm: float) -> np.ndarray:
    """Place a continuous white substrate just below the lowest supported surface."""
    ground = np.asarray(support_surface_mm, dtype=float)
    mask = np.asarray(aoi_mask, dtype=bool)
    if ground.ndim != 2 or ground.shape != mask.shape:
        raise ValueError("ground and AOI mask must be same-shaped two-dimensional arrays")
    base = float(base_mm)
    depth = float(color_depth_mm)
    if not math.isfinite(base) or not math.isfinite(depth) or base < 0 or depth <= 0:
        raise ValueError("base and color depth must be finite, with a positive color depth")
    if not np.isfinite(ground[mask]).all():
        raise ValueError("ground inside the AOI must be finite")
    result = np.full(ground.shape, base, dtype=np.float32)
    result[mask] = np.maximum(base, ground[mask] - depth)
    return result
