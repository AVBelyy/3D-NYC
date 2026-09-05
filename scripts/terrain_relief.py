"""Scale-aware, citywide terrain-relief decisions for printable NYC models."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class TerrainReliefDecision:
    factor: float
    source_low_m: float
    source_high_m: float
    source_robust_span_m: float
    unadjusted_robust_span_mm: float
    adjusted_robust_span_mm: float
    unadjusted_levels: float
    adjusted_levels: float
    mode: str


def choose_terrain_relief(
    elevations_m,
    *,
    scale_denominator: float,
    vertical_exaggeration: float,
    layer_height_mm: float,
    minimum_levels: float = 6.0,
    maximum_factor: float = 3.0,
    minimum_source_span_m: float = 0.50,
    requested_factor: float | None = None,
) -> TerrainReliefDecision:
    """Choose an extra terrain-only factor from robust source relief.

    The 5th–95th percentile span prevents a single LiDAR spike or deep edge
    artifact from controlling the whole tile.  Flat geography remains flat;
    automatic enlargement begins only when the source contains a meaningful
    elevation span.  Smooth terrain is not quantized here—the slicer's layers
    form the visible level sets without destroying the underlying elevation
    field.
    """
    values = np.asarray(elevations_m, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        raise ValueError("terrain relief requires at least one finite elevation")
    low, high = np.percentile(values, [5, 95])
    source_span = float(max(0.0, high - low))
    raw_mm = source_span * 1000.0 / float(scale_denominator) * float(vertical_exaggeration)
    layer = max(float(layer_height_mm), 1e-6)
    raw_levels = raw_mm / layer
    if requested_factor is not None:
        factor = float(requested_factor)
        mode = "explicit terrain-only factor"
    elif source_span < minimum_source_span_m or raw_mm <= 1e-9:
        factor = 1.0
        mode = "auto; genuinely flat/insufficient source relief retained"
    else:
        target_mm = float(minimum_levels) * layer
        factor = float(np.clip(target_mm / raw_mm, 1.0, maximum_factor))
        mode = "auto; robust LiDAR relief expanded to printable level budget" if factor > 1 else "auto; source relief already printable"
    if not (0.25 <= factor <= 10.0):
        raise ValueError("terrain-only vertical factor must be between 0.25 and 10")
    adjusted = raw_mm * factor
    return TerrainReliefDecision(
        factor=factor,
        source_low_m=float(low),
        source_high_m=float(high),
        source_robust_span_m=source_span,
        unadjusted_robust_span_mm=raw_mm,
        adjusted_robust_span_mm=adjusted,
        unadjusted_levels=raw_levels,
        adjusted_levels=adjusted / layer,
        mode=mode,
    )


def absolute_elevation_to_mm(elevation_m, *, origin_m: float, scale_denominator: float,
                             vertical_exaggeration: float, terrain_factor: float,
                             minimum_terrain_mm: float):
    return ((np.asarray(elevation_m) - float(origin_m)) * 1000.0 / float(scale_denominator)
            * float(vertical_exaggeration) * float(terrain_factor) + float(minimum_terrain_mm))
