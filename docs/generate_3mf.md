# Generate a 3MF

`scripts/generate_3mf.py` is the supported entry point. It selects cached NYC
source data, extracts the requested area, builds printable material solids,
packages Bambu project settings, validates the result, and records a
job provenance manifest.

Run it from the repository root in the Python 3.12 environment described in the
[project README](../README.md). Packaging always reads the installed Bambu Lab
P2S 0.4 mm profiles. Preview rendering additionally uses `xcrun` and `clang++`;
pass `--no-preview` when Xcode Command Line Tools are unavailable.

## Quick start

```bash
.venv/bin/python scripts/generate_3mf.py \
  --latitude 40.77945 \
  --longitude -73.96324 \
  --size-mm 200x200 \
  --output output/models/met_200.3mf
```

If `--output` is omitted, the model is written to
`output/models/<job-id>.3mf`. The checksum and optional preview are written
beside the model as `<model-stem>.sha256` and
`<model-stem>_preview.png`. Intermediate fields, extracted subsets, logs,
validation reports, and stage records go to `output/jobs/<job-id>/`.

## Crop forms

A centered rectangle uses `--latitude`, `--longitude`, and `--size-mm`.
An arbitrary WGS84 polygon uses WKT, inline GeoJSON, a path, or `@path`:

```bash
.venv/bin/python scripts/generate_3mf.py \
  --bounding-polygon @data/polygons/central_park.geojson \
  --length-mm 250
```

Use `scripts/plan_map_chunks.py` for a map that must be split across multiple
build plates; see [the planner guide](plan_map_chunks.md).

## Data and output roots

| Option | Default | Purpose |
| --- | --- | --- |
| `--data-dir` | `data/` | Checked-in and transient input root |
| `--cache-dir` | `data/cache/` | Per-dataset reusable caches |
| `--output-dir` | `output/` | Jobs, default models, plans, and other generated files |
| `--lidar-cache-dir` | `<cache-dir>/nyc_lidar_2017` | Optional LiDAR cache override |
| `--project-settings-template` | `data/bambu/project_settings.json` | Bambu project-settings input |

`--output-dir` controls the generated-data root. It does not change an explicit
`--output` path.

The cache directories are named for their exact source datasets. See
[the source-cache runbook](cache_source_datasets.md) for the full
`download_X.py -> data/raw/X` and `cache_X.py -> data/cache/X` mapping.

## Network and cache behavior

The default `--lidar-source cache` requires a completed LiDAR raster cache that
covers the crop plus source padding; the generator does not build that cache on
demand. For every other dataset, a complete production-ready cache is
preferred. If a cache is absent, an online run downloads the matching raw
source and processes the crop. A present but incomplete or non-production-ready
cache is an error rather than a signal to fall back silently.

Use `--offline` to forbid all network access. Offline generation may use
completed caches or already-downloaded raw sources, but the default LiDAR mode
still requires its raster cache. `--lidar-source laz` instead uses raw LAZ tiles
for the selected crop and is intended mainly for diagnostics.

Address-based building colors use NYC Planning GeoSearch. Responses are cached
under `data/cache/nyc_geosearch/`; repeated runs reuse them, and an offline run
requires the exact address response to have been cached already. Coordinate,
BIN, DoITT ID, and BBL selectors do not use GeoSearch.

## Useful controls

- `--scale` sets the map-scale denominator. For polygon crops, `--length-mm`
  derives a scale that makes the longer oriented side the requested size.
- `--vertical-exaggeration` scales measured vertical relief.
- `--terrain-relief-factor` explicitly scales ground relief; otherwise the
  generator enforces its minimum printable relief budget automatically.
- `--layer-height` selects an installed P2S process layer height.
- `--building-colors` assigns selected buildings to the four existing
  material colors using JSON identifiers.
- `--prime-tower auto` enables the fixed tower only when the centered model and
  both brim envelopes fit. It is off for a 200 x 200 mm square and can remain on
  for sufficiently narrow models.
- `--no-preview` skips preview rendering.
- `--full-validation` enables expensive cross-material checks.
- `--slice` performs an optional Bambu Studio validation slice.
- `--force` reruns every stage for an existing job ID.

Run `scripts/generate_3mf.py --help` for the complete option reference.

For example, select a visible building by BIN without calling GeoSearch:

```bash
.venv/bin/python scripts/generate_3mf.py \
  --latitude 40.7127 \
  --longitude -74.0060 \
  --building-colors '[{"bin":"1079147","color":"green"}]'
```

This selects New York City Hall in the example crop. A selector that matches no
visible footprint fails rather than being ignored.

## Print styling and support

The four materials are arranged around a continuous ivory substrate. Green,
blue, and tan surface regions begin only near the visible surface, while
buildings and raised tree crowns retain their selected color above that
substrate. This keeps the lower tile edge ivory and ensures that shallow tan
sidewalk regions are made from solid shell layers rather than exposing sparse
infill. Adjacent chunks must still use the shared print frame and terrain origin
emitted by the chunk planner so their edge geometry agrees.

Green ground comes from mapped park polygons and from the land-cover survey.
The survey records bare soil wherever a field is worn, shaded, seasonal, or
freshly graded, so a lawn arrives as vegetation stippled with unvegetated
ground. Bare soil that is continuous with a mostly vegetated region is treated
as the same field and prints green; ground that is predominantly unvegetated —
a construction site, a dirt lot, a beach — stays ivory even where it abuts a
lawn. This matters most outside mapped parks, where land cover is the only
evidence the model has for green.

Roads use printable ivory centerline ribbons: 0.5 mm for ordinary streets and
0.625 mm for major roads. Physical roadbed width is retained as source metadata
but does not widen the visible symbol. Outside parks, measured roadbeds and
authoritative sidewalks form the tan street field; inside parks, road shoulders
remain green. Park paths, plazas, and surface parking are tan, and categorical
trails never become ivory roads.

A surveyed transport structure is claimed by proximity, so every way on a
viaduct matches the same deck polygon: the roadway, and the footway or cycleway
OSM maps beside it. Each structure is attributed to the way it carries — a
trail keeps a structure only where it runs further along it than any
carriageway does — so a viaduct sidewalk yields the deck to the road it flanks
while a genuine footbridge keeps its own. Every matched structure still
supplies elevation evidence regardless of who claims its surface. Where decks
still overlap, the carriageway color wins, matching the road-over-tan priority
already applied at kerbs; bridge decks are therefore ranked by class rather
than by OSM source order.

LiDAR upper-surface heights define the varied canopy relief. The generator
closes only narrow gaps in the canopy classification, smooths measured heights
using a source halo, and rolls remaining canopy edges down over a print-scaled
distance. Mapped trails are applied after gap closing and receive a clear canopy
setback, so paths remain distinct instead of becoming dark fissures between
near-vertical tree masses.

Tunnel and underpass openings retain a full-width lower route, but their hidden
apertures are narrowed to at most three nozzle widths, leaving grounded
shoulders. Permanent bridge roofs are layer-aligned, at least three layers and
1.5 nozzle widths thick. These are modeled supports rather than removable
slicer support. A lower route must also retain at least one complete printed
layer above the base; an opening that cannot satisfy all three constraints is
left solid.

## Reproducibility

Each job records normalized configuration, cache identities, source subsets,
stage completion records, structured logs, validation output, and the final
checksum under `output/jobs/<job-id>/`. The 3MF embeds the shell-safe generation
command and packaged Bambu project settings, but the full normalized generation
configuration remains in the job directory. `output/` is disposable and
Git-ignored; preserve the job directory together with a model when exact
provenance matters.
