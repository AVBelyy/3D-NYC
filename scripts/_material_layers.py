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


# The coarsest layer height any supported print profile uses.  A drawn map
# symbol has to read as raised on that profile too, so it is the height the
# default symbol is sized against.  ``test_material_layers`` holds it to the
# profiles ``generate_3mf`` actually offers.
COARSEST_LAYER_HEIGHT_MM = 0.24
DRAWN_LINE_RELIEF_MM = 2 * COARSEST_LAYER_HEIGHT_MM


def drawn_line_relief_mm(config: dict) -> float:
    """Return how far a drawn road or trail line stands above the ground below it.

    A road is a printed line, not a painted patch.  Laid flush with the
    pavement pad beside it, an ivory carriageway ribbon is separated from the
    tan around it by colour alone, so the eye reads every junction, marked
    crossing and kerb overlap the two materials meet at as a break in a line
    that is in fact continuous.  Giving the line its own edge fixes that.

    Two bounds meet in the height.  The line is a cartographic symbol, so how
    far it stands proud is a map-design choice that must not shrink when a
    finer layer height is selected -- the default is two layers at the coarsest
    supported profile, the height at which the same symbol still prints raised
    everywhere.  It is also printed, so whatever is asked for is floored at a
    height that survives slicing: two layers above the ground it is drawn on,
    and one layer above the pavement pad it has to out-top.
    """
    pad = float(config["path_relief_mm"])
    layer = float(config["layer_height_mm"])
    if not math.isfinite(pad) or pad < 0:
        raise ValueError("pavement relief must be finite and non-negative")
    if not math.isfinite(layer) or layer <= 0:
        raise ValueError("layer height must be finite and positive")
    requested = config.get("road_line_relief_mm")
    requested = DRAWN_LINE_RELIEF_MM if requested is None else float(requested)
    if not math.isfinite(requested) or requested <= 0:
        raise ValueError("drawn road line relief must be finite and positive")
    return round(max(requested, 2.0 * layer, pad + layer), 10)
