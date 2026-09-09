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
.venv/bin/python scripts/estimate_print_stats.py output/models/manhattan_2m_A2.3mf
```

`estimate_print_stats.py` reports per-filament grams, the share spent on
tool-change purge, cost, and the slicer's own time prediction. A generated 3MF
carries no such numbers, so the script runs an offline Bambu Studio slice and
caches it under the validation directory keyed by the input's SHA-256; a project
that already carries slice metadata is read directly. Pass several models to get
a combined per-color spool total, and `--json` to keep the machine-readable
form.

## Dataset scripts

Durable dataset scripts follow this naming and destination convention:

```text
download_X.py -> data/raw/X/       # transient source download
cache_X.py    -> data/cache/X/     # reusable generator input
```

For example:

```bash
.venv/bin/python scripts/download_nyc_land_cover_2017.py
.venv/bin/python scripts/cache_nyc_land_cover_2017.py
```

The building-footprint cache streams the official feature service by default;
its download script is needed only for the CSV mode. LiDAR has additional
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
