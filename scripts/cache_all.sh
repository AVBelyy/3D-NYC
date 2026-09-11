#!/usr/bin/env bash
# Build every dataset cache under data/cache/ from the raw downloads.
#
# Wraps the per-dataset scripts/cache_X.py commands described in
# docs/cache_source_datasets.md. LiDAR is built first because the other
# builders read its catalog to define their coverage. Caches are resumable and
# reuse a current manifest, so a rerun costs little.
#
# This covers all of NYC, which is the extent of the LiDAR tile index rather
# than a coordinate list anyone has to know. That is a long one-time build, so
# each LAZ is streamed in and deleted after its last use, keeping resident
# source data under the builder's own cap.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

# Built after LiDAR, in the order of the runbook table.
DATASETS=(
  nyc_3d_buildings_2014
  nyc_planimetrics_2022
  nyc_parks_trails
  nyc_parks_structures
  mta_subway_entrances_2024
  nyc_land_cover_2017
  new_york_osm
  nyc_building_footprints
)

usage() {
  cat <<'USAGE'
Usage: scripts/cache_all.sh [options]

Build every dataset cache under data/cache/ from the downloads in data/raw/,
ready for scripts/generate_3mf.py. It covers all of NYC.

Options:
  --keep-lidar-sources
        Keep every downloaded LAZ under data/raw/ instead of deleting each one
        after its last use. Faster to rebuild from, but the whole city at once
        needs far more free space than the streamed default.
  --skip-lidar
        Keep the existing LiDAR cache and build only the rest. The other
        builders still need its catalog, so it has to exist already.
  --lidar-dataset NAME
        Which LiDAR collection to build and take citywide coverage from:
        nyc_lidar_2021 (default, published rasters) or nyc_lidar_2017
        (point cloud, and the only one with bathymetry).
  -h, --help
        Show this help.

Environment:
  PYTHON  Interpreter used for the dataset scripts (default .venv/bin/python).

Anything narrower or more tuned than this belongs to the individual builders:
caching a smaller area than the city, and the LiDAR worker count and
resident-source cap. Run those pairs directly as
docs/cache_source_datasets.md describes.
USAGE
}

die() {
  printf 'cache_all.sh: %s\n' "$1" >&2
  exit 1
}

run() {
  printf '\n==> %s\n' "$*" >&2
  "$@"
}

skip_lidar=0
lidar_dataset=nyc_lidar_2021
keep_sources=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-lidar-sources)
      keep_sources=1
      shift
      ;;
    --lidar-dataset)
      [[ $# -ge 2 ]] || die '--lidar-dataset needs a value'
      case $2 in
        nyc_lidar_2021|nyc_lidar_2017) lidar_dataset=$2 ;;
        *) die "unknown --lidar-dataset $2" ;;
      esac
      shift 2
      continue
      ;;
    --skip-lidar)
      skip_lidar=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

[[ -x "$PYTHON" ]] ||
  die "no interpreter at $PYTHON; create .venv as README.md describes or set PYTHON"

cd "$ROOT"

if [[ $skip_lidar -eq 1 ]]; then
  printf '\n==> skipping %s\n' "$lidar_dataset" >&2
  [[ -f "data/cache/$lidar_dataset/catalog.geojson" ]] ||
    die "data/cache/$lidar_dataset/catalog.geojson is missing; the other builders read it for coverage"
elif [[ $lidar_dataset == nyc_lidar_2021 ]]; then
  # The published rasters cover each borough whole, so there is no per-area
  # selection and nothing to stream: the builder reads whatever pairs are on
  # disk and --keep-lidar-sources has nothing to act on.
  run "$PYTHON" scripts/cache_nyc_lidar_2021.py
else
  # --coverage all without --bounds is every tile in the index, which is what
  # "all of NYC" means here. --download-missing is not optional in practice:
  # fixed raster chunks and the ground-fill halo reach past the request for
  # neighbouring source tiles.
  lidar=(scripts/cache_nyc_lidar_2017.py --coverage all --download-missing)
  # Streaming deletion caps resident LAZ, and the builder requires one worker
  # with it, which is its own default anyway.
  if [[ $keep_sources -eq 0 ]]; then
    lidar+=(--delete-source-after-last-use --workers 1)
  fi
  run "$PYTHON" "${lidar[@]}"
fi

# The vector builders take their citywide extent from the LiDAR catalog, so they
# have to read the one just built rather than the default.
coverage=("--coverage" "data/cache/$lidar_dataset/catalog.geojson")

for dataset in "${DATASETS[@]}"; do
  # Only the vector builders derive their extent from the LiDAR catalog; the
  # land-cover raster is clipped by its own source and takes no --coverage.
  if "$PYTHON" "scripts/cache_$dataset.py" --help 2>/dev/null | grep -q -- '--coverage'; then
    run "$PYTHON" "scripts/cache_$dataset.py" "${coverage[@]}"
  else
    run "$PYTHON" "scripts/cache_$dataset.py"
  fi
done

printf '\nBuilt caches under data/cache/. Next: scripts/generate_3mf.py\n' >&2
