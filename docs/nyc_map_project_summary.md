# NYC printable map: project summary

**Status: a free-data, four-color NYC workflow now generates Bambu-compatible 3MF files from coordinates. Data/API cost to date: $0. A physical calibration print is still required.**

## What was built

The workflow combines official NYC geometry with OpenStreetMap and turns it into four material solids:

| Printed layer | Color | Primary evidence | Treatment |
|---|---|---|---|
| Buildings, roofs, roof fixtures, road cores and subway entrance caps | Ivory | 2014 NYC CityGML, maintained BUILDING footprints, 2022 Planimetrics, 2017 LiDAR, OSM, MTA entrances | Detailed roofs are retained; current footprints fill gaps; tanks/cooling towers supplement roofs; motor roads and motor-road bridge decks receive a thin ivory inset. |
| Terrain, grass, recreation areas, gardens and tree canopy | Green | 2017 LiDAR, 2017 six-inch land cover and OSM | Ground is reconstructed from returns; playgrounds, pitches and tracks are semantic green regardless of surface. Gardens, dog runs and mapped grass supplement land cover; canopy relief is measured and remains visible over open OSM parking. |
| Water | Blue | Planimetric hydrography and OSM | Water is flattened to a local measured or inferred level. The blue object is omitted when a tile contains no water. |
| Roads, sidewalks, plazas, surface parking, paths and crossing floors | Tan | 2022 Planimetrics, OSM and Parks Trails | Actual city polygons are preferred; open OSM parking fills gaps in Planimetrics, while structured/covered parking is excluded. Narrow paths are widened only to the printable minimum. Tunnels and water bridges are separate levels. |

The result includes sloping and stepped roofs, rooftop tanks and cooling towers, retaining-wall caps, stairs, paths, dense canopy relief, lakes, bridges, open transverse-road tunnels, ivory motor-road surfaces and authoritative exterior subway-entrance markers. All source access is free. See the [generator guide](generate_3mf.md) for the active source and processing pipeline.

## Data findings that drive the design

- The 2014 3D model contains 1,083,437 building objects and 1,582,438 roof surfaces. The AMNH analysis area alone has 9,677 roof polygons, including 998 with varying elevation. This is much richer than simple footprint extrusion.
- Maintained footprints are needed for newer or changed buildings. Their publisher-projected EPSG:2263 export aligns with the CityGML far better than locally reprojecting the WGS84 CSV. The offline CSV fallback applies the audited dataset offset before use.
- Planimetrics contains more than three million features, including roadbeds, sidewalks, curbs, stairs, retaining walls, water tanks, cooling towers, bridge/portal geometry and typed elevation points.
- The supplied Planimetrics FileGDB has damaged spatial indexes for some layers. ROADBED returned a false empty bbox result in the first City Hall run despite 104,961 citywide features. The generator now falls back to a full layer scan and local spatial filter whenever an indexed query unexpectedly returns empty.
- ArcGIS building GeoJSON requested in EPSG:2263 was labeled WGS84 by the reader even though its numeric coordinates remained projected feet. The first City Hall run therefore filtered out 1,136 returned buildings. The extractor now applies the requested output CRS explicitly and falls back to the downloaded CSV when the API is unavailable or empty.
- The 2017 LiDAR point cloud does not separate buildings and vegetation into conventional classes. The model therefore combines upper returns with building and land-cover masks. It does not treat every upper return as a tree.
- The municipal tree inventory is far too sparse in Central Park to reconstruct the visible canopy and is not an input to the current generator. LiDAR plus land cover provides the tree canopy instead.
- OSM/Parks paths remain useful under trees, where imagery and surface-height inference are unreliable. OSM tunnel and layer tags also keep transverse roads below the terrain instead of draping them over it.
- For car roads on bridges, OSM supplies the free, current road/bridge semantics and centerline while NYC Planimetrics supplies the authoritative local deck polygon and measured Z. The combined core is clipped to the deck; pedestrian bridges remain tan. Decks are painted first and ivory cores last because several OSM carriageway ways may share one Planimetrics structure polygon.
- Full-city LiDAR is unnecessary for local prints. The generator reads the
  compact cached 0.5 m rasters by default and mosaics only intersecting
  tiles; `--lidar-source laz` retains the original selection of intersecting
  LAZ tiles from the 1,894-tile catalog. The complete point archive is about
  109.5 GB; the selected AMNH analysis used 12 tiles.
- Source dates are mixed: detailed roofs are principally 2014, LiDAR/land cover 2017, Planimetrics release 2022, and maintained footprints/OSM are newer snapshots. The retained OSM PBF has replication timestamp `2026-08-29T20:21:36Z`, about two days old at final validation. Recent buildings may have only a footprint and scalar height.

## AMNH model and print-time experiments

The detailed AMNH model established that the data can reproduce the reference's main visual vocabulary. Generated models and previews now live under the Git-ignored `output/models/` directory.

| Model/process | Exact Bambu estimate | Filament | Changes | Result |
|---|---:|---:|---:|---|
| Original detailed model, 0.08 mm High Quality, 2 walls, 12% infill | 73 h 04 m 36 s | 342.31 g | 238 | Quality reference; too slow. |
| Same geometry, official 0.16 mm Standard, 2 walls, 5% infill | 32 h 33 m 57 s | — | — | Still over target. |
| Enhanced model, official 0.24 mm Standard, 2 walls, 5% infill | **22 h 01 m 39 s** | **259.19 g** | **78** | Selected. 51 h 02 m 57 s faster than the baseline. |

The selected model keeps its 200 × 200 mm crop, 0.125 mm XY grid, two walls, all material boundaries and all detail layers. The time reduction comes mainly from fewer vertical layers. The worst-case height quantization changes from about ±0.04 mm at 0.08 mm layers to ±0.12 mm at 0.24 mm. The installed official P2S profiles stop at 0.24 mm; a 0.28 mm profile would be a separate untested custom process.

The thin ivory road treatment matches the useful visual distinction in the Lichtbild reference: an inset ivory carriageway sits 0.16 mm above tan road shoulders, while park trails remain tan. The AMNH crop also adds eight exterior MTA entrance caps. It does not invent a visible outdoor structure for the entrance record located inside the museum.

All four AMNH material meshes are watertight and consistently wound. Cross-material booleans, union/cavity tests, slicer output and 22,132 printable G-code islands were checked. No eligible island or exposed-surface patch was missing. Bambu Studio still reports floating regions on that very complex model; a physical calibration print is the remaining test.

The comb-like vertical lines visible on close preview renders are triangle edges/faceted shading along grid-sampled walls. The buildings are closed filled solids, not bundles of disconnected sticks. A sliced print receives outer and inner perimeter walls plus infill. Some fine vertical texture can remain at close range because the outline follows the 0.125 mm XY grid, but the walls will be continuous.

## Tree simplification experiment

The tree hypothesis was tested while holding the canopy footprint, total canopy volume, scale, buildings, roads, water and slicer process constant.

| Canopy relief | Exact estimate | Saving from selected model | Visual cost |
|---|---:|---:|---|
| Original measured relief | 22 h 01 m 39 s | — | Reference. |
| Moderate: 4 m source smoothing, 0.5 mm control grid | 21 h 37 m 27 s | 24 m 12 s (1.83%) | Loses fine crown separation. |
| Aggressive: 8 m source smoothing, 1.0 mm control grid | 21 h 34 m 20 s | 27 m 19 s (2.07%) | Clearly smoother and less tree-like. |

This saving is too small to justify the loss of the model's strongest visual detail. The completely flat-canopy control was generated but its slice was interrupted, so no timing claim is made for it.

## Prime tower and maximum tile size

The prime tower stabilizes nozzle pressure and color after AMS material changes. It does not eliminate purging: the AMNH estimate attributes 14 m 17 s to the tower and 1 h 44 m 28 s to flushing. Disabling it therefore frees plate area but is not a major time optimization and can worsen color transitions.

For 200 mm tiles the generator leaves the tower enabled by default. For larger tiles it disables the tower automatically and centers the model. The supported maximum is **250 × 250 mm**, leaving 3 mm around the nominal 256 mm plate; at that limit the outer brim is reduced to 2.8 mm plus a 0.1 mm gap so it remains inside the bed. A 256 × 256 model leaves no practical room for dimensional tolerance or a brim. Large tower-free multicolor tiles need a small physical purge/color-change test before committing to a long print.

## Remaining quality gaps

- No free source guarantees current, ornament-level geometry for every building. Recent roofs often remain simplified.
- Hidden tunnel floors are inferred between observed approaches; they are not measured underground surveys.
- Tree crowns are a continuous LiDAR-derived relief, not botanically segmented individual trees.
- No monument catalog contributes geometry to the current workflow; unsupported monuments are not placed speculatively.
- Minimum printable widths deliberately enlarge thin paths, fixtures and entrance markers at this scale.
- Mesh and slicer validation cannot certify bed adhesion, color bleed, unsupported surfaces or wall finish. Print a small representative tile first.

## General generator safeguards added during City Hall validation

- Every final material mesh is clipped to the requested local XY footprint. This fixed a bridge-water substrate that previously extended from `y=-50.49` to `x=255.16` mm on a 200 mm tile.
- Internal 3MF object members are derived from the output name; validators discover them from the package relationships rather than assuming an AMNH filename.
- Building, CityGML roof and roadbed cardinalities are cross-checked against independent OSM evidence before a mesh can be accepted.
- Full validation uses a print-resolution overlap tolerance: the larger of 0.05 mm³ or two nozzle-width-squared layer volumes. It still records every pairwise intersection volume.
- Final materials are made mutually exclusive after independent simplification and export-grid snapping. The mesh report records the removed overlap volume and effective thickness for every color; an intersection thicker than the combined simplification-motion bound is rejected instead of being silently assigned to one filament.
- Intermediate PLY meshes retain 64-bit vertex coordinates instead of Trimesh's default 32-bit PLY cast. The exporter reloads and validates the temporary on-disk file before atomically replacing a cached mesh, preventing sub-micron seam repairs from collapsing during serialization.
- The supported pipeline uses region-neutral script and generated-data names; historical AMNH experiments remain evidence, not runtime dependencies.
- The first bridge-core implementation applied each ivory centerline immediately after its deck. A later overlapping OSM bridge way could therefore repaint the shared deck tan. The production field builder now queues 48 motor-road bridge cores and applies them after every deck, preserving all 14 Brooklyn Bridge segments (12,623 manufacturing-grid cells at 0.872 mm nominal width).

## City Hall end-to-end validation

The corrected 200 × 200 mm tile centered at `(-74.0060, 40.7127)` was regenerated with the production entry point. Semantic checks found 1,162 OSM building footprints, 1,041 current NYC footprints, 1,055 CityGML building objects with 4,298 roof polygons, 657 OSM motor-road segments and 525 Planimetrics roadbeds. The rendered preview contains the dense Lower Manhattan buildings, parks, canopy and multilevel approaches. A full-resolution bridge crop confirms that the Brooklyn Bridge and approach carriageways retain ivory cores over the tan structural deck.

The final archive contains four watertight, consistently wound material solids and 3,406,138 triangles. Every material stays inside `[0, 200] × [0, 200]` mm, all meshes have zero degenerate triangles, the boolean union has one positive assembly component, and every measured cross-material intersection is below the print-resolution tolerance. SHA-256: `abea0ef4c6ed88b201b02b344c275d85f3bb5a9ea078dd7e4417c028ae182897`.

Bambu Studio sliced this exact 3MF successfully at 0.24 mm, two walls and 5% infill: **29 h 19 m 41 s**, **324.96 g** total filament and **78** filament changes. Bambu still emits its non-critical “floating regions” warning, so the geometry/slicer pass does not replace a representative physical print. The City Hall skyline is taller and denser than the AMNH crop, which explains why it exceeds the AMNH sub-24-hour estimate.

Use [the generator guide](generate_3mf.md) for new regions. Every generated job preserves configuration, source subsets, intermediate fields, logs, validation reports and checksums under `output/jobs/`.
