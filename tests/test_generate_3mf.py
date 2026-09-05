import sys
import unittest
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Polygon, box


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from generate_3mf import count_osm_semantic_features  # noqa: E402


class SemanticCoverageTests(unittest.TestCase):
    def test_osm_reference_counts_use_rotated_source_polygon_not_its_bbox(self):
        diamond = Polygon([(0, 5), (5, 10), (10, 5), (5, 0)])
        geometries = (
            [box(4, 4, 5, 5), box(5, 5, 6, 6)]
            + [box(0.1, 0.1, 0.4, 0.4)] * 10
            + [LineString([(4, 5), (6, 5)]), LineString([(0.1, 0.2), (0.3, 0.2)])]
        )
        osm = gpd.GeoDataFrame(
            {
                "building": ["yes"] * 12 + [None, None],
                "highway": [None] * 12 + ["primary", "primary"],
            },
            geometry=geometries,
            crs=2263,
        )

        counts = count_osm_semantic_features(osm, diamond)

        self.assertEqual(counts["osm_building_footprints"], 2)
        self.assertEqual(counts["osm_building_footprints_unfiltered"], 12)
        self.assertEqual(counts["osm_motor_road_segments"], 1)
        self.assertEqual(counts["osm_motor_road_segments_unfiltered"], 2)


if __name__ == "__main__":
    unittest.main()
