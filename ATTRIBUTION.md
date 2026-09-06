# Data sources and attribution

The MIT license in `LICENSE` covers this project's original software and
documentation. It does not relicense third-party source data. Downloaded data,
local caches, and outputs derived from them remain subject to their providers'
terms.

## OpenStreetMap

Contains information from [OpenStreetMap](https://www.openstreetmap.org/),
which is made available under the
[Open Database License (ODbL) 1.0](https://www.openstreetmap.org/copyright).
The New York extract is distributed by
[Geofabrik](https://download.geofabrik.de/north-america/us/new-york.html).

If you publicly share a generated 3MF, render, or other produced work, retain a
notice that makes viewers aware of the OpenStreetMap source and ODbL license.
Publishing a derived database may carry additional ODbL share-alike duties;
consult the license for your use case.

## City of New York

The following public datasets are provided by New York City agencies and are
subject to the [NYC Open Data terms](https://cityofnewyork.github.io/opendatatsm/publicpolicies.html)
and any dataset-specific terms:

- [NYC 2014 3D Building Model](https://www.nyc.gov/content/oti/pages/tools)
- [NYC Building Footprints](https://data.cityofnewyork.us/d/5zhs-2jue)
- [NYC 2022 Planimetric Database](https://www.arcgis.com/home/item.html?id=4b01b78d9eda44819f6c757ec00d0669)
- [NYC Parks Trails](https://data.cityofnewyork.us/d/vjbm-hsyr)
- [NYC Parks Structures](https://data.cityofnewyork.us/d/n8q6-i44s)
- [NYC 2017 Land Cover](https://data.cityofnewyork.us/d/he6d-2qns)
- [NYC 2017 LiDAR](https://maps.nyc.gov/lidar/2017/)
- [NYC Planning GeoSearch](https://geosearch.planninglabs.nyc/) (used only
  when a building-color override is selected by address)

NYC datasets are provided for informational purposes without warranties of
completeness, accuracy, or fitness for a particular use. The publishing agency
remains the authoritative source.

## State of New York / MTA

- [MTA Subway Entrances and Exits (2024)](https://data.ny.gov/d/i9wp-a4ja),
  subject to the [OPEN-NY terms of use](https://data.ny.gov/stories/s/Terms-of-Use/4un2-9t8b/).

Data-provider names and trademarks identify the sources only; their inclusion
does not imply endorsement of this project.

Several upstream datasets are updated in place. A cache manifest and a
generated job manifest describe the snapshot actually used; a dataset name or
year alone does not identify a byte-for-byte source version.
