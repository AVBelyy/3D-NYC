# Scripts

Generate one printable NYC map with:

```bash
.venv/bin/python scripts/generate_3mf.py \
  --latitude 40.77945 --longitude -73.96324 \
  --size-mm 200x200 \
  --output output/models/met_200.3mf
```

The generator reads stable inputs from `data/`, uses dataset caches under
`data/cache/`, and writes all run products under `output/`. Override that root
with `--output-dir`; override the cache root with `--cache-dir`.

## Dataset scripts

For every durable dataset `X`, the names and destinations match:

```text
download_X.py -> data/raw/X/       # transient source download
cache_X.py    -> data/cache/X/     # reusable generator input
```

For example:

```bash
.venv/bin/python scripts/download_nyc_land_cover_2017.py
.venv/bin/python scripts/cache_nyc_land_cover_2017.py
rm -rf data/raw/nyc_land_cover_2017  # safe after the cache completes
```

See [the cache runbook](../docs/cache_source_datasets.md) for the complete
mapping. `cache_common.py`, `_datasets.py`, `_download_dataset.py`, and
`_cache_vector_datasets.py` are shared implementation modules rather than
entry points.

## Other entry points

- `plan_map_chunks.py` plans a multi-piece map and writes the plan to
  `output/plans/`.
- `validate_chunk_plan.py` validates a generated plan.
- `generate_3mf.py` orchestrates extraction, geometry, packaging, and
  validation. The other build scripts are internal pipeline stages.

Run the tests with:

```bash
.venv/bin/python -m pytest
```
