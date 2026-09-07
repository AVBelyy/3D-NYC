"""Measured canopy smoothing for printable, trail-aware relief."""

from __future__ import annotations

import math

import numpy as np
from scipy.ndimage import binary_closing, distance_transform_edt, gaussian_filter, median_filter


def _disk(radius_cells: int) -> np.ndarray:
    radius = max(1, int(radius_cells))
    rows, cols = np.ogrid[-radius : radius + 1, -radius : radius + 1]
    return rows * rows + cols * cols <= radius * radius


def _core_slices(shape, core_slices):
    if core_slices is None:
        core_slices = (slice(0, shape[0]), slice(0, shape[1]))
    rows, cols = core_slices
    starts = (rows.start or 0, cols.start or 0)
    stops = (
        shape[0] if rows.stop is None else rows.stop,
        shape[1] if cols.stop is None else cols.stop,
    )
    if starts[0] < 0 or starts[1] < 0 or stops[0] > shape[0] or stops[1] > shape[1]:
        raise ValueError("canopy core slices escape the source array")
    if stops[0] <= starts[0] or stops[1] <= starts[1]:
        raise ValueError("canopy core slices must select a non-empty area")
    return core_slices, (stops[0] - starts[0], stops[1] - starts[1])


def measured_canopy_relief(
    canopy_height_m,
    vegetation_mask,
    eligible_core_mask,
    *,
    grid_step_mm: float,
    scale_denominator: float,
    smoothing_m: float,
    maximum_gap_mm: float,
    edge_roll_mm: float,
    minimum_source_height_m: float = 1.0,
    maximum_source_height_m: float = 36.0,
    core_slices: tuple[slice, slice] | None = None,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Return a smoothed measured canopy envelope and its printable footprint.

    The upper-surface measurements continue to determine the relief, preserving
    the varied, recognizable canopy of the original model. Narrow gaps in the
    classified canopy footprint are closed before explicit exclusions such as
    trails are applied. Remaining edges roll down over a print-scaled distance
    instead of producing the steep fissures seen in the first physical print.
    """
    values = np.asarray(canopy_height_m, dtype=float)
    vegetation = np.asarray(vegetation_mask, dtype=bool)
    eligible = np.asarray(eligible_core_mask, dtype=bool)
    if values.ndim != 2 or vegetation.shape != values.shape:
        raise ValueError("canopy height and vegetation mask must be same-shaped 2-D arrays")
    core_slices, output_shape = _core_slices(values.shape, core_slices)
    if eligible.shape != output_shape:
        raise ValueError("eligible canopy mask must match the selected core")
    positive = [grid_step_mm, scale_denominator, maximum_gap_mm, edge_roll_mm]
    if not all(math.isfinite(float(value)) and float(value) > 0 for value in positive):
        raise ValueError("canopy scale, grid, gap, and edge-roll values must be finite and positive")
    if not math.isfinite(float(smoothing_m)) or float(smoothing_m) < 0:
        raise ValueError("canopy smoothing must be finite and non-negative")
    if not (
        math.isfinite(float(minimum_source_height_m))
        and math.isfinite(float(maximum_source_height_m))
        and 0 <= float(minimum_source_height_m) < float(maximum_source_height_m)
    ):
        raise ValueError("source canopy height limits are invalid")

    gap_radius_cells = max(1, int(math.ceil(float(maximum_gap_mm) / (2 * float(grid_step_mm)))))
    closed = binary_closing(vegetation, structure=_disk(gap_radius_cells))
    good = (
        vegetation
        & np.isfinite(values)
        & (values > float(minimum_source_height_m))
        & (values <= float(maximum_source_height_m))
    )
    if not good.any():
        empty = np.zeros(output_shape, dtype=np.float32)
        return empty, np.zeros(output_shape, dtype=bool), {
            "method": "smoothed measured LiDAR canopy with print-scaled gap closing and edge roll",
            "source_height_cells": 0,
            "closed_gap_cells": 0,
            "printable_canopy_cells": 0,
            "median_height_m": 0.0,
            "maximum_height_m": 0.0,
            "smoothing_m": float(smoothing_m),
            "maximum_closed_gap_mm": float(maximum_gap_mm),
            "edge_roll_mm": float(edge_roll_mm),
        }

    nearest = distance_transform_edt(~good, return_distances=False, return_indices=True)
    filled = values[tuple(nearest)].astype(np.float32)
    del nearest
    filled = median_filter(filled, size=3)
    source_m_per_cell = float(grid_step_mm) * float(scale_denominator) / 1000.0
    sigma = float(smoothing_m) / source_m_per_cell
    if sigma > 0:
        weight = gaussian_filter(closed.astype(np.float32), sigma)
        filtered = gaussian_filter(filled * closed, sigma) / np.maximum(weight, 0.01)
    else:
        filtered = filled

    source_core = vegetation[core_slices]
    footprint = closed[core_slices] & eligible
    roll_cells = max(1, int(math.ceil(float(edge_roll_mm) / float(grid_step_mm))))
    padded = np.pad(footprint, roll_cells, mode="edge")
    distance_mm = distance_transform_edt(padded)[
        roll_cells : roll_cells + output_shape[0],
        roll_cells : roll_cells + output_shape[1],
    ] * float(grid_step_mm)
    normalized = np.clip(distance_mm / float(edge_roll_mm), 0.0, 1.0)
    roll = normalized * normalized * (3.0 - 2.0 * normalized)
    relief = np.zeros(output_shape, dtype=np.float32)
    heights = np.clip(filtered[core_slices], 0.0, float(maximum_source_height_m))
    relief[footprint] = (heights * roll)[footprint]
    printed = relief[footprint]
    return relief, footprint, {
        "method": "smoothed measured LiDAR canopy with print-scaled gap closing and edge roll",
        "source_height_cells": int(good[core_slices].sum()),
        "closed_gap_cells": int((closed[core_slices] & ~source_core & eligible).sum()),
        "printable_canopy_cells": int(footprint.sum()),
        "median_height_m": float(np.median(printed)) if printed.size else 0.0,
        "maximum_height_m": float(np.max(printed)) if printed.size else 0.0,
        "smoothing_m": float(smoothing_m),
        "maximum_closed_gap_mm": float(maximum_gap_mm),
        "edge_roll_mm": float(edge_roll_mm),
        "minimum_source_height_m": float(minimum_source_height_m),
        "maximum_source_height_m": float(maximum_source_height_m),
    }
