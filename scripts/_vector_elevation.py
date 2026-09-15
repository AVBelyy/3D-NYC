"""Terrain and canopy elevations built from vector sources instead of LiDAR.

The LiDAR caches supply exactly two rasters to the rest of the pipeline: a
bare-earth ``ground_m`` and an ``upper_surface_m`` whose only uses downstream
are canopy relief and rooftop fixture heights.  Everything else that stands up
in a printed map -- buildings, bridges, decks, stairs -- already comes from
vector records.  This module reconstructs those two rasters from datasets the
project already caches, so a model can be generated where no LiDAR collection
exists.

Terrain
    NYC Planimetrics publishes surveyed spot elevations, and the Building
    Footprints layer records a ``ground_elevation`` for each footprint.
    Together they cover a city block about every twenty metres, which is finer
    than the 0.65 mm print-scale smoothing the field builder applies to the
    terrain anyway.  A linear triangulation through those points is the
    terrain; outside their convex hull the nearest control value is carried.

Canopy
    Nothing in the vector sources measures tree height, so canopy relief is
    modelled from the shape of the land-cover canopy patches themselves: an
    isolated street tree is short, and height builds toward a closed-stand
    value as a patch grows wider.  ``crown_canopy_height`` states that as an
    exponential approach from an edge height to a mature height over a crown
    scale length, all three in source metres.  This is a model, not a
    measurement, and callers must record it as such.

Sub-feature codes are the published Planimetrics ELEVATION legend.  Only the
roadbed and water-surface codes describe terrain: bridge spots sit on a deck
and, as the footprint join in this repository's caches shows, code 302000
falls inside a building footprint 99.6% of the time and is a roof reading.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
import shapely
from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator
from scipy.ndimage import distance_transform_edt

# US survey foot in metres, matching the rest of the pipeline.
FT = 0.3048006096012192

SPOT_ROADBED = 300000
SPOT_BRIDGE = 300020
SPOT_WATER = 301000
SPOT_ROOF = 302000
#: Spot-elevation codes that describe the ground surface itself.
TERRAIN_SPOT_CODES = (SPOT_ROADBED, SPOT_WATER)

#: Plausible NAVD88 range for a New York City ground elevation, in metres.
#: Wide enough to keep Todt Hill and the harbour floor, narrow enough that a
#: placeholder such as -999 or a foot/metre unit slip is rejected.
MINIMUM_TERRAIN_M = -40.0
MAXIMUM_TERRAIN_M = 250.0

#: Canopy model defaults, in source metres.  ``EDGE`` and ``MATURE`` bracket
#: the measured 2021 DSM canopy heights over the land-cover canopy class, and
#: ``CROWN_SCALE`` is roughly a mature crown radius: the distance over which a
#: patch closes and stops gaining height.
CANOPY_EDGE_HEIGHT_M = 8.0
CANOPY_MATURE_HEIGHT_M = 21.5
CANOPY_CROWN_SCALE_M = 5.0


class VectorElevationError(RuntimeError):
    """The vector sources cannot describe terrain over the requested area."""


@dataclass(frozen=True)
class ControlPoints:
    """Surveyed ground elevations, in EPSG:2263 feet and NAVD88 metres."""

    x: np.ndarray
    y: np.ndarray
    z_m: np.ndarray
    provenance: dict

    def __len__(self) -> int:
        return int(self.x.size)


def _finite_columns(x, y, z) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = np.asarray(z, dtype=float)
    keep = (
        np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
        & (z >= MINIMUM_TERRAIN_M) & (z <= MAXIMUM_TERRAIN_M)
    )
    return x[keep], y[keep], z[keep]


def spot_elevation_points(elevations) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Terrain control points from a Planimetrics ELEVATION frame."""
    if elevations is None or not len(elevations):
        empty = np.empty(0, dtype=float)
        return empty, empty.copy(), empty.copy()
    codes = pd.to_numeric(elevations["SUB_FEATURE_CODE"], errors="coerce")
    selected = elevations[codes.isin(TERRAIN_SPOT_CODES).to_numpy()]
    if not len(selected):
        empty = np.empty(0, dtype=float)
        return empty, empty.copy(), empty.copy()
    geometry = selected.geometry.to_numpy()
    values = pd.to_numeric(selected["ELEVATION"], errors="coerce").to_numpy(dtype=float)
    return _finite_columns(
        shapely.get_x(geometry), shapely.get_y(geometry), values * FT
    )


def footprint_ground_points(footprints) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Terrain control points from Building Footprints ``ground_elevation``.

    One point per footprint, placed at a point guaranteed to lie inside it.
    These fill the block interiors that the street-centred spot elevations
    leave empty.
    """
    empty = np.empty(0, dtype=float)
    if footprints is None or not len(footprints) or "ground_elevation" not in footprints:
        return empty, empty.copy(), empty.copy()
    values = pd.to_numeric(footprints["ground_elevation"], errors="coerce").to_numpy(dtype=float)
    usable = np.isfinite(values) & footprints.geometry.notna().to_numpy()
    if not usable.any():
        return empty, empty.copy(), empty.copy()
    inside = footprints.geometry[usable].representative_point()
    return _finite_columns(inside.x.to_numpy(), inside.y.to_numpy(), values[usable] * FT)


def ground_control_points(elevations=None, footprints=None) -> ControlPoints:
    """Pool every surveyed ground elevation available for an area."""
    spot_x, spot_y, spot_z = spot_elevation_points(elevations)
    base_x, base_y, base_z = footprint_ground_points(footprints)
    points = ControlPoints(
        x=np.concatenate([spot_x, base_x]),
        y=np.concatenate([spot_y, base_y]),
        z_m=np.concatenate([spot_z, base_z]),
        provenance={
            "planimetric_spot_elevations": int(spot_x.size),
            "building_ground_elevations": int(base_x.size),
            "spot_sub_feature_codes": list(TERRAIN_SPOT_CODES),
        },
    )
    if len(points) < 3:
        raise VectorElevationError(
            "Vector terrain needs at least three surveyed ground elevations in the "
            f"sampled area; found {len(points)} "
            f"({points.provenance['planimetric_spot_elevations']} spot elevations, "
            f"{points.provenance['building_ground_elevations']} building ground elevations)"
        )
    return points


def interpolate_terrain(points: ControlPoints, xs, ys, *, chunk_rows: int = 512) -> np.ndarray:
    """Linear triangulation through the control points, nearest outside it.

    ``xs`` and ``ys`` are same-shaped coordinate arrays in EPSG:2263 feet; the
    result is NAVD88 metres in that shape.  Evaluation is chunked because a
    0.5 m destination grid for a whole plate is tens of millions of cells and
    the interpolator materializes one float64 per cell.

    A linear interpolant is bounded by its vertex values, which is what lets
    ``control_point_floor`` state an exact lower bound for a whole area without
    sampling the grid.
    """
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    if xs.shape != ys.shape:
        raise ValueError("terrain sample coordinates must be same-shaped")
    coordinates = np.column_stack([points.x, points.y])
    linear = LinearNDInterpolator(coordinates, points.z_m)
    nearest = NearestNDInterpolator(coordinates, points.z_m)
    flat_x = xs.reshape(-1)
    flat_y = ys.reshape(-1)
    result = np.empty(flat_x.size, dtype=np.float32)
    step = max(1, int(chunk_rows) * max(1, xs.shape[-1] if xs.ndim > 1 else 1))
    for start in range(0, flat_x.size, step):
        stop = min(start + step, flat_x.size)
        block_x = flat_x[start:stop]
        block_y = flat_y[start:stop]
        values = linear(block_x, block_y)
        outside = ~np.isfinite(values)
        if outside.any():
            values[outside] = nearest(block_x[outside], block_y[outside])
        result[start:stop] = values.astype(np.float32, copy=False)
    return result.reshape(xs.shape)


def interpolate_terrain_grid(points: ControlPoints, transform, shape,
                             *, chunk_rows: int = 512) -> np.ndarray:
    """``interpolate_terrain`` over a raster grid, a band of rows at a time.

    Cell-centre world coordinates are derived per band rather than for the
    whole raster, because a plate's 0.5 m grid is tens of millions of cells and
    two float64 coordinate arrays that size are the largest thing the stage
    would otherwise hold.
    """
    height, width = int(shape[0]), int(shape[1])
    a, b, c, d, e, f = transform.a, transform.b, transform.c, transform.d, transform.e, transform.f
    columns = np.arange(width, dtype=float) + 0.5
    result = np.empty((height, width), dtype=np.float32)
    step = max(1, int(chunk_rows))
    for start in range(0, height, step):
        stop = min(start + step, height)
        rows = (np.arange(start, stop, dtype=float) + 0.5)[:, None]
        xs = c + a * columns[None, :] + b * rows
        ys = f + d * columns[None, :] + e * rows
        result[start:stop] = interpolate_terrain(points, xs, ys, chunk_rows=step)
    return result


def control_point_floor(points: ControlPoints, mask=None) -> float:
    """The lowest terrain any linear interpolation of these points can reach.

    Used for the shared vertical datum a multi-plate plan pins: a plate must
    never sample ground below the origin its neighbours were built with.
    """
    values = points.z_m if mask is None else points.z_m[np.asarray(mask, dtype=bool)]
    if not values.size:
        raise VectorElevationError("No surveyed ground elevations fall inside the requested area")
    return float(values.min())


def crown_canopy_height(
    canopy_mask,
    *,
    cell_m: float,
    edge_height_m: float = CANOPY_EDGE_HEIGHT_M,
    mature_height_m: float = CANOPY_MATURE_HEIGHT_M,
    crown_scale_m: float = CANOPY_CROWN_SCALE_M,
) -> np.ndarray:
    """Model canopy height from the width of each canopy patch.

    ``h(d) = mature - (mature - edge) * exp(-d / scale)``, where ``d`` is the
    distance in source metres from the cell to the nearest cell outside the
    canopy.  A lone street tree keeps ``edge_height_m``; the interior of a
    closed stand approaches ``mature_height_m`` over about ``crown_scale_m``.

    Cells outside the canopy are zero, so the result adds to a terrain surface
    directly.  It is a model of how tree height and crown size go together,
    not a measurement of any particular tree.
    """
    mask = np.asarray(canopy_mask, dtype=bool)
    if mask.ndim != 2:
        raise ValueError("canopy mask must be a 2-D array")
    for name, value in (("cell_m", cell_m), ("crown_scale_m", crown_scale_m)):
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError(f"{name} must be finite and positive")
    edge = float(edge_height_m)
    mature = float(mature_height_m)
    if not (math.isfinite(edge) and math.isfinite(mature) and 0 <= edge <= mature):
        raise ValueError("canopy edge height must be between zero and the mature height")
    height = np.zeros(mask.shape, dtype=np.float32)
    if not mask.any():
        return height
    distance_m = distance_transform_edt(mask) * float(cell_m)
    height[mask] = (
        mature - (mature - edge) * np.exp(-distance_m[mask] / float(crown_scale_m))
    ).astype(np.float32)
    return height


def canopy_model_report(cell_m: float, *, edge_height_m: float, mature_height_m: float,
                        crown_scale_m: float, cells: int) -> dict:
    """What a caller must record about a modelled, unmeasured canopy."""
    return {
        "method": "crown-size canopy model; no measured tree heights",
        "measured": False,
        "edge_height_m": float(edge_height_m),
        "mature_height_m": float(mature_height_m),
        "crown_scale_m": float(crown_scale_m),
        "source_cell_m": float(cell_m),
        "canopy_cells": int(cells),
    }
