#!/usr/bin/env bash
# Download every raw source dataset into data/raw/.
#
# Wraps the per-dataset scripts/download_X.py commands described in
# docs/cache_source_datasets.md, then scripts/cache_all.sh turns the downloads
# into the caches the generator reads. Downloads are resumable and record a
# manifest, so a rerun skips whatever already arrived complete.
#
# There is no area to choose. Every source here is citywide, including the
# default nyc_lidar_2021 surfaces, which are published per borough and fetched
# whole. --lidar-dataset and --land-cover-dataset pick which collection of each
# to fetch; pass the same names to cache_all.sh so the pair agrees.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

# Datasets downloaded by a plain scripts/download_X.py call, in the order of the
# runbook table. Building footprints is deliberately absent: its cache is built
# by streaming the official feature service and never reads a download.
# LAND_COVER is a placeholder that --land-cover-dataset substitutes below, so
# only the survey being fetched changes and the order stays the runbook's.
DATASETS=(
  nyc_3d_buildings_2014
  nyc_planimetrics_2022
  nyc_parks_trails
  nyc_parks_structures
  mta_subway_entrances_2024
  LAND_COVER
  new_york_osm
)

usage() {
  cat <<'USAGE'
Usage: scripts/download_all.sh [options]

Download every raw source dataset into data/raw/, then run
scripts/cache_all.sh to build the caches scripts/generate_3mf.py reads.

Options:
  --lidar-dataset NAME
        Which LiDAR collection to fetch: nyc_lidar_2021 (default, about 39 GB
        of published per-borough rasters) or nyc_lidar_2017, for which only
        the tile index is fetched here because its LAZ are pulled by area as
        cache_all.sh builds. Pass the same name to scripts/cache_all.sh.
  --land-cover-dataset NAME
        Which land-cover survey to fetch: nyc_land_cover_2021 (default, a
        1.6 GB GeoTIFF) or nyc_land_cover_2017 (a much larger archive). Pass
        the same name to scripts/cache_all.sh.
  -h, --help
        Show this help.

Environment:
  PYTHON  Interpreter used for the dataset scripts (default .venv/bin/python).

Only the selected collections are fetched, so choosing nyc_lidar_2017 skips
the 39 GB of 2021 rasters entirely. The building-footprints CSV is left out
either way, because that cache is built by streaming the official feature
service. Both are covered by docs/cache_source_datasets.md.
USAGE
}

die() {
  printf 'download_all.sh: %s\n' "$1" >&2
  exit 1
}

run() {
  printf '\n==> %s\n' "$*" >&2
  "$@"
}

lidar_dataset=nyc_lidar_2021
land_cover_dataset=nyc_land_cover_2021

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lidar-dataset)
      [[ $# -ge 2 ]] || die '--lidar-dataset needs a value'
      case $2 in
        nyc_lidar_2021|nyc_lidar_2017) lidar_dataset=$2 ;;
        *) die "unknown --lidar-dataset $2" ;;
      esac
      shift 2
      continue
      ;;
    --land-cover-dataset)
      [[ $# -ge 2 ]] || die '--land-cover-dataset needs a value'
      case $2 in
        nyc_land_cover_2021|nyc_land_cover_2017) land_cover_dataset=$2 ;;
        *) die "unknown --land-cover-dataset $2" ;;
      esac
      shift 2
      continue
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

DATASETS=("${DATASETS[@]/LAND_COVER/$land_cover_dataset}")

for dataset in "${DATASETS[@]}"; do
  run "$PYTHON" "scripts/download_$dataset.py"
done

# The 2021 rasters are published per borough and fetched whole: about 39 GB,
# ten files, no per-area selection to make. The 2017 collection contributes
# only its tile index, because its LAZ are selected by area and pulled on
# demand by cache_all.sh. Fetching just the selected one keeps a 2017 build
# from dragging in 39 GB it will never cache.
if [[ $lidar_dataset == nyc_lidar_2021 ]]; then
  run "$PYTHON" scripts/download_nyc_lidar_2021.py
else
  run "$PYTHON" scripts/download_nyc_lidar_2017.py --index-only
fi

printf '\nDownloaded raw sources under data/raw/. Next: scripts/cache_all.sh\n' >&2
