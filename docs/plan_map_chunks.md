# Plan gap-free multi-plate NYC maps

[`scripts/plan_map_chunks.py`](../scripts/plan_map_chunks.py) partitions one
WGS84 Polygon or MultiPolygon into neighboring `generate_3mf.py` jobs. Every
chunk shares one scale, orientation, terrain datum, terrain-relief factor, and
manufacturing-grid phase. The planner checks that chunk interiors do not
overlap and that their union covers the exact request.

Planning writes commands; it does not generate the 3MF files itself.

## Prerequisites

Run from the repository root in the same Python 3.12 environment used by the
generator. Planning requires complete, production-ready caches for building
footprints, Planimetrics, parks trails, parks structures, land cover,
OpenStreetMap, and 3D buildings, plus LiDAR rasters covering the request and
source padding. See the [cache runbook](cache_source_datasets.md).

This readiness check applies to the production CLI even when
`--geometric-only` and `--skip-terrain-scan` are selected, because the emitted
generation commands still consume the complete source set. The deterministic
planning core has an explicit resolved-input boundary for hermetic tests; a
data-free invocation is recorded as `generation.readiness.result=not_checked`
and its command script warns that inputs must be provisioned before execution.

The planner does not use MTA entrances to choose seams, but its default offline
generation commands still require the MTA entrance cache or downloaded raw CSV.

## Recommended workflow

This example uses a polygon tracked in the repository:

```bash
.venv/bin/python scripts/plan_map_chunks.py \
  --bounding-polygon @data/polygons/central_park.geojson \
  --max-chunks 4 \
  --chunk-size-mm 235x235 \
  --plan-id central_park
```

Inspect `output/plans/central_park/preview.svg`, `plan.json`, and
`validation.json`, then run:

```bash
output/plans/central_park/commands.sh
```

The script revalidates the static plan, runs each exact generation command in
deterministic order, and requires a post-generation seam audit. It exits
nonzero if a chunk is missing or inconsistent.

For a fixed-scale assembly, provide both a scale and, when repeatability of the
layout matters, an orientation:

```bash
.venv/bin/python scripts/plan_map_chunks.py \
  --bounding-polygon @data/polygons/manhattan_island.geojson \
  --max-chunks 20 \
  --chunk-size-mm 235x235 \
  --scale 10528.713079 \
  --orientation-deg 27.67 \
  --plan-id manhattan_2m
```

`--max-chunks` is a ceiling, not a requested piece count. Concave shorelines
and disconnected islands can produce more than one printable polygon from an
occupied scaffold cell, and every emitted polygon counts toward the limit.

## How boundaries are chosen

The search runs in EPSG:2263 feet; print dimensions and cut locations are
snapped to one shared print-space grid. Automatic orientation considers north,
the polygon's minimum rotated envelope, and dominant cached road directions. It
may prefer a street-aligned candidate within three percent of the most detailed
feasible scale.

Rectangles are printer envelopes, not necessarily chunk shapes. The planner
first finds a feasible straight scaffold, then semantic mode can move shared
junctions and route free-form boundaries through available frame slack.
Neighbors reuse the same routed line, so bends do not create a gap or overlap.
Each finished polygon receives its own tight rectangular print frame.

The semantic router strongly avoids buildings, tall or long prominent objects,
transport structures, retaining walls, and park structures. It keeps clearance
from nearby buildings, favors wide roadbeds, trails, water, shorelines, parks,
plazas, parking lots, and other open space, and penalizes unnecessarily complex
paths and very short polyline sides. Building keep-out distance grows with roof
height and footprint length; bridge-like transport structures receive
length-aware protection. The route remains constrained by both neighboring
print frames, the generator's minimum dimensions, and the maximum seam
deviation.

Use `--seam-mode straight` for straight scaffold boundaries. Use
`--geometric-only` to skip semantic layers during a fast layout check and
`--skip-terrain-scan` to skip cached-ground normalization. Those two shortcuts
are useful for smoke tests, not final production plans.

## Important options

| Argument | Default | Meaning |
| --- | ---: | --- |
| `--bounding-polygon WKT\|GEOJSON\|PATH` | required | WGS84 Polygon/MultiPolygon; FeatureCollections are dissolved |
| `--max-chunks` | required | Maximum emitted polygon jobs, including disconnected pieces |
| `--chunk-size-mm WIDTHxHEIGHT` | `235x235` | Maximum frame; each dimension must be 20–250 mm and a grid multiple |
| `--scale` | automatic | Shared scale denominator; automatic mode chooses the most detailed feasible value with seam headroom |
| `--orientation-deg` | automatic | Shared model-Y bearing in degrees east of north |
| `--seam-mode` | `semantic-paths` | Free-form semantic boundaries or `straight` scaffold cuts |
| `--seam-flex-percent` | `4` | Automatic-scale detail reserved for movable seam corridors |
| `--candidate-step-mm` | `0.5` | Candidate straight-cut spacing |
| `--path-step-mm` | `0.25` | Semantic path sampling interval; must be a grid multiple |
| `--max-seam-deviation-mm` | `20` | Maximum routed departure from a scaffold edge, further limited by frame slack |
| `--junction-flex-mm` | up to `8` | Maximum two-dimensional movement of a shared internal junction; capped by maximum seam deviation |
| `--minimum-seam-side-mm` | `2` | Polyline sides below this length receive a penalty |
| `--seam-complexity-penalty` | `6` | Relative cost of extra and short seam sides |
| `--grid-step-mm` | `0.125` | Shared manufacturing-grid spacing |
| `--terrain-origin-m` | scanned | Shared NAVD88 datum; automatic mode uses 5 m below the exact cached minimum, rounded down |
| `--terrain-relief-factor` | scanned | Shared ground-relief factor derived from a stratified sample |
| `--preview-basemap` | on | Overlay cached NYC vector context in the SVG preview; failure falls back to a plain preview |
| `--preview-width-px` | `1600` | Long dimension of the cached preview basemap |
| `--offline` / `--no-offline` | offline | Whether emitted generation commands prohibit network access |
| `--full-validation` | off | Add each 3MF's expensive cross-material audit |
| `--slice` | off | Add an offline Bambu Studio slice to each generation command |

Run `scripts/plan_map_chunks.py --help` for every path, sampling, logging, and
preview control. Numeric limits are checked before output is committed, and
each emitted argv is parsed through the real generator configuration builder.

## Output contract

```text
output/plans/<plan-id>/
  plan.json                         # request, layout, seams, frames, and argv
  validation.json                   # independent static validation report
  post_generation_validation.json  # written after all models exist
  commands.sh                       # static check, jobs, and required post-check
  chunks.geojson                    # all printable footprints
  preview.svg                       # labeled plan view
  preview-map.png                   # optional cached-data background for preview.svg
  logs/planner.jsonl                # structured planning events
  chunks/<chunk-id>.geojson         # one bare Polygon per generator job
  frames/<chunk-id>.json            # one explicit shared-grid print frame
```

`preview-map.png` is omitted when basemap rendering is disabled or cannot be
completed. It is rendered from cached hydrography, parks, plazas, parking lots,
roadbeds, transport structures, and building footprints. Reusable basemap
rasters default to `data/cache/nyc_map_preview/`; change that with
`--preview-map-cache-dir`. Cache keys include the requested area, raster size,
and relevant source-cache metadata.

`plan.json` records whether source-cache readiness was validated for the
emitted generation commands. Normal CLI plans record `validated`. Programmatic
data-free smoke plans record `not_checked`; static plan validation remains
available, but that status is not evidence that the generation commands can run.

The plan stores both the normalized WGS84 request and an exact EPSG:2263 target,
so validation does not define correctness as merely the union of whatever chunk
files happen to exist. Plan paths and generated shell argv are absolute.

## Validation

[`scripts/validate_chunk_plan.py`](../scripts/validate_chunk_plan.py) reports
issues with a code, severity, message, and numeric or location context. Static
validation checks:

- schema, request, layout, frame, polygon, seam, and command structure;
- chunk/component limits, missing coverage, excess coverage, and overlaps;
- expected seams, adjacency, grid phase, tight frames, and containment;
- routed-seam and logical-cell agreement;
- shared generator options and the contents of `commands.sh`;
- agreement between the WGS84 and EPSG:2263 request representations.

The default test suite uses the resolved-input boundary with geometric-only,
skipped-terrain, and disabled-basemap modes. It must therefore pass in a clean
checkout without any ignored NYC cache. Tests that consume full publisher data
belong in a separately provisioned real-data integration workflow rather than
the pull-request unit-test job.

After generation, `--check-generated --require-generated` also inspects each job
configuration, field shape, realized terrain normalization, actual area of
interest, embedded command metadata, and neighboring field edges. By default,
a height seam fails above 0.24 mm at the 95th percentile or 0.72 mm maximum.
Material disagreement warns above 5 percent of paired cells and fails above 25
percent. The validator CLI exposes explicit tolerance overrides.

## Large plans

Planning memory follows semantic features near movable seam corridors rather
than final mesh triangle count. Terrain normalization streams cached LiDAR tiles
one at a time, and spatial indexes support coverage and adjacency checks.

For borough- or city-scale work, create a geometric-only smoke plan first, then
produce the semantic plan from frozen cache snapshots. Use an authoritative
land or administrative polygon instead of a bounding box to avoid ocean-only
plates. Sub-cell islands are not silently dropped: use a more detailed scale, a
finer grid, or explicitly simplify the input polygon.
