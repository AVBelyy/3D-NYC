"""Pure raster styling helpers for printable NYC surface materials."""

from __future__ import annotations

import numpy as np
from scipy import ndimage

# NYC Land Cover survey classes.  Only 1 and 2 record standing vegetation; 3 is
# unvegetated natural ground, and every remaining class is built or water.
LAND_COVER_VEGETATION = (1, 2)
LAND_COVER_BARE_SOIL = 3


def vegetated_ground_mask(
    landcover,
    *,
    vegetation_classes=LAND_COVER_VEGETATION,
    bare_soil_class=LAND_COVER_BARE_SOIL,
):
    """Return the land-cover cells that are unbuilt vegetated ground.

    Bare soil is not pavement.  The survey records it wherever a field is worn,
    shaded, seasonal or freshly graded, so a lawn is routinely delivered as
    vegetation stippled with unvegetated ground.  Reading those stipples as
    built surface punches ivory holes through green that no mapped park polygon
    happens to cover, which is exactly where land cover is the only evidence
    the model has.

    A bare patch is therefore the same ground as the field it lies in, and only
    that: bare soil is green where it is continuous with a region the survey
    calls mostly vegetation.  Ground that is predominantly unvegetated -- a
    construction site, a dirt lot, a beach -- stays ivory even when it abuts a
    lawn, because the region it belongs to is not a field.  Contiguity is
    judged on shared edges, matching the manufacturable contact the material
    topology cleanup enforces later.
    """
    grid = np.asarray(landcover)
    if grid.ndim != 2:
        raise ValueError("land cover must be a two-dimensional raster")
    vegetation = np.isin(grid, list(vegetation_classes))
    bare = grid == bare_soil_class
    if not bare.any():
        return vegetation
    regions, count = ndimage.label(vegetation | bare)
    if not count:
        return vegetation
    share = np.atleast_1d(np.asarray(
        ndimage.mean(vegetation, regions, np.arange(1, count + 1)), dtype=float))
    vegetated_share = np.concatenate([[0.], share])
    return vegetation | (bare & (vegetated_share[regions] > .5))


def _relief_pair(relief_source: float, line_relief_source: float) -> tuple[float, float]:
    """Validate the pavement pad and the drawn line that has to stand above it."""
    pad = float(relief_source)
    line = float(line_relief_source)
    if not np.isfinite(pad) or not np.isfinite(line):
        raise ValueError("surface relief must be finite")
    if line <= pad:
        raise ValueError("a drawn road or trail line must stand above the pavement pad")
    return pad, line


def apply_street_palette(
    material,
    top,
    ground,
    *,
    sidewalk_mask,
    tan_pavement_mask,
    road_mask,
    relief_source: float,
    line_relief_source: float,
) -> dict:
    """Paint tan sidewalks/paved places and raised ivory carriageway lines.

    Road priority is deliberate: small source overlaps at curb boundaries must
    not create intermittent tan holes inside an otherwise ivory road surface.

    A carriageway is a drawn line and the pavement around it is a pad, so the
    two cannot share one height.  Flush, the ivory ribbon is separated from the
    tan beside it by colour alone, and the eye reads every junction the two
    materials meet at as a break in the road.  Raising the ribbon gives the
    line an edge of its own that survives slicing and stays continuous through
    a crossroads.
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
    pad, line = _relief_pair(relief_source, line_relief_source)
    tan = sidewalks | tan_pavement
    top_array[tan] = ground_array[tan] + pad
    material_array[tan] = 3
    top_array[roads] = ground_array[roads] + line
    material_array[roads] = 0
    return {
        "sidewalk_cells": int(sidewalks.sum()),
        "tan_pavement_cells": int(tan_pavement.sum()),
        "ivory_road_cells": int(roads.sum()),
        "road_overlaps_repainted_ivory": int((roads & tan).sum()),
        "pavement_relief_source": pad,
        "drawn_line_relief_source": line,
    }


def paint_trail_ribbons(
    material,
    top,
    ground,
    *,
    trail_mask,
    road_mask,
    line_relief_source: float,
) -> dict:
    """Paint raised tan trail lines without punching holes in an ivory carriageway.

    A mapped footway ribbon routinely overlaps the roadway beside it: marked
    crossings run kerb to kerb, and a sidewalk drawn one printable width wide
    spills into a street of the same order. Painting trails after the street
    palette lets those overlaps scatter tan cells through an otherwise ivory
    road -- the same defect ``apply_street_palette`` avoids at kerbs and
    ``paint_bridge_decks`` avoids on a shared viaduct deck. Rank by class here
    too, so road-over-tan priority holds wherever the two ribbons meet.

    A trail yields its height as well as its colour. A trail and a carriageway
    are both drawn lines and stand at the same relief, but a marked crossing
    reaches a road at every intersection on the map, so writing a trail height
    over a road would cut a notch across the carriageway at each of them the
    moment the two reliefs ever differ. Yielding both together keeps one rule
    at the junction: where a trail meets a road, the road is the surface.

    Every trail is a drawn line, inside a measured street as much as across a
    park. The reference map raises the footway along each block edge into its
    own tan ridge, standing above the lower floor exactly as the ivory
    carriageway line beside it does, so a street reads as three parallel lines
    rather than one flat field with a stripe painted down it. Flattening the
    ones that happen to fall inside mapped pavement would erase that.
    """
    arrays = [
        np.asarray(material), np.asarray(top), np.asarray(ground),
        np.asarray(trail_mask, dtype=bool), np.asarray(road_mask, dtype=bool),
    ]
    shape = arrays[0].shape
    if arrays[0].ndim != 2 or any(array.shape != shape for array in arrays[1:]):
        raise ValueError("trail ribbon arrays must be same-shaped and two-dimensional")
    material_array, top_array, ground_array, trails, roads = arrays
    relief = float(line_relief_source)
    if not np.isfinite(relief):
        raise ValueError("trail relief must be finite")
    kept = trails & ~roads
    top_array[kept] = ground_array[kept] + relief
    material_array[kept] = 3
    return {
        "trail_cells": int(trails.sum()),
        "trail_cells_yielded_to_carriageways": int((trails & roads).sum()),
        "raised_trail_line_cells": int(kept.sum()),
        "drawn_line_relief_source": relief,
    }


def paint_bridge_decks(material, top, decks, *, line_relief_source: float) -> dict:
    """Paint mapped bridge decks so a carriageway always outranks a trail.

    Several mapped ways share one surveyed deck: a viaduct's roadway, the
    footway and cycleway beside it, and the approach ribbons that join them.
    Painting in source order lets whichever way happens to come last decide the
    colour of the whole deck, so an ivory carriageway is silently restyled tan
    by the sidewalk next to it. Rank by class instead, matching the deliberate
    road-over-tan priority ``apply_street_palette`` already applies at kerbs.

    A deck carries the drawn line across the crossing, so it is lifted by the
    drawn-line relief rather than the pavement relief: the ribbon that reaches
    the approach and the deck it continues onto have to meet at one height.
    """
    material_array = np.asarray(material)
    top_array = np.asarray(top)
    if material_array.ndim != 2 or material_array.shape != top_array.shape:
        raise ValueError("deck painting needs same-shaped two-dimensional material and top arrays")
    relief = float(line_relief_source)
    if not np.isfinite(relief):
        raise ValueError("deck relief must be finite")
    decks = list(decks)
    selections = []
    for deck in decks:
        rows = np.asarray(deck["rows"], dtype=np.intp)
        cols = np.asarray(deck["cols"], dtype=np.intp)
        values = np.asarray(deck["values"], dtype=float)
        if rows.ndim != 1 or rows.shape != cols.shape or rows.shape != values.shape:
            raise ValueError("each deck needs matching one-dimensional rows, cols, and values")
        selections.append((rows, cols, values, bool(deck["ivory"])))
    trail_painted = np.zeros(material_array.shape, dtype=bool)
    # Stable ordering, so ways of the same class keep their source order.
    for rows, cols, values, ivory in sorted(selections, key=lambda deck: deck[3]):
        top_array[rows, cols] = values + relief
        material_array[rows, cols] = 0 if ivory else 3
        if not ivory:
            trail_painted[rows, cols] = True
    carriageway_cells = sum(int(len(rows)) for rows, _, _, ivory in selections if ivory)
    return {
        "decks": len(selections),
        "carriageway_decks": int(sum(1 for deck in selections if deck[3])),
        "trail_decks": int(sum(1 for deck in selections if not deck[3])),
        "carriageway_deck_cells": carriageway_cells,
        "trail_deck_cells_reclaimed_by_carriageways": int(
            (trail_painted & (material_array == 0)).sum()
        ),
    }


def stair_tread_mask(
    run_mask,
    *,
    building_mask,
    water_mask,
    road_mask,
    protected_transport_mask,
):
    """Select the cells one mapped stair run may paint, and count what it yields.

    A stair run is a drawn tan line like any other trail, so the same
    road-over-tan priority ``apply_street_palette`` and ``paint_trail_ribbons``
    apply at a kerb holds where a run meets a carriageway.

    A tread carries a height as well as a colour, and that makes the yield
    matter twice over on a crossing. A tread is interpolated between the
    terrain sampled at the run's two ends, so a run written onto a bridge deck
    does not merely restyle the road: it drops the deck to the ground the
    crossing spans, cutting a notch through a carriageway the map otherwise
    draws end to end. The manhattan_2m_A3 failure was exactly that -- a
    staircase beside the ramp at the George Washington Bridge approach,
    stepping the ramp deck down to the street below it.

    A mapped deck therefore outranks the run even where the run is itself the
    way carried: the deck already stands at its surveyed elevation, which the
    terrain beneath a crossing cannot reconstruct.
    """
    arrays = [
        np.asarray(run_mask), np.asarray(building_mask), np.asarray(water_mask),
        np.asarray(road_mask), np.asarray(protected_transport_mask),
    ]
    shape = arrays[0].shape
    if arrays[0].ndim != 2 or any(array.shape != shape for array in arrays[1:]):
        raise ValueError("stair tread masks must be same-shaped and two-dimensional")
    run, buildings, water, roads, protected = arrays
    kept = run & ~buildings & ~water & ~roads & ~protected
    # Report the cells this priority costs the run, not every cell it never
    # owned: a run has always given way to a building and to open water, so
    # counting those too would hide the road and deck cessions being measured.
    ceded = run & ~buildings & ~water & (roads | protected)
    return kept, int(ceded.sum())
