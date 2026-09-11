# Source dataset cache runbook

The generator consumes one directory per source dataset under `data/cache/`.
The same dataset token `X` is used by its two maintenance scripts and paths:

```text
scripts/download_X.py -> data/raw/X/
scripts/cache_X.py    -> data/cache/X/
```

`data/raw/` is only a staging area. After `cache_X.py` finishes and publishes a
complete `data/cache/X/manifest.json`, its raw directory is no longer needed by
normal cached generation. Both raw and cached data are Git-ignored. They can be
rebuilt by the scripts below, although live sources can change between builds.

| X | Download | Build cache |
| --- | --- | --- |
| `nyc_3d_buildings_2014` | `download_nyc_3d_buildings_2014.py` | `cache_nyc_3d_buildings_2014.py` |
| `nyc_building_footprints` | `download_nyc_building_footprints.py` | `cache_nyc_building_footprints.py` |
| `nyc_planimetrics_2022` | `download_nyc_planimetrics_2022.py` | `cache_nyc_planimetrics_2022.py` |
| `nyc_parks_trails` | `download_nyc_parks_trails.py` | `cache_nyc_parks_trails.py` |
| `nyc_parks_structures` | `download_nyc_parks_structures.py` | `cache_nyc_parks_structures.py` |
| `mta_subway_entrances_2024` | `download_mta_subway_entrances_2024.py` | `cache_mta_subway_entrances_2024.py` |
| `nyc_land_cover_2021` | `download_nyc_land_cover_2021.py` | `cache_nyc_land_cover_2021.py` |
| `nyc_land_cover_2017` | `download_nyc_land_cover_2017.py` | `cache_nyc_land_cover_2017.py` |
| `new_york_osm` | `download_new_york_osm.py` | `cache_new_york_osm.py` |
| `nyc_lidar_2021` | `download_nyc_lidar_2021.py` | `cache_nyc_lidar_2021.py` |
| `nyc_lidar_2017` | `download_nyc_lidar_2017.py` | `cache_nyc_lidar_2017.py` |

`scripts/download_all.sh` and `scripts/cache_all.sh` run that whole table from
the repository root, and neither one asks for an area:

```bash
scripts/download_all.sh
scripts/cache_all.sh
```

`download_all.sh` fetches the citywide sources plus the LiDAR and land-cover
collections it was asked for, and nothing else. The default `nyc_lidar_2021`
surfaces are published per borough, so there is no area to select and nothing
to stream: about 39 GB in ten files. With `--lidar-dataset nyc_lidar_2017` it
takes only that collection's tile index instead, because its LAZ are selected
by area and pulled by its cache builder on demand -- so a 2017 build never
downloads the 2021 rasters. All of NYC is then simply the extent of the
catalog, not a bounding box anyone has to look up.

`cache_all.sh` builds LiDAR before anything else, because the remaining builders
read its catalog for their coverage, and `--skip-lidar` leaves an existing LiDAR
cache alone and rebuilds only the rest. The streaming controls belong to the
2017 path alone: a citywide LAZ build reads far more source than it is worth
keeping, so with `--lidar-dataset nyc_lidar_2017` the builder streams each tile
in and deletes it after its last use, and `--keep-lidar-sources` retains them
instead. The 2021 builder reads the borough rasters already on disk, so neither
option has anything to act on.

Neither script downloads the building-footprints CSV, because the footprint
cache is built from the official feature service and never reads it.

Those are the only options. Both scripts run `.venv/bin/python` unless `PYTHON`
names another interpreter, and print each command before running it. Anything
else is a job for the individual pairs below: caching a smaller area than the
whole city, and the LiDAR controls.

Run a pair from the repository root, for example:

```bash
.venv/bin/python scripts/download_new_york_osm.py
.venv/bin/python scripts/cache_new_york_osm.py
```

The vector cache builders take citywide coverage from a LiDAR catalog, so build
the LiDAR cache first. `nyc_lidar_2021` is the default; `cache_all.sh` passes the
catalog it built to every builder that accepts `--coverage`. Planimetrics automatically extracts
its downloaded FileGDB archive. The building-footprint cache is the exception
to the normal pair: it is always built by streaming the official feature
service, and never reads `data/raw/nyc_building_footprints`. Its download
script remains because `generate_3mf.py` falls back to that CSV during an
online run when no footprint cache exists and the service call fails; nothing
in the cache path uses it.

Two LiDAR collections publish the same cache contract, and the generator and the
chunk planner read whichever one they are pointed at. They are alternatives, not
additions:

* `nyc_lidar_2021` (**default**) converts the survey's published per-borough
  DTM/DSM rasters, about 39 GB fetched whole. See the
  [2021 LiDAR cache guide](cache_nyc_lidar_2021.md).
* `nyc_lidar_2017` bins the point cloud, is region-selective and streamed, and is
  the only one carrying bathymetry. See the
  [2017 LiDAR cache guide](cache_nyc_lidar_2017.md).

Point the generator at one with `--lidar-cache-dir`, and the planner,
`download_all.sh`, and `cache_all.sh` with `--lidar-dataset`. Pass the same
name to the download and cache scripts so the pair agrees.

Land cover is the other dataset published as two collections, and they are
likewise alternatives rather than additions. Both carry the same eight-class
six-inch legend in EPSG:2263, so one cache layout serves either and nothing
downstream changes when you switch:

* `nyc_land_cover_2021` (**default**) is the current survey, a 1.6 GB GeoTIFF.
  It is not on the city's open-data portal: it was produced for the city by
  TNC/UVM and published on Zenodo under CC BY-NC-SA 4.0, a narrower licence
  than the raster it supersedes.
* `nyc_land_cover_2017` is the previous city-published survey, distributed as
  an ERDAS IMG inside a ZIP whose companion file is about 98 GB. The builder
  streams it through `/vsizip` rather than extracting it.

Select one with `--land-cover-dataset`, which the generator, the planner,
`download_all.sh`, and `cache_all.sh` all accept. Only classes 1 and 2 are
read, so a plate built from either survey differs only where the vegetation
itself changed between 2017 and 2021.

Non-LiDAR cache manifests record source identity, configuration, output
inventory, and whether the cache is production-ready. Bounded non-LiDAR caches
are deliberately not production-ready and are rejected by the generator. The
LiDAR cache uses a separate manifest/catalog contract and may cover only the
area being generated. `generate_3mf.py` validates the relevant contract before
use.

To store caches elsewhere, pass the same `--cache-root` to every non-LiDAR
cache builder and `--cache-dir` to the generator. Pass `--output-dir` to the
LiDAR cache builder and the resulting dataset directory as
`--lidar-cache-dir` to the generator. These options name different levels:
`--cache-root`/`--cache-dir` is the parent of dataset directories, while the
LiDAR options point at `nyc_lidar_2021` (or `nyc_lidar_2017`) itself.

For a cache-only offline generation run, cache the eight non-LiDAR datasets
in the table (one land-cover collection, not both) plus one LiDAR collection.

To choose seams, `scripts/plan_map_chunks.py` reads complete, production-ready
caches for LiDAR, Planimetrics, building footprints, and OpenStreetMap, and uses
land cover when it is present. It does not read parks, 3D buildings, or MTA
entrances. The generation commands it emits are ordinary `generate_3mf.py` runs,
so those still need whatever the generator itself requires.

`data/cache/nyc_geosearch` is different: the generator fills it lazily with
NYC Planning GeoSearch responses only for address-selected building colors, so
it has no download/cache script pair. An offline address selection succeeds
only if its exact normalized-address response is already cached; coordinate or
identifier selections do not use GeoSearch.
