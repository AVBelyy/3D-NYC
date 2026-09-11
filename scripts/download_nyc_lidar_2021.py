#!/usr/bin/env python3
"""Download the published NYC 2021 DTM and DSM rasters, by borough.

The 2021 survey is distributed two ways: 1740 uncompressed LAS tiles totalling
about 700 GB, and per-borough gridded surfaces totalling about 39 GB. The
generator reads exactly two measurements from LiDAR, bare-earth ground and an
upper surface, and both are among the published surfaces, so this fetches the
rasters and leaves the point cloud alone.

The two directories are not listed on https://gis.ny.gov/lidar and return 403 to
an HTTP listing, but the same paths are readable over FTP, so the filenames are
discovered rather than written down. They would not survive being written down:
the DTMs are named for the county and the DSMs for the borough, with Queens
abbreviated and Manhattan's DTM filed under New York. What is declared here is
the county/borough synonymy needed to pair them, which is the durable fact.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from download_data import ROOT, download

FTP_ROOT = "ftp://ftp.gis.ny.gov/elevation"
HTTP_ROOT = "https://gisdata.ny.gov/elevation"
GROUND_DIRECTORY = "DEM/NYC_2021"
UPPER_DIRECTORY = "DEM/NYC_2021_DSM"
DEFAULT_SOURCE = ROOT / "data/raw/nyc_lidar_2021"

# Every name a borough is published under across the two directories. NYS files
# the bare-earth rasters by county and the surface rasters by borough, so the
# pairing is this synonymy and nothing else.
BOROUGH_ALIASES = {
    "Bronx": ("bronx",),
    "Brooklyn": ("brooklyn", "kings"),
    "Manhattan": ("manhattan", "newyork", "new_york"),
    "Queens": ("queens", "qn"),
    "Staten Island": ("staten", "richmond"),
}
SURFACE_DIRECTORIES = {"ground": GROUND_DIRECTORY, "upper": UPPER_DIRECTORY}


def list_directory(directory: str) -> list[str]:
    """Names published in one NYS elevation directory, over FTP."""
    import subprocess

    result = subprocess.run(
        ["curl", "--silent", "--max-time", "120", "--list-only",
         f"{FTP_ROOT}/{directory}/"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise SystemExit(
            f"Could not list {FTP_ROOT}/{directory}/ (curl exit {result.returncode}). "
            "The published rasters are only discoverable over FTP."
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def borough_for(name: str) -> str | None:
    """Match one published filename to the borough it covers."""
    token = re.sub(r"[^a-z]", "", name.lower())
    for borough, aliases in BOROUGH_ALIASES.items():
        if any(re.sub(r"[^a-z]", "", alias) in token for alias in aliases):
            return borough
    return None


def discover(surfaces: list[str]) -> dict[str, dict[str, str]]:
    """Map borough -> surface -> published filename, refusing an ambiguous pairing."""
    found: dict[str, dict[str, str]] = {}
    for surface in surfaces:
        directory = SURFACE_DIRECTORIES[surface]
        rasters = [name for name in list_directory(directory) if name.lower().endswith(".tif")]
        seen: dict[str, str] = {}
        for name in rasters:
            borough = borough_for(name)
            if borough is None:
                continue
            if borough in seen:
                raise SystemExit(
                    f"{directory} publishes two rasters for {borough}: "
                    f"{seen[borough]} and {name}. Pair them by hand."
                )
            seen[borough] = name
        missing = sorted(set(BOROUGH_ALIASES) - set(seen))
        if missing:
            raise SystemExit(
                f"{directory} is missing a raster for: {', '.join(missing)}. "
                f"It published {', '.join(rasters) or 'nothing'}."
            )
        for borough, name in seen.items():
            found.setdefault(borough, {})[surface] = name
    return found


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--borough", action="append", dest="boroughs", choices=sorted(BOROUGH_ALIASES),
        help="Restrict to one borough; repeatable (default: all five)",
    )
    parser.add_argument(
        "--surface", choices=sorted(SURFACE_DIRECTORIES), action="append", dest="surfaces",
        help="Restrict to one surface; repeatable (default: both, which the cache needs)",
    )
    parser.add_argument("--tile-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--list-only", action="store_true",
        help="Print the published filenames this would fetch and stop",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    boroughs = sorted(args.boroughs or BOROUGH_ALIASES)
    surfaces = sorted(args.surfaces or SURFACE_DIRECTORIES)
    published = discover(surfaces)
    directory = args.tile_dir.resolve()

    if args.list_only:
        for borough in boroughs:
            for surface in surfaces:
                print(f"{borough:<14} {surface:<7} "
                      f"{HTTP_ROOT}/{SURFACE_DIRECTORIES[surface]}/{published[borough][surface]}")
        return

    for number, borough in enumerate(boroughs, 1):
        print(f"Borough {number}/{len(boroughs)}: {borough}", flush=True)
        for surface in surfaces:
            name = published[borough][surface]
            download(f"{HTTP_ROOT}/{SURFACE_DIRECTORIES[surface]}/{name}",
                     f"{directory.name}/{name}", raw_dir=directory.parent)
    print(f"Published rasters ready: {directory}")


if __name__ == "__main__":
    main()
