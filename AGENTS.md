# AGENTS.md

## Project overview

3D NYC converts public NYC geospatial datasets into four-material,
Bambu-compatible 3MF maps. `scripts/generate_3mf.py` is the supported
single-model entry point. `scripts/plan_map_chunks.py` plans neighboring plates,
and `scripts/validate_chunk_plan.py` validates those plans and generated seams.

Run commands from the repository root. Use Python 3.12 and install
`scripts/requirements.txt` in `.venv`.

## Documentation rules

- Keep the root `README.md` concise. It is a landing page with a short setup,
  representative commands, and links—not an exhaustive prerequisite, option,
  architecture, troubleshooting, or implementation reference.
- Put generation details in `docs/generate_3mf.md`, cache procedures in
  `docs/cache_source_datasets.md` or the LiDAR guide, chunk-planning details in
  `docs/plan_map_chunks.md`, and durable design context in the project summary.
- Update the relevant guide whenever behavior, defaults, paths, required data,
  output contracts, or CLI options change.
- Verify example commands against current `--help` output and repository paths.
  Examples must run from the repository root unless they explicitly say
  otherwise.
- Prefer durable statements. Do not present local cache counts, timings,
  checksums, snapshot dates, or ignored output files as project-wide facts.
- When exact reproduction matters, say which cache manifests, job manifests,
  and generated artifacts must be retained. Cache scripts rebuild live sources;
  they do not guarantee a byte-identical future snapshot.
- Keep `ATTRIBUTION.md` aligned with the datasets actually used and link to
  authoritative publisher or license pages.

## Code and data boundaries

- Keep user-facing orchestration in `generate_3mf.py`; individual extraction,
  field, mesh, render, package, and validation scripts are pipeline stages.
- Dataset pairs follow `download_X.py` to `data/raw/X/` and `cache_X.py` to
  `data/cache/X/`, with documented exceptions such as streamed Building
  Footprints and region-selective LiDAR.
- `data/raw/`, `data/cache/`, and `output/` are ignored, potentially large, and
  may contain expensive user-generated state. Do not delete or rebuild them
  unless the task explicitly requires it.
- Do not use ignored generated models, plans, logs, or local cache inventories
  as the sole evidence for a tracked documentation claim.
- Keep the default test suite hermetic: it must pass from a clean checkout with
  empty `data/raw/`, `data/cache/`, and `output/` directories. Tests for
  orchestration must inject explicit resolved inputs or use small tracked
  fixtures; they must not silently read a developer's ignored caches or create
  fake manifests marked production-ready. Put full real-data checks in an
  explicitly marked, separately provisioned integration workflow.
- Preserve the distinction between EPSG:2263 source geometry, metre-valued
  NAVD88 elevations, and millimetre-valued print geometry.

## Validation

Run the smallest relevant tests while iterating, then run the full suite before
handoff when practical:

```bash
.venv/bin/python -m pytest
```

CI runs the same command on Ubuntu with Python 3.12. For documentation changes,
also check local Markdown links, run affected CLIs with `--help`, and use
`git diff --check`.

Generating a complete 3MF requires source caches and installed Bambu Lab P2S
0.4 mm profiles; previews additionally use the platform renderer. Use parser,
unit, and validation tests when a full geospatial build would be disproportionate.

Do not claim that a model is physically printable solely because mesh or slicer
validation passed. Recommend a representative physical test for consequential
prints.
