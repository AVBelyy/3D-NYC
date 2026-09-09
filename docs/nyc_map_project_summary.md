# NYC printable map: design summary

This project turns public NYC datasets into four-material, Bambu-compatible
3MF maps. It can generate one coordinate- or polygon-based model, or plan a
larger polygon as gap-free neighboring plates. Source access does not require a
paid API, but downloads, storage, slicing, and printing still use local
resources.

The software validates geometry and can ask Bambu Studio to perform an offline
slice. It does not connect to a printer, and a successful slice is not a
substitute for a representative physical test.

## Printed model

| Material | Modeled content |
| --- | --- |
| Ivory | Road ribbons, default buildings, roof fixtures, structural bridge roofs, and the hidden substrate by default |
| Green | Vegetated terrain, recreation areas, parks, institutional lawns and commons, gardens, grass, smoothed measured canopy, and selected buildings |
| Blue | Level water surfaces and selected buildings |
| Tan | Sidewalks, paths, plazas, surface parking, and selected buildings |

The generator retains detailed 2014 CityGML roofs where available, supplements
them with maintained building footprints and Planimetrics fixtures, and uses
LiDAR for ground and upper-surface measurements. Roads, paths, land use,
bridges, tunnels, and current semantic context combine NYC sources with
OpenStreetMap. Classified carriageway centerlines become fixed-width ivory
ribbons, drawn as raised lines standing above the pavement pad rather than as
color painted onto it, so a junction or kerb overlap reads as a continuous
street; measured urban roadbeds and sidewalk polygons are tan, while park-road
shoulders remain green.

Every drawn width is floored at what the selected nozzle can lay, and a color
region the nozzle cannot draw at all is absorbed into the neighbor that
surrounds it rather than widened at that neighbor's expense. `--nozzle-mm`
selects the installed P2S nozzle -- 0.2, 0.4 (the default), 0.6, or 0.8 mm --
and sets that printable minimum.

The substrate every visible surface is seated on is the largest share of the
print by volume and none of its cartography. `--foundation-color` chooses which
of the four filaments it consumes; ivory is the default, and the only visible
consequence is the lower tile edge.

Building-color overrides reuse the four installed materials; they do not add a
fifth material. Address selectors use NYC Planning GeoSearch, while coordinate,
BIN, DoITT ID, and BBL selectors are resolved from local source data.

## Pipeline

`scripts/generate_3mf.py` is the supported single-model entry point. It:

1. normalizes the crop, scale, print frame, layer height, and material choices;
2. validates the required cache manifests or prepares permitted raw inputs;
3. extracts source subsets and derives terrain, surface, building, and detail
   fields on a shared manufacturing grid;
4. builds mutually exclusive, printable material solids;
5. renders an optional preview, packages project settings and generation
   metadata, validates the 3MF, and optionally asks Bambu Studio to slice it.

The model, checksum, and preview are written under `output/models/` by default.
The normalized configuration, source identities, subsets, intermediate fields,
logs, validation reports, and stage records live under `output/jobs/<job-id>/`.
Because `output/` is ignored by Git, preserve the job directory with any model
that is published as a release artifact.

For exact commands and prerequisites, use the
[generation guide](generate_3mf.md). For larger assemblies, use the
[chunk-planning guide](plan_map_chunks.md).

## Data and reproducibility

Most geometry is processed in EPSG:2263. LiDAR elevations are stored in metres
relative to NAVD88, and final model dimensions are millimetres. Generated job
metadata records the transformations and normalization choices used.

The source datasets represent different collection dates and are not a single
time-consistent survey. OpenStreetMap and several NYC endpoints also change in
place. The cache scripts make a run rebuildable, but only retained cache and
job manifests identify the exact snapshot used for a particular output.

The default elevation path uses canonical 0.5 m cache rasters: mean class-2
ground and maximum class 1/2/17/25 upper surface. The measured upper surface
defines varied canopy relief; narrow source gaps are closed and remaining edges
are rolled down, while mapped trails retain a print-scaled canopy setback.
Hidden tunnel profiles, unmeasured fixture heights, and some water levels are
explicit inferences rather than underground or architectural surveys.

## Validation guarantees

The generation pipeline checks source cardinalities against independent
context, rejects out-of-bounds or degenerate geometry, and requires each
material mesh to be watertight, consistently wound, and positive-volume.
Colored surface solids are seated into the continuous substrate, then all
materials are made mutually exclusive after simplification and manufacturing-
grid snapping. Crossing validation also requires layer-aligned permanent roofs
at least three layers thick and limits unsupported apertures to three nozzle
widths. Full validation adds more expensive pairwise intersection checks.

The 3MF embeds a shell-safe generation command and the packaged Bambu project
settings. The full normalized configuration remains in the job directory. An
optional Bambu Studio slice verifies that the archive can be consumed with the
installed P2S profiles for the selected nozzle; it does not certify adhesion,
color transitions, surface finish, or unsupported features.

Each nozzle ships its own process layer heights, so `--layer-height` is
validated against the selected `--nozzle-mm` rather than against one global
list. At the default 0.4 mm nozzle they are 0.08, 0.12, 0.16, 0.20, and 0.24 mm,
and 0.24 mm is the default. Prime-tower `auto` enables the fixed tower only when
the centered model and both brim envelopes fit. It is therefore off for the
default 200 x 200 mm square. Tower-free models may be as large as 250 x 250 mm,
subject to the generator's grid and elevation-cell limits.

## Known limitations

- No free source guarantees current, ornament-level geometry for every
  building; newer or changed roofs can be simplified.
- Narrow paths, fixtures, road symbols, and entrance markers may be enlarged to
  remain printable at the selected nozzle, and a color region still too thin to
  print is absorbed into its neighbor rather than drawn.
- Inferred hidden roads are not measured underground geometry.
- The project has no general monument catalog and does not invent unsupported
  landmark geometry.
- Validation cannot predict every printer, filament, or slicer outcome. Print a
  small representative tile before committing to a large assembly.

Do not treat local cache counts, generation timings, triangle totals, or model
checksums as project-wide constants. Read them from the manifest and validation
files belonging to the job being evaluated.
