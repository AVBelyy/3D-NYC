"""Canonical public source locations and matching raw-data paths."""

DATASETS = {
    "nyc_3d_buildings_2014": (
        "https://maps.nyc.gov/download/3dmodel/DA_WISE_GML.zip",
        "nyc_3d_buildings_2014/DA_WISE_GML.zip",
    ),
    "nyc_building_footprints": (
        "https://data.cityofnewyork.us/api/views/5zhs-2jue/rows.csv?accessType=DOWNLOAD",
        "nyc_building_footprints/buildings.csv",
    ),
    "nyc_planimetrics_2022": (
        "https://www.arcgis.com/sharing/rest/content/items/4b01b78d9eda44819f6c757ec00d0669/data",
        "nyc_planimetrics_2022/Planimetric_2022.gdb.zip",
    ),
    "nyc_parks_trails": (
        "https://data.cityofnewyork.us/api/views/vjbm-hsyr/rows.csv?accessType=DOWNLOAD",
        "nyc_parks_trails/parks_trails.csv",
    ),
    "nyc_land_cover_2017": (
        "https://data.cityofnewyork.us/download/he6d-2qns/application%2Fzip",
        "nyc_land_cover_2017/Land_Cover.zip",
    ),
    # The 2021 survey is not on the city's open-data portal. It was produced
    # for the city by TNC/UVM and published on Zenodo under CC BY-NC-SA 4.0,
    # which is a narrower licence than the 2017 raster it supersedes.
    "nyc_land_cover_2021": (
        "https://zenodo.org/api/records/14053441/files/landcover_nyc_2021_6in.tif/content",
        "nyc_land_cover_2021/landcover_nyc_2021_6in.tif",
    ),
    "new_york_osm": (
        "https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf",
        "new_york_osm/new-york-latest.osm.pbf",
    ),
    "nyc_parks_structures": (
        "https://data.cityofnewyork.us/resource/n8q6-i44s.geojson?$limit=50000",
        "nyc_parks_structures/structures.geojson",
    ),
    "mta_subway_entrances_2024": (
        "https://data.ny.gov/resource/i9wp-a4ja.csv?$limit=50000",
        "mta_subway_entrances_2024/subway_entrances.csv",
    ),
}
