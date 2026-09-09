# AGENTS.md

## Project overview

3D NYC converts public NYC geospatial datasets into four-material,
Bambu-compatible 3MF maps. `scripts/generate_3mf.py` is the supported
single-model entry point, and `scripts/plan_map_chunks.py` splits a larger
polygon into gap-free neighboring plates.

Run commands from the repository root. Use Python 3.12 and install
`scripts/requirements.txt` in `.venv`.

## Documentation rules

- Keep the root `README.md` concise. It is a landing page with a short setup,
  representative commands, and links—not an exhaustive prerequisite, option,
  architecture, troubleshooting, or implementation reference.
- Put generation details in `docs/generate_3mf.md`, multi-plate planning in
  `docs/plan_map_chunks.md`, and cache procedures in
  `docs/cache_source_datasets.md`.
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

- Keep user-facing orchestration in `generate_3mf.py` and `plan_map_chunks.py`;
  individual extraction, field, mesh, render, package, and validation scripts
  are pipeline stages. Give a new module that is not a command-line entry point
  a `_` prefix. Six shared modules predate that rule and keep unprefixed names:
  `cache_common`, `map_common`, `crossings`, `road_symbols`, `terrain_relief`,
  and `mesh_precision`. Import-only is what makes a module internal, not the
  name; do not add a `__main__` guard to one to settle the question.
- Dataset pairs follow `download_X.py` to `data/raw/X/` and `cache_X.py` to
  `data/cache/X/`, with documented exceptions such as streamed Building
  Footprints and region-selective LiDAR.
- `data/raw/`, `data/cache/`, and `output/` are ignored, potentially large, and
  may contain expensive user-generated state. Do not delete or rebuild them
  unless the task explicitly requires it.
- Do not use ignored generated models, plans, logs, or local cache inventories
  as the sole evidence for a tracked documentation claim.
- `output/` holds artifacts a script produces for a named job, plan, or model,
  each under the directory that owns it: `output/jobs/<job-id>/` for a
  generation and everything derived from its model, `output/plans/<plan-id>/`
  for a chunk plan, `output/models/` for finished 3MFs. Nothing else belongs
  there.
- A stage script run outside a job writes to a private temporary directory that
  is deleted when the process exits, because `map_common` resolves `MAP_WORK_DIR`,
  `NYC_VALID_DIR`, `NYC_PROCESSED_DIR`, and `NYC_ANALYSIS_DIR` to one per-process
  scratch directory when they are unset. Rely on that for one-off exploratory
  analysis — comparison slices, diagnostic renders, scratch reports, anything
  named for the question of the moment rather than for a job. Report the
  findings; do not leave the evidence behind in the project.
- When such a run does need to outlive it, say so explicitly: set those
  environment variables, or pass `--slice-dir`, `--report-dir`, or `--report`.
  Point them at a directory of your own such as `$(mktemp -d)`, never into
  `output/`. These artifacts are large, they are not reproducible from the
  repository, and a name like `diag_now` means nothing a week later.
- Keep the default test suite hermetic: it must pass from a clean checkout with
  empty `data/raw/`, `data/cache/`, and `output/` directories. Tests for
  orchestration must inject explicit resolved inputs or use small tracked
  fixtures; they must not silently read a developer's ignored caches or create
  fake manifests marked production-ready. Put full real-data checks in an
  explicitly marked, separately provisioned integration workflow.
- Preserve the distinction between EPSG:2263 source geometry, metre-valued
  NAVD88 elevations, and millimetre-valued print geometry.

## Fixing generation failures

A generation failure always arrives as one chunk, one scale, and one seed, but
it is never a fact about that chunk. Fix the rule, not the instance.

- Diagnose the invariant the failing stage was defending and decide whether the
  geometry actually violates it. Failures that surface only at map scale are
  usually a threshold applied to the wrong measure, not corrupt data.
- Prefer fixes derived from print physics and the pipeline's own numerical
  bounds — nozzle width, layer height, simplify and export-grid motion — over
  tuned constants. A constant chosen to clear one chunk will fail the next.
- Keep a single definition of any bound that more than one stage tests, and
  make every stage that judges the same quantity agree.
- Never special-case a chunk id, bounding polygon, OSM id, plan, or borough,
  and never widen a tolerance merely to get past one plate. Loosening a real
  safety bound is a modeling decision, not a fix; say so and stop.
- Repairs that only improve geometry must degrade to keeping the input when
  they cannot run. Reserve hard failures for output that is actually
  unacceptable, judged by the same rule the validator applies.
- Cover the fix with a hermetic test that reproduces the failing measurements,
  and confirm the symmetric case — a genuine defect of the same shape must
  still be rejected. Reproducing on the original chunk is evidence, not proof.
- Stage caching in `generate_3mf.py` keys on the config hash and the stage
  `variant` only; it never hashes the code that produced the outputs. Changing
  what a stage builds therefore requires bumping that stage's version constant,
  or existing jobs silently reuse stale outputs and appear to ignore the fix.
  Confirm a rerun really re-executed the stage — check the log for
  `stage_started`, not `stage_skipped`, and look for a field the new code
  writes — before concluding anything about whether a fix worked.

## Validation

Run the smallest relevant tests while iterating, then run the full suite before
handoff when practical:

```bash
.venv/bin/python -m pytest
```

CI runs the same command on Ubuntu with Python 3.12. For documentation changes,
also check local Markdown links, run affected CLIs with `--help`, and use
`git diff --check`.

Generating a complete 3MF requires source caches and the installed Bambu Lab
P2S profiles for the selected `--nozzle-mm` (0.4 mm by default). Use parser,
unit, and validation tests when a full geospatial build would be
disproportionate.

Do not claim that a model is physically printable solely because mesh or slicer
validation passed. Recommend a representative physical test for consequential
prints.
