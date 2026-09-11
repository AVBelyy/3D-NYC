# AGENTS.md

## Project overview

3D NYC converts public NYC geospatial datasets into four-material,
Bambu-compatible 3MF maps. `scripts/generate_3mf.py` is the supported
single-model entry point, and `scripts/plan_map_chunks.py` splits a larger
polygon into gap-free neighboring plates.

Run commands from the repository root. Use Python 3.12 and install
`scripts/requirements.txt` in `.venv`. That virtualenv is uv-managed and has no
`pip` in it, so install and add dependencies with `uv pip`. CI provisions its
own interpreter and installs the same file with plain `pip`; a step working
there is not evidence that `.venv` accepts it.

## Documentation rules

- Keep the root `README.md` concise. It is a landing page with a short setup,
  representative commands, and links—not an exhaustive prerequisite, option,
  architecture, troubleshooting, or implementation reference.
- Put generation details in `docs/generate_3mf.md`, multi-plate planning in
  `docs/plan_map_chunks.md`, and cache procedures in
  `docs/cache_source_datasets.md`, with the per-collection LiDAR procedures in
  `docs/cache_nyc_lidar_2021.md` and `docs/cache_nyc_lidar_2017.md`.
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
  Footprints and the borough-selective LiDAR collections. The generator reads
  the 2021 LiDAR cache by default; the 2017 collection is still reachable
  through `--lidar-source laz`. Land cover is a 2017/2021 pair chosen by
  `--land-cover-dataset`, and that choice is part of the stage variant.
- `data/raw/`, `data/cache/`, and `output/` are ignored, potentially large, and
  may contain expensive user-generated state. Do not delete or rebuild them
  unless the task explicitly requires it.
- Do not use ignored generated models, plans, logs, or local cache inventories
  as the sole evidence for a tracked documentation claim.
- `output/` holds artifacts a script produces for a named job, plan, or model,
  each under the directory that owns it: `output/jobs/<job-id>/` for a
  generation and everything derived from its model, `output/plans/<plan-id>/`
  for a chunk plan, `output/models/` for finished 3MFs. Nothing else belongs
  there. A model's sliced project, `<model>.gcode.3mf`, is the exception that
  proves the rule: it is a deliverable of the model rather than of a job, so it
  sits beside the model it was sliced from and doubles as that model's estimate
  cache. It holds toolpaths and no mesh, which is what makes Bambu Studio treat
  it as a sliced file to print rather than a project to reslice.
- A stage script run outside a job writes to a private temporary directory that
  is deleted when the process exits, because `map_common` resolves `MAP_WORK_DIR`,
  `NYC_VALID_DIR`, `NYC_PROCESSED_DIR`, and `NYC_ANALYSIS_DIR` to one per-process
  scratch directory when they are unset. Rely on that for one-off exploratory
  analysis — comparison slices, diagnostic renders, scratch reports, anything
  named for the question of the moment rather than for a job. Report the
  findings; do not leave the evidence behind in the project.
- Such a run also reads a default, which is the easier half to miss:
  `map_common` resolves `MAP_CONFIG` to `scripts/map_config.example.json` when
  it is unset, so a stage launched by hand measures the example prototype's AOI
  and scale rather than the chunk being investigated, and says nothing about the
  substitution. Point `MAP_CONFIG` at the job's own `config.json` before
  treating any standalone measurement as evidence about that job.
- When such a run does need to outlive it, say so explicitly: set those
  environment variables, or pass `--report-dir` or `--report`.
  Point them at a directory of your own such as `$(mktemp -d)`, never into
  `output/`. These artifacts are large, they are not reproducible from the
  repository, and a name like `diag_now` means nothing a week later.
- Keep the default test suite hermetic: it must pass from a clean checkout with
  empty `data/raw/`, `data/cache/`, and `output/` directories. Tests for
  orchestration must inject explicit resolved inputs or use small tracked
  fixtures; they must not silently read a developer's ignored caches or create
  fake manifests marked production-ready. Put full real-data checks in an
  explicitly marked, separately provisioned integration workflow.
- Printer specifics belong to the installed profile, not to code: the printer is
  named by `data/bambu/project_settings.json`, and its nozzle sizes, layer
  heights, machine and process preset names, and vendor model id are read from
  the profiles beside the Bambu Studio executable. Do not restate one printer's
  catalogue in a constant, and keep tests off it so they stay hermetic.
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

### Stage caching and version constants

A stage is skipped when the config hash, the stage `variant`, and the output
list all match its last completed run and those outputs still exist. The code
that produced them is never hashed, so a version constant is the only way a
stage can announce that its own outputs changed. Get that wrong and the job
reuses stale geometry, ships what the fix was meant to replace, and looks like
the fix did nothing.

- Bump every stage whose code changed, less any already downstream of another
  in that set. `stage_variants` embeds each stage's inputs in its key, so a bump
  restales everything below it; bumping those as well only re-extracts sources
  for nothing.
- Derive that set from what imports the module you edited, not from the stage
  you had in mind. `road_symbols` reaches `prepare_details`, `build_fields`,
  `validate_crossing_fields` and `build_meshes`; `crossings` reaches
  `build_fields` and `build_meshes`. One bump covers a chain, never a pair of
  branches: `validate_crossing_fields` and `build_meshes` both read the fields
  and neither reads the other, so a change to both needs
  `CROSSING_VALIDATION_VERSION` and `MESH_PIPELINE_VERSION`, and a mesh bump
  alone leaves crossing validation running the code you replaced.
- Shared code that feeds the hashed config bypasses the constants entirely.
  `crossings.structural_roof_thickness_mm` sets the tunnel-cover and
  bridge-deck bounds, and `PIPELINE_VERSION` travels there too, so changing
  either restales every stage of every job with no bump at all.
- Six stages have no constant. `prepare_vectors`, `extract_citygml` and
  `extract_osm` key only on their source cache identity, `prepare_landcover` on
  that plus the dataset name, `prepare_lidar` deliberately carries no key, and
  `render_preview` rides the mesh key. Changing what one of them builds
  invalidates nothing: say so in the change, and rerun with `--force`.
- `--force` reruns every stage of the job, expensive LiDAR and extraction
  included, so prefer the constants when an existing job should be rerun
  surgically. The two also differ in scope: `--force` is about the job in front
  of you, a bump says everyone else's cached jobs are stale too.
- A new stage needs its own `stage_variants` entry with its inputs declared, or
  it caches on the config alone and survives every later bump upstream of it.
  `tests/test_stage_cache_keys.py` is the executable spec: what each bump
  reaches, what it leaves alone, and that every wired stage declares a key.
- Confirm the rerun actually happened — the log says `stage_started`, not
  `stage_skipped`, and a field the new code writes is present — before
  concluding anything about whether a fix worked.

## Validation

Run the smallest relevant tests while iterating, then run the full suite before
handoff when practical:

```bash
.venv/bin/python -m pytest
```

That command skips the real-data integration tests and still exits zero, so a
green run is not on its own a full pass. They are gated on an environment
variable and need a populated `data/cache`:

```bash
NYC_CHUNK_INTEGRATION=1 .venv/bin/python -m pytest
```

CI runs the ungated command on Ubuntu with Python 3.12, so those tests do not
run there either; run them locally before handing off a change to planning,
chunk geometry, or the generator's argument contract. For documentation changes,
also check local Markdown links, run affected CLIs with `--help`, and use
`git diff --check`.

Generating a complete 3MF requires source caches and the profiles Bambu Studio
installs for the printer named in `data/bambu/project_settings.json`, for the
selected `--nozzle-mm` (the template's own nozzle by default). Use parser,
unit, and validation tests when a full geospatial build would be
disproportionate.

Do not claim that a model is physically printable solely because mesh or slicer
validation passed. Recommend a representative physical test for consequential
prints.
