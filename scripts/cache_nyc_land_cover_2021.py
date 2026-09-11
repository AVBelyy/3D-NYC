#!/usr/bin/env python3
"""Convert the NYC 2021 land-cover GeoTIFF into a resumable native-resolution GeoTIFF."""

from _cache_land_cover import main_for

if __name__ == "__main__":
    main_for("nyc_land_cover_2021")
