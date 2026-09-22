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

A shortcut has to be clear, but the steps of the staircase it is shortening do
not: the search prices a keep-out finitely so that a band with no clear
corridor still yields the least-bad cut, so a cut may touch one. Requiring
both left a cut with a single keep-out anywhere on it with no admissible path
at all, and the straightening was then abandoned for the whole cut rather than
done where it could be -- so `angled` fell silently back to `staircase` on
exactly the cuts that had the most corners to lose. Lower Manhattan is where
that bit: fixing it took its worst joint from nine sides to seven, and the
plan as a whole from 145 m to 126 m of seam inside a building, because a clear
diagonal is a better seam than the two right angles it replaces as well as a
simpler one.

Cuts also have to agree with the cuts they cross. Both choose positions on
the same lattice, and nothing else makes them coincide, so a cut's level and
a crossing cut's jog can land on neighbouring lattice positions -- one step
apart. That is not a tidy corner: it is a finger of plate one step wide
reaching the length of the jog, with a slot the same size in the plate
wrapped around it, and it lands at a three-plate corner where the assembly
has least tolerance. A level within a couple of steps of a crossing seam's
turn is therefore pulled onto it, and a jog within a couple of steps of a
crossing seam's straight is put on it, in both cases only when the move stays
inside the deviation allowance, keeps every jog full size, and crosses no
more keep-out than staying put did.

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
  joint fit      102 sides, 65 corners (14.1 per printed metre of seam)
                 shortest side 3.0 mm, shortest joint 32 mm, most corners on A10/B10
  keep-outs      34 crossings, 4.0 mm longest:
    - building (building, 4.0 mm on the A12/B13 seam)
    - FDR Drive (bridge, 3.4 mm on the C12.2/B13 seam)
    - High Line (bridge, 2.7 mm on the A11/A12 seam)
  shared datum   -11.47 m NAVD88, relief factor 1.000
```

The longest crossings are listed under the summary, named where the publisher
supplied a name, so a suspicious one can be found in the preview directly.

A joint is measured over the part of it the plates actually mate along,
which is not always all of the boundary they share. A cut that runs down
part of a region's own outline leaves one piece with a sliver of that line:
real boundary, and the ground it encloses belongs to a plate, but it is
thinner than a manufacturing cell, so nothing prints either side of it. A
stretch counts only where a point a cell to each side lands in a different
plate -- the same question the raster asks -- and `plan.json` records both
figures per joint, `length_ft` and `shared_length_ft`. The preview leaves
the sliver undrawn, because drawn it reads as a seam striking out into open
ground, which is the one thing it is not. The geometry keeps it either way.

This shape turns up when a plan replaces plates of an earlier one: the
target's outline is built from that plan's cut lines, and a new cut is drawn
to the same streets they were, so the two coincide.

Quality is measured on the finished plate joints, not on the cuts the search
made. Compaction merges neighboring pieces afterwards, so part of an early cut
can end up inside a chunk and never be cut at all; scoring the cuts instead
reports crossings that do not exist in the plan. Each crossing therefore names
the two plates whose joint runs through it.

## How well the plates fit

The seam metrics above say what a cut runs *over*. `joint fit` says what the
plates have to mate *along*, and the two are independent: a joint of one long
side over a building still butts flat, while a staircase over open pavement
does not.

Two plates meet on the straight sides of their joint. Every corner between
them is a tab on one plate and a notch on the other at zero clearance, and an
extruded wall is laid a fraction of a bead proud of the mesh it came from, so
a tab never seats to the bottom of its notch. The plates stand off by whatever
the tightest corner leaves, which reads as a gap along the rest of the joint.
That is the "one edge is tight, the next is gappy" failure, and it is why
corners are counted rather than only measured.

Corners per printed metre of seam is the comparable figure; the raw count
moves with the size of the target. On `manhattan_2m_240` the two halves of
the island were not alike. Each has 17 joints between its own plates:

| | plates | joints | corners | per metre | straight joints |
| --- | --- | --- | --- | --- | --- |
| rows 1-7, on the Commissioners' grid | 11 | 17 | 23 | 9.8 | 8 |
| rows 8-11, Lower Manhattan | 9 | 17 | 34 | 18.0 | 7 |

Two Lower Manhattan joints carried 18 of those 34 between them: `A10/B10`
staircased ten times over 270 mm and `B10/C10` eight times over 188 mm. A
seam that has to jog is not free, and where the frame is off the local street
grid it jogs constantly.

Read it next to the seam metrics, never instead of them. The two are
independent, and the ways of buying corners divide sharply into one that is
free and several that are not.

**Deleting a joint is free; bending a seam is not.** Two joints cost more to
fit than one joint twice as long, and every plate the plan does not need is a
pair of joints it does not have. Both of the things that reduced Lower
Manhattan's joint count left the seams where they were or improved them:

| Lower Manhattan | plates | joints | corners | seam inside buildings |
| --- | --- | --- | --- | --- |
| 240 mm plates, as printed | 9 | 17 | 34 | 145 m |
| 250 mm plates | 8 | 13 | 23 | 126 m |
| and pieces packed against the plate | **7** | **9** | **23** | **59 m** |

Going from 240 to 250 mm moves no seam at all: it only lets the compaction
pass put the same pieces on fewer plates. Packing does move them, and moved
them onto better ground -- fewer, wider bands leave each cut more room to
find a street, so the seam inside buildings more than halved while the joint
count did.

On rows 1-7 the same two changes give 13 plates to 11, and 0.12 % of seam
inside a building to 0.00 %.

**`--cut-deviation-mm` and `--min-edge-mm` are not free, and they do not
behave.** They buy corners by narrowing the search, and what the search gives
up is the clear street it was looking for. On Lower Manhattan, holding
everything else at the defaults:

| deviation / min edge | plates | corners | seam inside buildings |
| --- | --- | --- | --- |
| 35 / 35 (default) | 8 | 24 | 103 m |
| 20 / 60 | 7 | 17 | **254 m** |
| 35 / 40 | 8 | 27 | **593 m** |
| 35 / 50 | 8 | 19 | 182 m |
| 28 / 45 | 8 | 24 | 138 m |

Fewest corners and most building cut is the same row. A 5 mm change in
`--min-edge-mm` moved the building cut by a factor of nine, in both
directions: this is not a surface with an optimum to find, it is a search
that either finds a corridor or does not. Do not tune these two against the
corner count. If a plan needs them moved, move them one at a time and read
the seam metrics after every run.

`--cut-straightness` is not a third lever: raising it from 3 to 8 left one
target's plan bit-identical, because the jog price already dominated.

Before reaching for any of them, check whether the corners are load-bearing.
On the worst Lower Manhattan joint they were: its four jogs hold the seam to
2.7 % of its length inside a keep-out, where the same span taken as two jogs
is 18 %, as one jog 23 %, and as a straight line 37 % at the best of the
thirty-one positions it could take. Angling does not rescue it either -- the
best straight seam at *any* angle across that band is still 24 % blocked.
`--cut-style axis` will give you 2 corners over the whole plan instead of 23,
and 1684 m of sliced building instead of 126. Lower Manhattan has no street
running the width of a plate, and a seam that stays out of the buildings has
to change street as often as the city does.

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
- `--preview-format svg|png|both` and `--preview-pixels` control the render.
  `--preview-pixels` sets the basemap's resolution, never the page's size:
  the page is three quarters of the printed map's own size, so a plan of a
  smaller area opens smaller rather than closer, and two plans of one map
  open at the same scale and can be compared by flipping between them. The
  basemap is never below one pixel per cost cell whatever is asked for; for
  more basemap texture lower `--cost-resolution-m` instead. Planning all of
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

## How many plates a target needs

`plan_cut_count` sizes the pieces along an axis, and what it sizes them
against decides the plate count. Against the plate *less twice the wiggle
budget* -- the safe reading, since a cut wandering either way can grow a
piece by twice what it is allowed -- a 250 mm plate with a 35 mm budget is
worth 180 mm, and Lower Manhattan, 436 mm across, asks for three columns. It
is nowhere wider than 371 mm: two would do.

The budget does not have to be charged to every piece at once, because only
one cut is placed at a time, between two boundaries that are already fixed.
A cut is therefore sized against the plate itself and allowed to wander into
the room its own two sides actually have -- a side already inside the plate
must stay inside it, a side still over the limit will be cut again and
constrains nothing. The recursion cuts anything still too big, so the extra
piece is bought only where the geometry really needs it.

What tight packing risks is a cut coming to rest exactly on the plate limit,
a fraction of a step from a cut crossing it, leaving two plates sharing a
stub of an edge rather than meeting at a corner. That cannot be seen from
inside a single cut and the compaction pass absorbs most of them, so it is
judged on the finished plates: if any two share less than `--min-edge-mm`,
the plan is rebuilt with the budget reserved up front and says so in its
notes. On rows 1-7 the tight packing is kept; the target that first exposed
the hazard, a synthetic city with a ragged edge, falls back.

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

## Replanning part of a map you have already printed

A plan is a plan of one target. Planning a smaller piece of that target on its
own gives it its own frame and its own datum, and its plates will not register
with the ones on the wall: the frame origin is anchored to the *first*
target's corner and the datum to its lowest ground, neither of which a
sub-target reproduces. `--continue-plan` is what joins the two.

```bash
.venv/bin/python scripts/plan_map_chunks.py \
  --continue-plan output/plans/manhattan_2m_240/plan.json \
  --replace-plates A8,B8,A9,B9,C9,A10,B10,C10,B11 \
  --scale 10533 --max-chunk-size-mm 250x250 \
  --plan-id lower_manhattan_2m_250
```

The frame origin, axes, vertical datum and relief factor come from that plan
verbatim. Everything else an assembly has to agree on -- scale, grid step,
layer height, vertical exaggeration, source padding, land-cover survey -- is
stated on both command lines and checked, so a disagreement is an error naming
the value to pass rather than a plan that silently will not fit. `--fit-scale`
is refused for the same reason: it would make the new plates a different map.
`--orientation` is ignored, because the frame is inherited.

`--replace-plates` names the plates to replan in place of
`--bounding-polygon`, and builds the target from their own outlines, so the
join is the boundary those plates were printed to.

Those outlines reach this plan through WGS84, and that projection does not
keep a straight line straight: an edge cut dead straight down one lattice
position comes back with its ends about a hundredth of a foot apart. Left
alone, a new cut drawn to that same position runs straight while the outline
slants away from it, and the wedge between them is a sliver far thinner than
the raster. Nothing prints from it, but it is real geometry: it reached the
solid modelling as a degenerate face and came out as a spike standing off
one plate, and it inflated that plate by 45 mm of height for no printed
area. Coordinates within a tenth of a foot of the lattice the earlier plan
cut on are therefore put back on it, which restores the straight line rather
than approximating it, and moves the outline by a four-hundredth of a
manufacturing cell -- far below the resolution either plan is rasterized at,
so the printed edge does not move at all.

Give the same target as a polygon instead and it is still checked. A target
that covers part of a plate but not all of it is refused, naming the plate --
that plate would otherwise have to be reprinted against a boundary that is
not its own.

The report and `plan.json` record the join under `continues`, listing the
plates the new plan replaces and the printed ones it now butts against.

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
