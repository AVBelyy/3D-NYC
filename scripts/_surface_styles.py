"""Pure raster styling helpers for printable NYC surface materials."""

from __future__ import annotations

import numpy as np


def apply_street_palette(
    material,
    top,
    ground,
    *,
    sidewalk_mask,
    tan_pavement_mask,
    road_mask,
    relief_source: float,
) -> dict:
    """Paint tan sidewalks/paved places and ivory carriageways.

    Road priority is deliberate: small source overlaps at curb boundaries must
    not create intermittent tan holes inside an otherwise ivory road surface.
    """
    arrays = [
        np.asarray(material), np.asarray(top), np.asarray(ground),
        np.asarray(sidewalk_mask, dtype=bool), np.asarray(tan_pavement_mask, dtype=bool),
        np.asarray(road_mask, dtype=bool),
    ]
    shape = arrays[0].shape
    if arrays[0].ndim != 2 or any(array.shape != shape for array in arrays[1:]):
        raise ValueError("street palette arrays must be same-shaped and two-dimensional")
    material_array, top_array, ground_array, sidewalks, tan_pavement, roads = arrays
    tan = sidewalks | tan_pavement
    top_array[tan] = ground_array[tan] + float(relief_source)
    material_array[tan] = 3
    top_array[roads] = ground_array[roads] + float(relief_source)
    material_array[roads] = 0
    return {
        "sidewalk_cells": int(sidewalks.sum()),
        "tan_pavement_cells": int(tan_pavement.sum()),
        "ivory_road_cells": int(roads.sum()),
        "road_overlaps_repainted_ivory": int((roads & tan).sum()),
    }
