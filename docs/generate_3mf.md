# Generate a 3MF

`scripts/generate_3mf.py` is the supported entry point. It selects cached NYC
source data, extracts the requested area, builds printable material solids,
packages Bambu project settings, validates the result, and records a
reproducible job manifest.

## Quick start

```bash
.venv/bin/python scripts/generate_3mf.py \
  --latitude 40.77945 \
  --longitude -73.96324 \
  --size-mm 200x200 \
  --output output/models/met_200.3mf
```

If `--output` is omitted, the model is written to
`output/models/<job-id>.3mf`. All intermediate fields, extracted subsets,
logs, previews, validation reports, and stage records go to
`output/jobs/<job-id>/`.

## Crop forms

A centered rectangle uses `--latitude`, `--longitude`, and `--size-mm`.
An arbitrary WGS84 polygon uses WKT, inline GeoJSON, a path, or `@path`:

```bash
.venv/bin/python scripts/generate_3mf.py \
  --bounding-polygon @data/polygons/central_park.geojson \
  --length-mm 250
```

Use `scripts/plan_map_chunks.py` for a larger map that must be split across
multiple build plates; see [the planner guide](plan_map_chunks.md).

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

By default, complete dataset caches are preferred. Missing source files may be
downloaded into the matching `data/raw/X/` directory. Use `--offline` to
forbid all network access and require the caches needed by the selected crop.

Address-based building colors use NYC Planning GeoSearch. Responses are
cached under `data/cache/nyc_geosearch/`; repeated and offline runs reuse them.

Elevation defaults to `--lidar-source cache`. `--lidar-source laz` uses raw
LAZ tiles for the selected crop and is mainly useful for diagnostics.

## Useful controls

- `--scale` or `--length-mm` controls map extent/printed size.
- `--vertical-exaggeration` scales measured vertical relief.
- `--terrain-relief-factor` explicitly scales ground relief; otherwise the
  generator enforces its minimum printable relief budget automatically.
- `--layer-height` selects an installed P2S process layer height.
- `--building-colors` assigns selected buildings to the four existing
  material colors using JSON identifiers.
- `--no-preview` skips preview rendering.
- `--full-validation` enables expensive cross-material checks.
- `--slice` performs an optional Bambu Studio validation slice.
- `--force` reruns every stage for an existing job ID.

Run `scripts/generate_3mf.py --help` for the complete option reference.

## Reproducibility

Each job records normalized configuration, cache identities, source subsets,
stage completion records, structured logs, validation output, and the final
checksum under `output/jobs/<job-id>/`. The 3MF also embeds its generation
configuration. `output/` is disposable and Git-ignored; preserve or publish a
specific model separately if it is a release artifact.
