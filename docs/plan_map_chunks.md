# Plan a multi-plate map

`scripts/plan_map_chunks.py` splits one large WGS84 Polygon into chunks that
each fit a single build plate, routes every seam over low, uniform ground, and
writes the exact `generate_3mf.py` commands that build them. The union of the
chunks is the requested polygon: no gaps, no overlaps.

Planning writes commands, a plan manifest, and a preview. It does not generate
any 3MF itself.

## Prerequisites

Run from the repository root in the Python 3.12 environment described in the
[project README](../README.md). Planning reads the LiDAR, Planimetrics,
building-footprint, and OpenStreetMap caches, and optionally land cover. See
the [cache runbook](cache_source_datasets.md). The emitted generation commands
need the full source set that `generate_3mf.py` requires.

## Quick start

```bash
.venv/bin/python scripts/plan_map_chunks.py \
  --bounding-polygon @data/polygons/manhattan_island.geojson \
  --scale 10533 \
  --max-chunks 30 \
  --plan-id manhattan_2m
```

Inspect `output/plans/manhattan_2m/preview.svg` and `plan.json`, then build the
plates:

```bash
output/plans/manhattan_2m/commands.sh
```

To choose the scale from a target chunk budget instead, pass `--fit-scale`. The
planner then steps the scale back from `--scale` only until a real plan fits
`--max-chunks`, and reports the scale it settled on.

## Sizing an assembly

The planner prints the assembled size, so you can work back from a wall:

```text
Plan manhattan_2m: 23 plates at 1:10533
  assembled      509 x 1995 mm (0.51 x 2.00 m)
```

To pick a scale for a target height, divide the polygon's long dimension by the
height you want. Manhattan is about 21.1 km along its axis, so a 2.0 m map is
1:10533. `--max-chunk-size-mm` defaults to 235 x 235 mm, which leaves margin
inside a 256 mm bed.

Larger scale denominators mean a smaller printed map and fewer plates. The
30-million-cell elevation limit in `generate_3mf.py` caps a full 235 mm plate at
roughly 1:11450; the planner reports the exact ceiling when a request exceeds it.

## How seams are chosen

All chunks share one print frame, so their 0.125 mm manufacturing grids
co-register. `--orientation auto` derives that frame's rotation from cached OSM
street bearings inside the target, falling back to the polygon's minimum rotated
rectangle when no coherent grid is found.

Getting that angle right matters more than any other setting. The frame axes are
the directions a seam can run straight along, so if the frame is off the street
grid, every cut drifts out of its street and has to jog back. Over the 4 km
width of Manhattan a 1.5 degree error drifts a seam more than a full block.

The bearing is therefore the *mode* of the length-weighted bearing histogram,
not the mean: averaging is pulled off by Lower Manhattan, the Village grid, and
the diagonal avenues. Over the whole island the mean lands on 27.3 degrees while
the real Commissioners' grid is 29.1. Correcting that alone took Manhattan from
about 11 sides per cut to 5, and halved the share of seam over buildings.

Cut placement scores a raster built from real measurements:

| Evidence | Effect on a cut |
| --- | --- |
| LiDAR `upper_surface_m - ground_m` | Primary cost. Cuts prefer low ground. |
| Building footprints | Keep-out. A seam may not cross a building. |
| Planimetrics `TRANSPORT_STRUCTURE`, OSM road bridges and tunnels | Keep-out. |
| Planimetrics `ROADBED`, `MEDIAN`, `PLAZA`, `PARKING_LOT` | Cheap. Seams follow pavement. |
| Planimetrics `PARK`, land-cover vegetation classes | Cheap where the surface is low. |
| Planimetrics `HYDROGRAPHY` | Penalised to cross, free to follow. |
| Tree canopy height | Discounted against building height. |

Only land-cover classes 1 and 2 are used, the two with a documented meaning in
`build_map_fields.py`. The rest of that raster's legend is not relied on.

Water is penalised because `generate_3mf.py` fits each water body's level from
whatever falls inside one crop, so a body split across a seam can step. Bridges
and tunnels are keep-outs for the same reason: their deck grades are fitted per
crop.

The search finds the cheapest street-following staircase, then `--cut-style
angled` replaces those right angles with straight segments wherever a straight
segment stays clear of every keep-out. In open ground that yields long
diagonals; in a dense grid the right angles survive, because no straight line
across a Manhattan block misses the buildings.

Where a band offers no clear corridor at all, the planner first tries a
different split position, then splits the region one piece further to widen the
search. An extra plate is spent only where the geometry demands it, rather than
cutting through a building.

## Reading the report

```text
  seams          55.8 km over 42 plate joints
  on paved surface 84.98%
  over buildings  0.25%
  over structures  0.19%
  mean height    6.4 m above ground
  keep-outs      34 crossings, 4.0 mm longest:
    - building (building, 4.0 mm on the A12/B13 seam)
    - FDR Drive (bridge, 3.4 mm on the C12.2/B13 seam)
    - High Line (bridge, 2.7 mm on the A11/A12 seam)
  shared datum   -11.47 m NAVD88, relief factor 1.000
```

The longest crossings are listed under the summary, named where the publisher
supplied a name, so a suspicious one can be found in the preview directly.

Quality is measured on the finished plate joints, not on the cuts the search
made. Compaction merges neighboring pieces afterwards, so part of an early cut
can end up inside a chunk and never be cut at all; scoring the cuts instead
reports crossings that do not exist in the plan. Each crossing therefore names
the two plates whose joint runs through it.

The preview rings every keep-out a seam passes through: a dashed orange circle
for a square crossing, a solid magenta one for a lengthwise run. On a two-metre
map a clipped building is under a pixel, so the circle is what makes it findable.

Seams, chunk outlines, labels and those markers are vector, so the preview
stays sharp at any zoom; only the basemap behind them is a raster, at the cut
surface's own sampling. Zoom in to check exactly where a seam sits relative to
a kerb. Pass `--preview-format png` or `both` if you need a bitmap as well.

The preview carries no print cost when it is written, because the plates do not
exist yet. Once they are built and estimated, caption it with what the plan
actually costs to print:

```bash
.venv/bin/python scripts/compute_print_stats.py \
  output/models/manhattan_2m_240_*.3mf \
  --update-preview output/plans/manhattan_2m_240/preview.svg
```

Each plate's label card grows to hold one more line — that plate's own print
time and filament weight — and a band above the map carries the totals for the
whole plan, split into model material and tool-change purge, with a colour chip
per filament. Plates are matched to models by the label the model's name
ends in (`<plan-id>_<label>.3mf`), so estimating a subset captions only those
plates and the run names any model it could not place.

The band is a strip grown above matplotlib's own canvas rather than an overlay,
so it hides no map. A rerun restores the boxes the planner drew before
captioning them again, so the preview never accumulates a second set.

A seam has to cross an elevated road somewhere, and crossing one square costs
about its width. A seam traveling *along* a structure is the real defect. Each
crossing is therefore judged against the feature's own narrow width rather than
a flat millimetre budget: a crossing longer than 1.6 times that width is marked
`along_feature`, and one longer than `--max-keep-out-crossing-mm` (3 mm) fails
the run unless you pass `--allow-keep-out-crossings`. Square crossings are
reported but never fail.

`plan.json` names every crossing with its length, the feature's width, the cut
it belongs to, and whether it runs lengthwise.

## Useful controls

- `--cut-style angled|staircase|axis`. `angled` straightens the street-following
  path wherever it can; `staircase` keeps the right angles; `axis` forces one
  straight line parallel to the frame axis, which is only sensible where a
  single street runs the whole width.
- `--cut-deviation-mm` is how far a seam may wander from straight, and it is a
  hard ceiling rather than a target: the search never roams further even when
  spare plate width would allow it, because roaming for marginal savings is what
  turns a straight seam into a staircase. Larger values reach cleaner ground but
  cost plates, since the budget is reserved on both sides of every cut. It is a
  printed measurement, so the same budget covers less ground at a finer scale.
- `--min-edge-mm` and `--min-jog-mm` set the shortest run and sideways step a
  seam may make. Raising `--min-edge-mm` buys fewer sides. With the frame on the
  grid the cost of doing so is small: planning all of Manhattan at 1:10533
  against one cache snapshot gave about 6 sides per cut at 25 mm and 4 at 45 mm,
  with the share of seam over buildings staying near 0.25 % throughout. If
  raising it noticeably degrades seam quality on your target, suspect the frame
  angle before reaching for this. Re-measure rather than treating those figures
  as fixed.
- `--cut-straightness` prices a single jog, as a multiple of what a minimum run
  costs over the band's *mean* ground. Pricing it against the cheap tail instead
  makes jogging nearly free on a band that is mostly pavement, which is the
  classic way to end up with a staircase that buys nothing. `--cut-centering`
  keeps a seam near its straight position and so keeps plates evenly sized.
- `--keep-out-height-m` defaults to 0, forbidding every building. Raise it only
  when a target is too dense to route around all of them.
- `--cost-resolution-m` is the cut-search sampling, and also the basemap's
  resolution in the preview. 4 m resolves Manhattan streets; finer sharpens both
  the search and the preview at a real cost in time and memory, since the whole
  target is held as several full-resolution rasters.
- `--preview-format svg|png|both` and `--preview-pixels` control the render. The
  canvas is never smaller than one pixel per cost cell, so raising
  `--preview-pixels` enlarges the embedded basemap rather than resampling it;
  for more basemap texture lower `--cost-resolution-m` instead. Planning all of
  Manhattan at 2 m completes but peaked near 9 GB of memory on one run.
- `--orientation north` or an explicit bearing in degrees overrides the
  automatic frame rotation. Use an explicit bearing when a target spans two
  street grids and you want the frame on a particular one; `plan.json` records
  the detected bearing and its coherence so you can see what `auto` chose.

Run `scripts/plan_map_chunks.py --help` for the complete option reference.

## What the plan contains

`output/plans/<plan-id>/` holds:

| Path | Contents |
| --- | --- |
| `plan.json` | Normalized request, frame, shared values, per-chunk record, seam quality |
| `commands.sh` | One `generate_3mf.py` invocation per chunk, in assembly order |
| `chunks/<plan-id>_<label>.geojson`, `.frame.json` | The files those commands reference |
| `chunks.geojson` | All chunks with attributes, for GIS inspection |
| `preview.svg` | Chunk layout over a basemap drawn from the local caches |
| `preview_cuts.svg` | With `--diagnostics`: the cut-cost surface and keep-outs |

The plates themselves are written outside the plan, to
`<output-dir>/models/<plan-id>_<label>.3mf`, by the commands rather than by the
planner.

Chunks are labelled by frame position for wall assembly, top-left first: a
column letter and a row number (`A1`, `B2`, ...). Rows and columns are grouped
by overlapping extent rather than a fixed pitch, so where two plates land in one
cell the second takes a suffix (`C12.2`).

The cut-cost surface is rebuilt from `data/cache` on every run and never
persisted, so a plan always reflects the caches as they are now. Building it
dominates the runtime: roughly 70 of the 90 seconds a Manhattan-sized plan
takes at `--cost-resolution-m 4`, against about 11 for the partition search.

## Why the shared values matter

`generate_3mf.py` defaults both the vertical datum and the ground-relief factor
to statistics of whatever crop it is given, so two neighboring plates would
otherwise map the same real elevation to different heights. The planner computes
both once over the whole target and pins them on every command, together with
the scale, grid step, layer height, and vertical exaggeration. Editing any of
those for a single plate will break its seams.

The planner also checks, before writing anything, that the chunks partition the
target within tolerance, that each plate satisfies every `generate_3mf.py`
precondition, and that each chunk's frame origin sits on the shared lattice.

## Limits

- A gap-free plan is a geometric result, not a printing guarantee. Print two
  adjacent chunks and check the joint before committing to a full assembly.
- Water bodies and crossings split by a seam remain a residual risk from
  crop-local fitting in the generator. The planner avoids them where it can and
  reports the ones it could not.
- Canopy smoothing in `build_map_fields.py` has no cross-tile halo, so a seam
  through dense canopy can show a small discontinuity. Canopy height is part of
  the cut cost for that reason.
- Chunk boundaries inherit the target polygon's own outline. A detailed
  shoreline stays detailed; the planner only regularizes the cuts it makes.
