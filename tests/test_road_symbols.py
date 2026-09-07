import sys
import unittest
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, box


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from road_symbols import (  # noqa: E402
    TRAIL_HIGHWAYS,
    trail_width_mm,
    build_road_symbols,
    classify_highway,
    printable_road_width,
    sample_roadbed_widths_m,
)


CFG = {"grid_step_mm": 0.125, "nozzle_mm": 0.4, "road_minimum_bead_ratio": 0.85}


class RoadClassificationTests(unittest.TestCase):
    def test_trail_width_uses_its_own_printable_width(self):
        config={"minimum_path_width_mm":0.5,"scale_denominator":5576.988668344}
        self.assertEqual(trail_width_mm("footway",{},config),0.5)
        self.assertAlmostEqual(trail_width_mm("footway",{"width":"6"},config),
            6000/config["scale_denominator"])

    def test_all_hard_trail_classes_are_tan_only(self):
        for highway in TRAIL_HIGHWAYS:
            with self.subTest(highway=highway):
                classification, _ = classify_highway(
                    highway,
                    {"surface": "asphalt", "name": "Named trail", "lanes": "2", "maxspeed": "20"},
                    1.0,
                )
                self.assertEqual(classification, "trail")

    def test_car_free_road_form_can_still_be_a_carriageway(self):
        classification, _ = classify_highway("pedestrian", {
            "name": "Park Drive", "surface": "asphalt", "lanes": "1",
            "maxspeed": "20 mph", "oneway": "yes", "motor_vehicle": "no",
        }, 0.9)
        self.assertEqual(classification, "carriageway")

    def test_pedestrian_walk_and_plaza_remain_tan(self):
        for tags in [
            {"name": "Garden Walk", "surface": "asphalt"},
            {"name": "Civic Plaza", "surface": "paving_stones", "area": "yes"},
        ]:
            with self.subTest(tags=tags):
                self.assertEqual(classify_highway("pedestrian", tags, 1.0)[0], "trail")

    def test_minor_service_access_is_not_promoted(self):
        for service in ["parking_aisle", "driveway", "alley", "emergency_access"]:
            with self.subTest(service=service):
                result = classify_highway("service", {
                    "service": service, "surface": "asphalt", "lanes": "1", "name": "Access"
                }, 1.0)
                self.assertEqual(result[0], "other")

    def test_print_widths_are_grid_aligned_and_printable(self):
        for major in [False, True]:
            width = printable_road_width(CFG, major).surface_mm
            self.assertAlmostEqual(width / 0.125, round(width / 0.125))
            self.assertGreaterEqual(width, 0.45)

    def test_road_ribbon_does_not_expand_to_physical_roadbed_width(self):
        small_scale = printable_road_width(CFG, physical_width_m=9.0, scale_denominator=50_000)
        large_scale = printable_road_width(CFG, physical_width_m=9.0, scale_denominator=3_144)
        self.assertEqual(small_scale, printable_road_width(CFG))
        self.assertEqual(large_scale.surface_mm, small_scale.surface_mm)

    def test_cross_section_width_estimator_rejects_intersection_flare(self):
        # Source coordinates are EPSG:2263 US survey feet. A 10 m street has a
        # short 24 m-wide intersection flare in its middle.
        feet_per_m = 1 / 0.3048006096012192
        line = LineString([(0, 0), (120 * feet_per_m, 0)])
        street = box(0, -5 * feet_per_m, 120 * feet_per_m, 5 * feet_per_m)
        flare = box(55 * feet_per_m, -12 * feet_per_m, 65 * feet_per_m, 12 * feet_per_m)
        samples = sample_roadbed_widths_m(line, street.union(flare), interval_m=10)
        self.assertGreaterEqual(len(samples), 8)
        self.assertAlmostEqual(float(np.percentile(samples, 30)), 10.0, delta=0.2)


class RoadGeometryTests(unittest.TestCase):
    def setUp(self):
        self.aoi = box(0, 0, 100, 100)
        self.scale = 10_000

    def frame(self, rows):
        if not rows:
            return gpd.GeoDataFrame({"geometry": gpd.GeoSeries([], crs=2263)}, geometry="geometry", crs=2263)
        return gpd.GeoDataFrame(rows, geometry="geometry", crs=2263)

    def test_surface_is_continuous_across_split_road_and_never_follows_trail(self):
        osm = self.frame([
            {"osm_id": 1, "highway": "residential", "tags": '{"highway":"residential","name":"Main"}',
             "geometry": LineString([(10, 50), (50, 50)])},
            {"osm_id": 2, "highway": "residential", "tags": '{"highway":"residential","name":"Main"}',
             "geometry": LineString([(50, 50), (90, 50)])},
            {"osm_id": 3, "highway": "footway", "tags": '{"highway":"footway","name":"Woodland Ramble"}',
             "geometry": LineString([(50, 10), (50, 90)])},
        ])
        roadbed = self.frame([{"geometry": box(8, 46, 92, 54), "SUB_FEATURE_CODE": 350000}])
        routes, roads, report = build_road_symbols(osm, roadbed, self.aoi, self.scale, CFG)
        self.assertEqual(report["categorical_trails_eligible"], 0)
        self.assertFalse(bool(routes.loc[routes.osm_id.eq(3), "ivory_eligible"].iloc[0]))
        road_union = roads.geometry.union_all()
        self.assertTrue(road_union.covers(LineString([(10, 50), (90, 50)])))
        # The trail crosses the road surface once, but no ivory branch extends
        # north or south along it.
        self.assertFalse(road_union.intersects(LineString([(50, 70), (50, 90)])))

    def test_centerline_fallback_works_without_roadbed(self):
        osm = self.frame([{
            "osm_id": 8, "highway": "primary", "tags": '{"highway":"primary","name":"Broadway"}',
            "geometry": LineString([(10, 20), (90, 20)]),
        }])
        roadbed = self.frame([])
        routes, roads, _ = build_road_symbols(osm, roadbed, self.aoi, self.scale, CFG)
        self.assertTrue(bool(routes.ivory_eligible.iloc[0]))
        self.assertGreater(roads.area.sum(), 0)
        self.assertTrue(roads.geometry.union_all().covers(osm.geometry.iloc[0]))

    def test_bridge_and_tunnel_do_not_leak_into_surface_polygons(self):
        osm = self.frame([
            {"osm_id": 11, "highway": "primary", "tags": '{"highway":"primary","bridge":"yes","layer":"1"}',
             "geometry": LineString([(10, 30), (90, 30)])},
            {"osm_id": 12, "highway": "secondary", "tags": '{"highway":"secondary","tunnel":"yes","layer":"-1"}',
             "geometry": LineString([(10, 70), (90, 70)])},
        ])
        roadbed = self.frame([])
        routes, roads, _ = build_road_symbols(osm, roadbed, self.aoi, self.scale, CFG)
        self.assertTrue(routes.ivory_eligible.all())
        self.assertTrue(routes.bridge.any())
        self.assertTrue(routes.tunnel.any())
        self.assertEqual(len(roads), 0)


if __name__ == "__main__":
    unittest.main()
