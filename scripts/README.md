# Scripts

Run scripts from the repository root with the project's Python 3.12
environment.

## Main entry points

Generate one printable map:

```bash
.venv/bin/python scripts/generate_3mf.py \
  --latitude 40.77945 \
  --longitude -73.96324 \
  --size-mm 200x200 \
  --output output/models/met_200.3mf
```

`generate_3mf.py` orchestrates source preparation, geometry, preview,
packaging, validation, and optional slicing. Use its `--help` output and the
[generation guide](../docs/generate_3mf.md) for supported options.

Plan a map too large for one plate:

```bash
.venv/bin/python scripts/plan_map_chunks.py \
  --bounding-polygon @data/polygons/manhattan_island.geojson \
  --scale 10533 \
  --max-chunks 30
```

`plan_map_chunks.py` partitions the polygon, chooses seams from cached
elevation and land-cover evidence, and writes `generate_3mf.py` commands plus a
preview under `output/plans/`. See the
[planner guide](../docs/plan_map_chunks.md).

Estimate filament and print time for finished models:

```bash
.venv/bin/python scripts/compute_print_stats.py output/models/manhattan_2m_A2.3mf
```

`compute_print_stats.py` reports per-filament grams, the share spent on
tool-change purge, cost, and the slicer's own time prediction. A generated 3MF
carries no such numbers, so the script runs an offline Bambu Studio slice and
writes it beside the model as `<model>.gcode.3mf`, a sliced file that Bambu
Studio opens ready to print without reslicing. That file carries the toolpaths
and the plate but no mesh: a 3MF that still holds its objects opens as a project
to edit, and Studio then discards the G-code and reslices. The mesh stays in the
`<model>.3mf` beside it. That project is also the cache:
a rerun reuses it while it still matches the model's SHA-256, and no loose
G-code is left behind. A project that already carries slice metadata is read
directly. A batch resolves its cached models first and shows a progress bar
only for the models it still has to slice, so the time remaining counts real
work and a fully cached batch prints no bar at all.

Several models print a table each and then the batch as one print: per-filament
totals, the purge share, the spread of per-model times, and any grams the slicer
never split into model and purge. `--summary-only` prints that batch block
alone, and `--json` writes both forms as `{"entries": [...], "summary": {...}}`.
`--update-preview output/plans/<plan-id>/preview.svg` captions a planner
preview: each plate's label card grows to hold its own print time and filament
weight, and a band above the map carries the batch totals — split into model
material and purge — with a chip per filament. Rerunning replaces those captions rather than stacking
them.

## Dataset scripts

Durable dataset scripts follow this naming and destination convention:

```text
download_X.py -> data/raw/X/       # transient source download
cache_X.py    -> data/cache/X/     # reusable generator input
```

For example:

```bash
.venv/bin/python scripts/download_nyc_land_cover_2021.py
.venv/bin/python scripts/cache_nyc_land_cover_2021.py
```

`download_all.sh` and `cache_all.sh` run every dataset pair, with no area to
choose:

```bash
scripts/download_all.sh
scripts/cache_all.sh
```

They cover all of NYC, which is the extent of the LiDAR tile index, and LiDAR
is built first because the other builders take their coverage from its catalog.
Caching a smaller area means running the individual pairs; see the
[cache runbook](../docs/cache_source_datasets.md).

The building-footprint cache is always built by streaming the official feature
service; its downloaded CSV is a `generate_3mf.py` fallback, not a cache input.
LiDAR has additional
coverage and streaming controls. Use the [cache runbook](../docs/cache_source_datasets.md)
instead of assuming every dataset has identical requirements.

Do not delete raw inputs until the matching cache has a complete manifest.
Retain both cache and job manifests when an exact source snapshot must be
reproduced, because several upstream sources change over time.

## Internal modules

`generate_3mf.py` invokes the extraction, field-building, mesh, rendering,
packaging, 3MF validation, slicing, and bridge-validation scripts with a
generated configuration and environment. Treat those stage scripts as pipeline
internals unless you are debugging a specific stage.

`cache_common.py`, `_datasets.py`, `_download_dataset.py`,
`_cache_vector_datasets.py`, `_chunk_geometry.py`, `_chunk_cost.py`, and
`_chunk_preview.py` are shared implementation modules, not command-line entry
points.

Run the test suite with:

```bash
.venv/bin/python -m pytest
```
