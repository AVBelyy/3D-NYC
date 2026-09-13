# Generate a jigsaw puzzle from a map

`scripts/generate_puzzle.py` takes one finished 3MF from
[`generate_3mf.py`](generate_3mf.md) and produces a printable jigsaw of exactly
N interlocking pieces: a single 3MF in which every piece is its own object, cut
so that the plate comes off the printer in pieces you assemble by hand. The
pieces keep their positions, so the plate you slice is the map you generated,
only divided.

It is a 3MF-to-3MF transform. It reads no source caches, needs no Bambu Studio
profiles and creates no job directory: the input project already carries the
resolved print profile it was generated against, and every clearance this
script defends is derived from that rather than assumed.

## Quick start

```bash
.venv/bin/python scripts/generate_puzzle.py output/models/34_w_76_235.3mf --pieces 25
```

```text
Profile: 0.4 mm nozzle, 0.24 mm layers, 2 walls of 0.45 mm, 256 x 256 mm plate
Brim: disabled because a brim loop fits the 0.64 mm left between two pieces
      after the 0.1 mm object gap, so it would weld the first layer together.
Model 235.000 x 235.000 mm, 4 filaments, 4,434,194 triangles
Grid 5 x 5 = 25 pieces, 47.00 x 47.00 mm each (1.00:1)
Joint: 1.64 mm floor (7 layers), 11.28 mm narrowest knob neck, 0.84 mm gap,
       1.04 mm undercut leaving 0.2 mm of lock
Knob: 1.16 mm thick (5 layers), recessed 0.48 mm under its neighbour's surface
...
output/puzzles/34_w_76_235_25/34_w_76_235_25.3mf 43,151,370 bytes, 25 pieces
Validation passed: 25 pieces, closest neighbours 0.480 mm apart (74s)
```

That writes `output/puzzles/34_w_76_235_25/`:

| File | Contents |
| --- | --- |
| `34_w_76_235_25.3mf` | one object per piece, each still carrying its four filaments |
| `preview.svg` | the cut drawn over a top-down render of the map it was cut from |
| | *(the same render, with the cut drawn in, becomes the 3MF's plate thumbnail)* |
| `puzzle.json` | the grid, the seed, and every derived bound |
| `validation.json` | the audit of the written 3MF, piece by piece |

`--puzzle-id` names the directory and `--output-dir` moves the root. Add
`--dry-run` to plan the cut, write the preview and manifest, and cut no meshes —
it is the cheap way to try piece counts and seeds.

## How the cut works

The map surface is what the cut has to protect. New York is dense enough that a
jigsaw outline dragged across it lands on a building in almost every segment,
which is why [the multi-plate planner](plan_map_chunks.md) treats footprints as
hard keep-outs. So the puzzle is cut on two levels instead of one.

| | Above the floor plane | Below the floor plane |
| --- | --- | --- |
| Shape | a plain `rows x cols` rectangle | a classic jigsaw curve with one knob per edge |
| What you see | straight hairlines across the map | nothing |
| Cut by | half-space plane splits | one Boolean against a prism |

Seen from above the map is crossed by straight lines and nothing else: a
building is clipped by a straight edge or it is untouched, and no seam wanders
across a block. All the interlocking is in the floor, where it holds the pieces
together and is never looked at.

That split is available because the generator already guarantees the lower level
is a solid slab. Only the foundation filament reaches the plate, and
`crossings.minimum_crossing_floor_mm` keeps every tunnel floor and colour skin
above `base_mm`, so the bottom of the model is a flat prism of one material
across the whole footprint. Every run re-checks that on the mesh it was given
before it cuts anything.

The two levels are cut by different means for the same reason they look
different. A surface mesh carries millions of triangles and a half-space split
is cheap on it; a general Boolean against a knobbed prism is not. The floor slab
is a box, so the Boolean that carves the knobs is cheap there.

## What is derived, and from where

Nothing about the machine is hard-coded. The input project's
`Metadata/project_settings.config` was resolved against an installed Bambu
profile, and the cut reads its bounds back out of it:

| Bound | Derived from | Why that quantity |
| --- | --- | --- |
| Gap between pieces | `2 x outer_wall_line_width` | One extrusion of squish allowance on each side of the seam. Every piece prints at once, side by side, and the plate has to come off in pieces rather than as one tile. |
| Knob undercut | the gap, plus half a nozzle | See below — the gap is subtracted from the joint before any of the overhang is left to lock with. |
| Knob recess | two layer heights | A knob lies *under* its neighbour's surface tier. Without a gap in Z the printer lays that surface straight onto the knob. Two layers: one of air, one for the sag of the layer bridged across it. |
| Brim | kept only when a loop cannot fit the gap | A brim is laid outward from every object and clipped back from the others by `brim_object_gap`. Where an extrusion still fits in what is left, the first layer welds the puzzle into a tile. |
| Floor plane | one layer below the lowest non-foundation geometry | The mesh states where the map's colour begins. One layer under it is the same shape of bound the generator sets itself, and keeps the cut off a colour skin's underside rather than grazing it. |
| Narrowest knob neck | `2 x wall_loops x wall line width` | Below it a knob is two walls touching rather than a solid section, so it has no strength to give. |
| Loose-fragment threshold | `nozzle² x layer height` | One extrusion voxel — the same bound `validate_3mf` uses on the finished map, shared through `mesh_precision.minimum_printable_shell_volume_mm3`. |
| Plate area | `printable_area` | The printer the project was resolved for. |
| Plate margin | `brim_width + brim_object_gap` | What the configured brim actually needs; zero when the project prints without one. |
| Knob facet size | half the nozzle | The knob is the only curved surface in the model; its chords should not be the thing that shows. |
| Foundation filament | the material that reaches z = 0 | Which index carries the substrate depends on `--foundation-color` at generation time, so it is read off the geometry rather than assumed. |

Every one of those has an override flag for a project whose settings are
incomplete or a fit you have measured yourself: `--clearance-mm`, `--floor-mm`,
`--knob-recess-mm`, `--tab-undercut-mm`, `--tab-neck`, `--plate-mm`,
`--plate-margin-mm`, `--brim`/`--no-brim`, `--foundation-material`. A project that cannot supply the profile fails with the
list of flags to pass instead, rather than silently substituting numbers.

What is *not* derived is the knob's shape — its reach, its neck as a fraction of
the edge, how much it is jittered from one edge to the next. Those are
cartographic-style design choices, in the same class as
`_material_layers.PAVEMENT_PAD_RELIEF_MM`: they are the proportions of a
cardboard puzzle, held as module constants and scaled with the edge they sit on.

## The joint, and why the gap sets the undercut

Both halves of a joint are cut from one curve and then each eroded by half the
gap. So a knob's head loses half the gap and the notch it has to enter gains
half the gap, and the interference the two pieces actually feel is

```text
lock = undercut - clearance
```

An undercut sized on its own — half a nozzle, say — vanishes completely once a
gap wide enough to separate the pieces is subtracted, and the puzzle falls apart
in the hand. So the undercut is derived from whatever gap is in force rather
than from the profile alone, and the run reports both:

```text
0.84 mm gap, 1.04 mm undercut leaving 0.2 mm of lock
```

`tests/test_generate_puzzle.py` checks this the way a hand does: it pulls one
piece straight away from its neighbour and asserts the knob is caught on the way
out, and that it is *not* caught when the undercut only matches the gap.

The joint needs a second clearance that a flat jigsaw does not. A knob reaches
*under* the neighbour it locks into, and that neighbour's surface tier starts at
the floor plane — so a knob extruded to full floor height would present its top
face flat against the underside of the neighbour's surface, and the printer
would lay that surface straight onto it. Every knob is therefore recessed:

```text
Knob: 1.16 mm thick (5 layers), recessed 0.48 mm under its neighbour's surface
```

The neighbour bridges the gap, which is a short bridge anchored on three sides
and the only overhang the cut creates. `--knob-recess-mm` overrides it, and the
run refuses a recess that would leave the knob under two layers thick.

The knob is also shaped so its neck really is the throat: below the neck it
narrows monotonically from a root twice the neck's width, so there is exactly
one interference in the joint. The finished curve is measured back rather than
assumed — `generate_puzzle.knob_interference` reads the number off the sampled
polyline.

A printed piece is far stiffer than cardboard, and a knob has to enter its notch
sideways because the map above the notch roofs it over. **Print one pair of
neighbouring pieces and try the fit before committing a full plate.** If they
will not go together, raise `--clearance-mm` or lower `--tab-undercut-mm`; if
they fall apart, do the reverse. Nothing in mesh or slicer validation can settle
this for you.

## Piece count and grid

`--pieces` is exact. The planner picks the factorisation of that number whose
pieces come out squarest for the map's proportions — 25 over a square map is
5 x 5, and 8 over a 2:1 map is 2 x 4.

A count that does not factor into acceptable pieces fails rather than quietly
producing ribbons:

```text
error: 23 pieces over a 235 x 235 mm map only factors into shapes as narrow as
1 x 23 (23.00:1 pieces), past the 1.6:1 limit. Try 20, 24, 25 pieces, raise
--max-piece-aspect, or set --grid ROWSxCOLS explicitly.
```

`--max-piece-aspect` (default 1.6) sets how oblong a piece may be, and
`--grid ROWSxCOLS` overrides the choice entirely.

There is no minimum piece size in millimetres, because the real limit is the
knob, not the piece. Asking for too many pieces fails on the neck instead, in
the profile's own terms:

```text
error: 12 x 12 gives 19.6 x 19.6 mm pieces, whose knobs are only 4.70 mm across
the neck. This profile's 2 walls of 0.45 mm need 1.80 mm before a knob is solid
rather than two walls touching.
```

At 235 mm square, 25 pieces are 47 mm across — chunky — and 100 pieces are
23.5 mm, about the size of a piece in a 1000-piece cardboard puzzle.

## The preview

`preview.svg` draws the cut over a top-down render of the map itself, at model
scale, so a seam can be checked against what it actually crosses. The render
comes from `render_map`'s own rasteriser, driven from the solids in hand and
framed on the model's measured bounds, so the image covers the footprint
one-to-one. `--preview-px-per-mm` (default 8) sets its resolution and
`--no-preview` skips it.

Nothing is drawn inside a piece: the preview exists to be looked at, and a label
in every cell is noise over the only thing worth seeing.

## Plate limits

The pieces stay where they were in the map, so the assembled cut occupies the
same footprint as the input model and the whole puzzle prints on one plate.
That only works if the model fits:

```text
error: A 300.0 x 235.0 mm map does not fit 256 x 256 mm with 3.1 mm of margin
(249.8 x 249.8 mm usable). Regenerate the chunk smaller, or raise --plate-mm to
your printer's real area.
```

If you want a map larger than one plate, cut a
[multi-plate plan](plan_map_chunks.md) first and generate a puzzle from each
plate.

## Validation

Every run re-opens the 3MF it just wrote and audits it independently — the
meshes are parsed back out of the serialized XML rather than reused from memory,
because a solid that did not survive serialization intact has not been checked
at all. `--no-validate` skips it; `validation.json` records what passed.

The per-piece rules are `validate_3mf`'s own, applied one piece at a time:

- the archive's ZIP integrity, millimetre units, and one shared production
  object path;
- one object, one build item, one plate instance and one settings block per
  piece, with part ids matching the serialized meshes;
- the filament palette matching the colours the meshes are indexed against, and
  every carried-through metadata member byte-identical to the source model's;
- every material mesh closed, wound consistently, positive in volume, free of
  degenerate triangles, and inside the map's own footprint and above the plate;
- each piece's filaments unioning to exactly one printable component with no
  sealed printable chamber, judged against the shared crumb bound;
- `print_sequence` still `by layer`, because a multi-object plate printed piece
  by piece would drive the toolhead through pieces already standing;
- no brim wide enough to reach across a seam.

It adds the two rules only a puzzle has: **neighbouring pieces must not
overlap**, and nowhere may they come closer than the joint is built to — the
tighter of the seam clearance and the knob recess — so the plate really does
come off in pieces. The closest pair is reported:

```text
Validation passed: 25 pieces, closest neighbours 0.480 mm apart
```

That 0.48 mm is the knob recess: across the seam the pieces stand 0.84 mm apart,
and the closest they ever come is a knob under its neighbour's surface.

A straight cut can sever a bridge abutment and leave its deck floating; that is
what the one-printable-component rule catches, and it names the piece and the
fragment volumes so you can try a different `--seed`, `--pieces` or `--grid`.

The hermetic geometry tests run separately:

```bash
.venv/bin/python -m pytest tests/test_generate_puzzle.py
```

They exercise the grid, the knob profile, the cut polygons and the profile
derivation with no model, cache or 3MF, and they check that the loose-fragment
bound agrees with the one `validate_3mf` applies to the whole map.

A passing run is still not a claim that the fit is right. Print the two-piece
test.

## Reproducing a cut

`--seed` decides every knob's direction, size and position along its edge. The
same seed gives the same puzzle; a different seed gives a different one from the
same map. `puzzle.json` records the seed together with every derived bound, so a
cut can be reproduced from the manifest alone.

The input's `Metadata/generation_command.json` is carried through into the
output 3MF unchanged, so a puzzle still records the command that generated the
map underneath it.

## What this does not do

- It does not lay pieces out to fit a plate they did not already fit.
- It does not route the cut around anything. The surface grid is regular by
  design; a building on a grid line is cut by it.
- It does not slice. Open the result in Bambu Studio, or run
  `scripts/slice_3mf.py` against it.
