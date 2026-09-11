# NYC 2021 LiDAR cache

The 2021 survey covers the whole city and is the generator's default LiDAR
source. NYS publishes it two ways: 1740 uncompressed LAS tiles totalling about
**700 GB**, and per-borough gridded surfaces totalling about **39 GB**. The
generator reads exactly two measurements from LiDAR and both are among the
published surfaces, so this pair converts the rasters and never touches the point
cloud.

```text
scripts/download_nyc_lidar_2021.py -> data/raw/nyc_lidar_2021/
scripts/cache_nyc_lidar_2021.py    -> data/cache/nyc_lidar_2021/
```

```bash
.venv/bin/python scripts/download_nyc_lidar_2021.py
.venv/bin/python scripts/cache_nyc_lidar_2021.py
```

Both default to all five boroughs. `--borough NAME` restricts either and is
repeatable; `--surface ground|upper` restricts the download. `--list-only` prints
the URLs the download would fetch without fetching them. The cache builder also
takes `--bounds XMIN YMIN XMAX YMAX` (EPSG:2263 US survey feet) and `--dry-run`,
and is resumable: a rerun reuses any tile whose recorded configuration still
matches.

The download directories are not listed on <https://gis.ny.gov/lidar> and return
403 to an HTTP listing, but the same paths are readable over FTP, so the
filenames are **discovered** rather than written down. They would not survive
being written down: the DTMs are named for the county and the DSMs for the
borough, with Queens abbreviated and Manhattan's DTM filed under New York. What
the code declares is the county/borough synonymy needed to pair them.

## What the conversion does

The published rasters and the cache contract differ in four ways, all reconciled
here. Every source property is read off the rasters rather than assumed, so a
republication at a different cell size or projection converts without an edit.

* **Horizontal.** The published projection is reprojected to the cache's
  EPSG:2263. For EPSG:6539 (NAD83(2011)) that is a null transform in PROJ, so it
  introduces no shift.
* **Vertical.** US survey feet to metres, via the project's exact `1200/3937`.
* **Resolution.** Source cells are resampled to the cache's 0.5 m. The DTM is a
  continuous surface and is interpolated; the DSM steps at roof and canopy edges
  and is sampled without blending, so interpolation cannot invent a ramp up a
  wall for the mesh to follow.
* **Ordering.** The published DSM is gridded independently of the DTM and dips
  below it on a small fraction of cells, nearly all by single-digit millimetres.
  The 2017 upper surface includes class 2 and so is never below ground; the same
  invariant is restored by raising the upper surface to the ground where the DSM
  falls below it.

Borough rasters are read in a fixed order and the first valid value wins, so a
seam where two overlap resolves identically on every rebuild. The tile grid, the
0.5 m resolution and the 10 m nearest-ground fill are imported from
`cache_nyc_lidar_2017.py`, so the two builders cannot drift apart.

## How it differs from the 2017 cache

This is a functional replacement for the cache contract, not the same
measurement, and the manifest says so: it records `source_kind: published
rasters` and names both published products rather than claiming the per-class
point aggregation it did not perform. `generate_3mf.ensure_lidar_cache()`
validates the shared grid contract for both collections and then the derivation
belonging to the declared `source_kind`, so a raster cache cannot pass itself off
as point-derived.

| | 2017 | 2021 |
| --- | --- | --- |
| Source | classified point cloud | published DTM/DSM rasters |
| Download | ~110 GB LAZ, selected by area | ~39 GB, ten files |
| Ground | mean class-2 elevation | published bare-earth DTM |
| Upper surface | max class 1/2/17/25 | published DSM, raised to ground |
| Bathymetry | yes, topobathymetric | **no, topographic only** |

The 2017 collection remains the only one with bathymetry. Water is built from
mapped hydrography rather than LiDAR, so this does not remove a printed feature,
but it is the reason the older cache is still selectable.

## Choosing a collection

| Consumer | Option |
| --- | --- |
| `generate_3mf.py` | `--lidar-cache-dir data/cache/nyc_lidar_2017` |
| `plan_map_chunks.py` | `--lidar-dataset nyc_lidar_2017` |
| `download_all.sh` | `--lidar-dataset nyc_lidar_2017` |
| `cache_all.sh` | `--lidar-dataset nyc_lidar_2017` |

The plan records which collection its cut-cost surface measured height from, in
`plan.json` under `sources.datasets`.

## What the rasters cannot do

The published surfaces keep no per-point information, so anything needing
classification, return number, intensity or density has to go back to the LAS
tiles under <https://gisdata.ny.gov/elevation/LIDAR/NYC_2021/>. No current
generation or planning stage needs them: roof geometry comes from the 2014
CityGML model and building footprints, not from LiDAR.
