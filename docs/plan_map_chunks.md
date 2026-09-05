# Plan gap-free multi-plate NYC maps

[`scripts/plan_map_chunks.py`](../scripts/plan_map_chunks.py) partitions one WGS84 Polygon or MultiPolygon into reproducible `generate_3mf.py` jobs. Every chunk uses one common map scale, terrain datum, terrain-relief factor, orientation and manufacturing-grid phase. The emitted polygons have disjoint interiors and their union is independently checked against the exact request.

The planner writes commands; it does not run the expensive 3MF jobs itself.

## Recommended workflow

Run from the repository root with completed citywide vector, OSM, CityGML, land-cover and 0.5 m LiDAR caches:

```sh
.venv/bin/python scripts/plan_map_chunks.py \
  --bounding-polygon @data/polygons/uws_central_park_ues.geojson \
  --max-chunks 9 \
  --chunk-size-mm 235x235 \
  --plan-id uws_central_park_ues
```

The output directory defaults to `output/plans/<plan-id>/`. Inspect `preview.svg`, `plan.json` and `validation.json`, then run:

```sh
output/plans/uws_central_park_ues/commands.sh
```

`commands.sh` first revalidates the static plan, runs every exact generation command in deterministic order, and finally requires a post-generation seam audit. The last audit reads the generated field grids and 3MF archives; the script exits nonzero if any required chunk is missing or inconsistent.

### Two-metre Manhattan Island plan

The repository includes the Manhattan Island boundary extracted from the cached OSM relation. This verified command fixes the assembled north-south span at exactly 2,000 mm while keeping every frame at or below 235 x 235 mm:

```sh
.venv/bin/python scripts/plan_map_chunks.py \
  --bounding-polygon @data/polygons/manhattan_island.geojson \
  --max-chunks 20 \
  --chunk-size-mm 235x235 \
  --scale 10528.713079 \
  --orientation-deg 27.67 \
  --plan-id manhattan_2m
```

The fixed orientation is the closest tested street-grid alignment that keeps the 2,000 mm-tall envelope within two 235 mm columns. The checked result has a 470 x 2,000 mm print-space envelope, a 2 x 9 logical scaffold and 18 emitted Polygon jobs. `--max-chunks` is a ceiling, not a request to manufacture exactly that many pieces. The exact job count can differ from the number of occupied scaffold cells when a concave shoreline intersection has multiple Polygon components.

## Boundary selection

The search is performed in EPSG:2263 feet, while dimensions and cut locations are snapped to the shared print-space grid. Automatic orientation considers north, the requested polygon's minimum rotated envelope and the dominant cached roadbed orientation. It favors a street-aligned orientation when it costs no more than 3% map detail.

The local cache is sufficiently complete for city-scale boundary decisions:

| Cache | Relevant retained data |
|---|---|
| LiDAR | 296 canonical tiles derived from 1,894 LAZ sources; 0.5 m EPSG:2263 ground and upper surfaces in metres NAVD88. |
| Vectors | 4,031,333 features, including 1,083,026 current building footprints, 104,961 roadbeds, 13,206 parks, 2,206 hydrography features, 2,247 transport structures and 4,681 retaining walls. |
| OSM | 2,372,144 current cached features (2,403,527 tiled rows), used for named-road seam labels and current topology. |
| CityGML | 1,083,433 building objects and 1,579,005 retained roof surfaces, used by generation after boundaries are selected. |
| Land cover | One unquantized 310,844 × 314,414 native classification raster, used by generation for terrain/canopy materials. |

Current footprints are the decisive cut-avoidance layer because a split building creates the most obvious assembly defect. Roof heights increase the penalty for especially visible cuts. Roadbeds are the best positive seam signal: a joint can sit in an existing narrow, low-relief visual break, while named OSM centerlines make the result reviewable. Structures and retaining walls are treated as conflicts; parks and water are weaker fallbacks because they are visually simple but can still carry canopy, shoreline or inferred-level detail. LiDAR is used globally to choose one vertical normalization, while actual generated LiDAR-derived edge heights are compared after the jobs finish.

Rectangles are only printer envelopes, not chunk shapes. Planning first finds a feasible straight scaffold, then routes each shared scaffold-edge segment as a free-form path through the frame slack. Segment endpoints meet at common junctions, while intermediate vertices remain on the shared manufacturing grid. Adjacent chunks reuse the identical routed line, so bends cannot introduce a gap or overlap. Each finished Polygon receives its own tight rectangular print frame, which may overlap a neighbor's frame even though their printable polygons never overlap.

The semantic path router uses dynamic programming inside each safe deviation corridor. It is subject to the requested maximum frame dimensions, the 20 mm generator minimum and `--max-seam-deviation-mm`. Candidate path samples are scored in this order:

1. Avoid cutting cached building footprints. Building count, crossing length and recorded roof height dominate the score.
2. Avoid transport structures, retaining walls and park structures.
3. Keep clearance from nearby buildings.
4. Prefer seams running along roadbeds (including small streets), park trails, hydrography or shoreline, then parks/open areas, over seams through ordinary modeled surface.
5. Prefer reasonably even chunk widths/heights when semantic scores are otherwise close.

This produces arbitrary non-rectangular but manufacturable chunks. “Best” means the lowest score inside the explicit monotone path corridors, rather than an unconstrained cut that could make a chunk exceed its printer frame. Use `--seam-mode straight` for the legacy straight boundaries, `--orientation-deg` to require a particular global scaffold, or reduce `--path-step-mm` for denser route sampling.

The UWS + Central Park + UES semantic-path smoke plan selected the Manhattan street grid at 28.665835° east of north and a 3 × 3 scaffold at 1:6,362.1. No selected seam cuts a building. Routed segments include boundaries that are almost entirely inside roadbed, one segment that follows a Central Park trail for 42% of its length and another that uses water for 45%. Every semantic fraction and conflict is stored per segment in `plan.json` and emitted in the structured log.

## Inputs and important options

| Argument | Default | Meaning |
|---|---:|---|
| `--bounding-polygon WKT\|GEOJSON\|PATH` | required | WGS84 Polygon/MultiPolygon. GeoJSON FeatureCollections are dissolved. Prefix paths with `@` when helpful. |
| `--max-chunks` | required | Hard maximum number of emitted Polygon jobs, including disconnected pieces and islands. |
| `--chunk-size-mm WIDTHxHEIGHT` | `235x235` | Maximum print frame for each chunk; each dimension must be 20–250 mm and a grid multiple. |
| `--scale` | automatic | One shared denominator. Automatic mode chooses the most detailed feasible scale with seam-movement headroom. |
| `--orientation-deg` | automatic | Shared model-Y bearing, degrees east of north. |
| `--seam-mode` | `semantic-paths` | Route non-rectangular semantic boundaries; `straight` retains global straight cuts. |
| `--seam-flex-percent` | `4` | Detail traded for a wider semantic seam-search corridor in automatic-scale mode. |
| `--candidate-step-mm` | `0.5` | Candidate seam spacing; actual cuts remain on `--grid-step-mm`. |
| `--path-step-mm` | `0.25` | Semantic path sampling interval; must be a manufacturing-grid multiple. |
| `--max-seam-deviation-mm` | `20` | Maximum path departure from a scaffold edge, further reduced automatically by each neighboring frame's available slack. |
| `--grid-step-mm` | `0.125` | Shared manufacturing-grid spacing. |
| `--terrain-origin-m` | scanned | Shared NAVD88 datum. Automatic mode uses five metres below the exact cached minimum, rounded down. |
| `--terrain-relief-factor` | scanned | Shared terrain-only relief factor. Automatic selection uses a stratified citywide sample of the request. |
| `--skip-terrain-scan` | off | Fast planning smoke-test mode; uses `-50 m` and factor `1` unless explicitly supplied. Do not use for a final production plan. |
| `--geometric-only` | off | Skip semantic seam scoring. Intended for performance/geometry smoke tests, not final boundary selection. |
| `--offline` / `--no-offline` | offline | Whether emitted generator commands prohibit network access. Planning still requires completed local caches so all jobs share one frozen source snapshot. |
| `--full-validation` | off | Add each 3MF's expensive cross-material Boolean audit. Static and seam validation are always present. |
| `--slice` | off | Add an offline Bambu Studio slice to every generation command. |
| `--log-level` | `INFO` | Structured JSON event verbosity written to stdout and `logs/planner.jsonl`. |

Generator-compatible numeric constraints are checked before any plan is written. The planner also validates all cache manifests and every required semantic file, and parses every emitted argv through the real generator configuration builder.

## Output contract

```text
output/plans/<plan-id>/
  plan.json                         # exact request, layout, seams, frames and argv
  validation.json                   # independent static validation report
  post_generation_validation.json  # written after all models exist
  commands.sh                       # static check, jobs, required post-check
  chunks.geojson                    # all chunk footprints for GIS inspection
  preview.svg                       # labeled plan view
  logs/planner.jsonl                # structured planning stages and per-seam/per-command events
  chunks/<chunk-id>.geojson         # one bare Polygon per generator job
  frames/<chunk-id>.json            # explicit shared-grid print frame
```

The exact EPSG:2263 target is stored alongside the original normalized WGS84 request. This lets the validator detect a missing strip or hole independently; it does not define correctness as “the union of whatever chunk files happen to exist.” All paths and shell-safe argv are absolute in the plan.

## Validation and errors

[`scripts/validate_chunk_plan.py`](../scripts/validate_chunk_plan.py) emits structured issues with a stable code, severity, descriptive message and numeric/location context. Static validation covers:

- malformed/missing request, layout, frame, polygon or command data;
- chunk count and disconnected-component limits;
- gaps, coverage outside the request and pairwise/aggregate overlap;
- broken expected seams, unplanned internal seams and disconnected adjacency;
- shared scale, axes, grid phase, tight frame bounds and polygon containment;
- exact free-form logical-cell geometry and routed-seam geometry;
- polygon pieces with no manufacturing-grid cell center;
- exact agreement of every common generator option/flag and `commands.sh`;
- independent agreement between the WGS84 and EPSG:2263 request representations.

After generation, `--check-generated --require-generated` additionally verifies every job configuration and field shape, the realized terrain origin/factor, actual AOI, hidden command metadata, and all neighboring field edges. Ground and visible-surface heights are extrapolated to the physical seam from the two nearest valid cell centers. By default, a seam fails above 0.24 mm p95 or 0.72 mm maximum mismatch; localized values above 0.24 mm warn. Material disagreement warns above 5% of paired cells and fails above 25%. Tolerances can be changed explicitly on the validator CLI.

Examples of actionable diagnostics include `COVERAGE_GAP`, `CHUNK_INTERIOR_OVERLAP`, `BROKEN_INTERNAL_SEAM`, `FRAME_ORIGIN_OFF_SHARED_GRID`, `COMMAND_SHARED_OPTION_MISMATCH`, `CHUNK_HAS_NO_MANUFACTURING_CELLS`, `GENERATED_CONFIGURATION_MISMATCH`, `GENERATED_HEIGHT_SEAM_DISCONTINUITY` and `EMBEDDED_COMMAND_METADATA_MISMATCH`.

## Scaling to all NYC

Planning memory is proportional to the cached vector features near movable seam corridors, not to the number of 3MF mesh triangles. Terrain normalization streams one cached LiDAR tile at a time. Chunk geometry, coverage and adjacency validation use spatial indexes.

A rough NYC-wide bounding-box smoke test (`-74.26,40.49` to `-73.69,40.92`) completed as exactly 400 validated 235 × 235 mm chunks at 1:10,693.3. This is a geometry/scaling test, not a recommended city boundary: use an authoritative land/envelope polygon for production so ocean-only plates are not created. The same request with 300 chunks fails early with a descriptive capacity message because the required scale would exceed the per-job 30-million-cell elevation-grid limit.

For borough or city MultiPolygons, every disconnected printable piece counts toward `--max-chunks`. Sub-cell islands are never silently dropped: the request must use a more detailed scale/finer grid or explicitly simplify the source polygon. At full-city scale, first create a geometric-only smoke plan, then create the final semantic plan and keep its frozen cache manifests for every chunk.
