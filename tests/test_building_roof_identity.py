"""A 2014 roof surface belongs to one building, and only while it describes it.

Applied by position, a demolished building's roofs decide the height of
whatever replaced it: One Vanderbilt, 1401 ft on a footprint 96% covered by the
roofs of the four buildings it replaced, printed as the 150-378 ft block it
stands on plus the few slivers no old roof reached.  Rejected by identity
alone, a complex the footprint layer merely renumbered loses its roof instead:
the American Museum of Natural History was surveyed under eight BINs and is two
polygons carrying two of them today, and dropping the other six flattens
242,000 sq ft of museum into one slab at its tallest point.  The measurements
below are both of those sites'.
"""

import atexit
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


# ``build_map_fields`` imports ``map_common``, which resolves and creates work
# directories at import time.  Point every one of them at a throwaway directory
# so these tests never read or write a developer's real caches.
_SANDBOX = tempfile.TemporaryDirectory()
atexit.register(_SANDBOX.cleanup)
for _name in ["NYC_DATA_DIR", "NYC_RAW_DIR", "NYC_CACHE_DIR", "NYC_OUTPUT_DIR",
              "MAP_WORK_DIR", "NYC_VALID_DIR", "NYC_PROCESSED_DIR", "NYC_ANALYSIS_DIR"]:
    os.environ[_name] = str(Path(_SANDBOX.name) / _name.lower())

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import geopandas as gpd  # noqa: E402
from shapely.geometry import box  # noqa: E402

from build_map_fields import (  # noqa: E402
    building_identity_keys,
    building_part_massing,
    keep_higher_surface,
    osm_height_m,
    own_roof_cells,
    roof_owner_index,
    survey_describes_building,
)


def frame(records):
    """A building table shaped like the two sources this join has to bridge."""
    return gpd.GeoDataFrame(
        [{key: value for key, value in record.items() if key != "geometry"} for record in records],
        geometry=[record["geometry"] for record in records],
        crs=2263,
    )


class BuildingIdentityKeyTests(unittest.TestCase):
    def test_bin_is_preferred_and_the_footprint_id_is_the_fallback(self):
        keys = building_identity_keys(frame([
            {"bin": "1090825", "doitt_id": "1284912", "geometry": box(0, 0, 1, 1)},
        ]))
        self.assertEqual(keys, [("1090825", "1284912")])

    def test_the_borough_placeholder_bin_identifies_nothing(self):
        """``<borough>000000`` means no BIN assigned, so it must not join.

        Every unassigned record in a borough shares that value; treating it as
        an identity would join them all to each other.
        """
        keys = building_identity_keys(frame([
            {"bin": "1000000", "doitt_id": "764709", "geometry": box(0, 0, 1, 1)},
            {"bin": "3000000", "doitt_id": "12", "geometry": box(0, 0, 1, 1)},
            {"bin": "0", "doitt_id": "13", "geometry": box(0, 0, 1, 1)},
        ]))
        self.assertEqual(keys, [("", "764709"), ("", "12"), ("", "13")])

    def test_ids_read_back_as_floats_still_match_ids_read_back_as_text(self):
        """One source round-trips these ids through a float column."""
        keys = building_identity_keys(frame([
            {"bin": "1090825.0", "doitt_id": "1284912.0", "geometry": box(0, 0, 1, 1)},
        ]))
        self.assertEqual(keys, [("1090825", "1284912")])

    def test_missing_or_unusable_ids_yield_no_key(self):
        self.assertEqual(
            building_identity_keys(frame([
                {"bin": None, "doitt_id": "", "geometry": box(0, 0, 1, 1)},
                {"bin": "n/a", "doitt_id": "0", "geometry": box(0, 0, 1, 1)},
            ])),
            [("", ""), ("", "")],
        )
        self.assertEqual(
            building_identity_keys(frame([{"geometry": box(0, 0, 1, 1)}])),
            [("", "")],
        )


class RoofOwnerTests(unittest.TestCase):
    def setUp(self):
        # One Vanderbilt's block: the tower footprint carries a BIN issued after
        # the four buildings it replaced were demolished.
        self.tower = frame([
            {"bin": "1090825", "doitt_id": "1284912", "geometry": box(0, 0, 100, 100)},
        ])
        self.replaced = frame([
            {"bin": "1035349", "doitt_id": "247514", "geometry": box(0, 0, 50, 50)},
            {"bin": "1035350", "doitt_id": "557929", "geometry": box(50, 0, 100, 50)},
            {"bin": "1035351", "doitt_id": "333501", "geometry": box(0, 50, 50, 100)},
            {"bin": "1035352", "doitt_id": "22949", "geometry": box(50, 50, 100, 100)},
        ])

    def test_a_retired_identity_falls_back_to_the_footprint_underneath(self):
        """A retired BIN is not evidence the building went with it.

        Whether these surfaces may set the tower's height is decided by
        ``survey_describes_building``, not by throwing them away here.
        """
        owner = roof_owner_index(self.replaced, self.tower)
        self.assertEqual(list(owner), [0, 0, 0, 0])

    def test_a_surface_over_no_footprint_at_all_is_unplaced(self):
        owner = roof_owner_index(
            frame([{"bin": "1035349", "doitt_id": "247514", "geometry": box(900, 900, 950, 950)}]),
            self.tower,
        )
        self.assertEqual(list(owner), [-1])

    def test_a_surviving_building_keeps_its_own_roof_detail(self):
        """The symmetric case: the join must not throw real detail away.

        Grand Central still carries the BIN its 2014 roofs were modelled under,
        so its setbacks stay.
        """
        survivor = frame([
            {"bin": "1035381", "doitt_id": "49507", "geometry": box(0, 0, 100, 100)},
        ])
        roofs = frame([
            {"bin": "1035381", "doitt_id": "49507", "geometry": box(0, 0, 40, 40)},
            {"bin": "1035381", "doitt_id": "49507", "geometry": box(40, 40, 100, 100)},
        ])
        self.assertEqual(list(roof_owner_index(roofs, survivor)), [0, 0])

    def test_a_re_identified_footprint_still_matches_on_the_footprint_id(self):
        """A 2014 record with no BIN assigned joins on the id it does carry."""
        survivor = frame([
            {"bin": "1064799", "doitt_id": "494835", "geometry": box(0, 0, 100, 100)},
        ])
        roofs = frame([
            {"bin": "1000000", "doitt_id": "494835", "geometry": box(0, 0, 40, 40)},
        ])
        self.assertEqual(list(roof_owner_index(roofs, survivor)), [0])

    def test_a_neighbours_roof_belongs_to_the_neighbour(self):
        """A roof overhanging a lot line is not evidence about the lot beneath."""
        buildings = frame([
            {"bin": "1000001", "doitt_id": "1", "geometry": box(0, 0, 50, 100)},
            {"bin": "1000002", "doitt_id": "2", "geometry": box(50, 0, 100, 100)},
        ])
        roofs = frame([
            {"bin": "1000002", "doitt_id": "2", "geometry": box(45, 0, 100, 100)},
        ])
        self.assertEqual(list(roof_owner_index(roofs, buildings)), [1])

    def test_one_identity_over_several_footprint_parts_follows_the_geometry(self):
        """A building split into parts keeps each roof with the part under it."""
        parts = frame([
            {"bin": "1064238", "doitt_id": "1299035", "geometry": box(0, 0, 50, 100)},
            {"bin": "1064238", "doitt_id": "1299036", "geometry": box(50, 0, 100, 100)},
        ])
        roofs = frame([
            {"bin": "1064238", "doitt_id": "1299035", "geometry": box(60, 10, 90, 90)},
            {"bin": "1064238", "doitt_id": "1299035", "geometry": box(10, 10, 40, 90)},
        ])
        self.assertEqual(list(roof_owner_index(roofs, parts)), [1, 0])

    def test_an_empty_footprint_table_owns_nothing(self):
        owner = roof_owner_index(self.replaced, frame([]).assign(geometry=[]))
        self.assertTrue((owner < 0).all())


class SurveyStillDescribesTests(unittest.TestCase):
    """Feet of height above ground: what the 2014 surfaces reach, versus the record."""

    def test_a_tower_on_a_replaced_block_is_no_longer_described(self):
        """One Vanderbilt: 318 ft of old roofs under a 1401 ft record."""
        self.assertFalse(bool(survey_describes_building(318.0, 1401.0)))

    def test_the_inwood_library_is_no_longer_described(self):
        """38 ft of the building it replaced under a 226 ft record."""
        self.assertFalse(bool(survey_describes_building(38.0, 226.0)))

    def test_a_renumbered_complex_is_still_described(self):
        """The museum: the survey reaches 159 ft of a 147 ft record.

        The regression this guards: rejecting these surfaces flattens the whole
        complex to one height and fills its courtyards with solid mass.
        """
        self.assertTrue(bool(survey_describes_building(159.0, 147.0)))
        self.assertTrue(bool(survey_describes_building(138.6, 147.0)))

    def test_an_ordinary_building_whose_sources_agree_is_described(self):
        self.assertTrue(bool(survey_describes_building(62.8, 62.78)))

    def test_a_modest_extension_keeps_the_roof_it_has(self):
        """Shape is worth more than the last few feet of height."""
        self.assertTrue(bool(survey_describes_building(103.0, 146.0)))

    def test_a_footprint_with_no_surfaces_under_it_is_not_described(self):
        self.assertFalse(bool(survey_describes_building(0.0, 200.0)))
        self.assertFalse(bool(survey_describes_building(-3.0, 200.0)))

    def test_it_evaluates_a_whole_array_of_footprints_at_once(self):
        described = np.array([318.0, 159.0, 0.0, 62.8])
        recorded = np.array([1401.0, 147.0, 200.0, 62.78])
        self.assertEqual(list(survey_describes_building(described, recorded)),
                         [False, True, False, True])


class RoofSurfaceGridTests(unittest.TestCase):
    def test_the_higher_surface_wins_and_takes_its_owner_with_it(self):
        heights = np.full((2, 2), np.nan, np.float32)
        owners = np.zeros((2, 2), np.int32)
        everywhere = np.ones((2, 2), bool)
        keep_higher_surface(heights, owners, everywhere, np.full((2, 2), 20.0), 7)
        keep_higher_surface(heights, owners, everywhere, np.full((2, 2), 5.0), 9)
        self.assertTrue((heights == 20.0).all())
        self.assertTrue((owners == 7).all())
        keep_higher_surface(heights, owners, everywhere, np.full((2, 2), 90.0), 9)
        self.assertTrue((heights == 90.0).all())
        self.assertTrue((owners == 9).all())

    def test_an_unmeasurable_surface_leaves_the_cell_alone(self):
        heights = np.array([[12.0]], np.float32)
        owners = np.array([[4]], np.int32)
        keep_higher_surface(heights, owners, np.ones((1, 1), bool), np.array([[np.nan]]), 5)
        self.assertEqual(heights[0, 0], 12.0)
        self.assertEqual(owners[0, 0], 4)

    def test_only_a_footprints_own_roof_describes_its_top(self):
        """The failing measurement, on the grid: 96% covered, 4% left standing.

        Cells 0-95 of the footprint carry a demolished building's roof and cells
        96-99 carry none.  Position alone accepts the first group and prints the
        tower as the block it replaced; identity accepts neither, so every cell
        falls back to the one height the current record actually states.
        """
        bids = np.full((1, 100), 3, np.int32)
        roof_grid = np.where(np.arange(100) < 96, 115.0, np.nan).astype(np.float32)[None, :]
        roof_owner = np.where(np.arange(100) < 96, 8, 0).astype(np.int32)[None, :]
        by_position = np.isfinite(roof_grid) & (bids > 0)
        self.assertEqual(int(by_position.sum()), 96)
        self.assertEqual(int(own_roof_cells(roof_grid, roof_owner, bids).sum()), 0)

    def test_a_surviving_buildings_own_roof_is_still_accepted(self):
        bids = np.full((1, 100), 3, np.int32)
        roof_grid = np.full((1, 100), 115.0, np.float32)
        roof_owner = np.full((1, 100), 3, np.int32)
        self.assertEqual(int(own_roof_cells(roof_grid, roof_owner, bids).sum()), 100)

    def test_ground_outside_every_footprint_is_never_a_roof(self):
        bids = np.zeros((1, 4), np.int32)
        roof_grid = np.full((1, 4), 115.0, np.float32)
        self.assertFalse(own_roof_cells(roof_grid, np.zeros((1, 4), np.int32), bids).any())


class OsmHeightTests(unittest.TestCase):
    def test_a_bare_number_is_metres(self):
        """Simple 3D Buildings' default unit; One Vanderbilt's spire."""
        self.assertAlmostEqual(osm_height_m({"height": "427"}), 427.0)
        self.assertAlmostEqual(osm_height_m({"height": "96.4"}), 96.4)

    def test_a_written_unit_is_honoured(self):
        """Read as metres, a tower tagged in feet is three times too tall."""
        self.assertAlmostEqual(osm_height_m({"height": "427 m"}), 427.0)
        self.assertAlmostEqual(osm_height_m({"height": "100 ft"}), 30.48, places=2)
        self.assertAlmostEqual(osm_height_m({"height": "100'"}), 30.48, places=2)

    def test_an_unusable_tag_is_no_height_rather_than_a_guess(self):
        for value in [None, "", "tall", "about 40", "-5", "0", "40m2"]:
            with self.subTest(value=value):
                self.assertIsNone(osm_height_m({"height": value} if value is not None else {}))

    def test_it_reads_whichever_height_tag_is_asked_for(self):
        self.assertAlmostEqual(osm_height_m({"min_height": "96"}, "min_height"), 96.0)


class BuildingPartMassingTests(unittest.TestCase):
    """One Vanderbilt's mapped massing: setbacks at 315, 330, 350, 397, 427 m."""

    def setUp(self):
        self.tower = frame([
            {"bin": "1090825", "doitt_id": "1284912", "geometry": box(0, 0, 100, 100)},
        ])

    def parts(self, records):
        return gpd.GeoDataFrame(
            {"building:part": ["yes"] * len(records),
             "tags": [json.dumps(t) for t, _ in records]},
            geometry=[g for _, g in records], crs=2263,
        )

    def test_mapped_setbacks_reach_the_grid_with_their_owner(self):
        massing = building_part_massing(self.parts([
            ({"height": "315"}, box(0, 0, 100, 50)),
            ({"height": "397"}, box(0, 50, 100, 100)),
        ]), self.tower)
        self.assertEqual(sorted(massing.height_m), [315.0, 397.0])
        self.assertEqual(set(massing.owner_fid), {1})

    def test_a_part_with_no_usable_height_describes_nothing(self):
        massing = building_part_massing(self.parts([
            ({"roof:shape": "flat"}, box(0, 0, 100, 50)),
            ({"height": "397"}, box(0, 50, 100, 100)),
        ]), self.tower)
        self.assertEqual(list(massing.height_m), [397.0])

    def test_a_part_over_no_footprint_is_dropped(self):
        massing = building_part_massing(
            self.parts([({"height": "40"}, box(900, 900, 950, 950))]), self.tower)
        self.assertEqual(len(massing), 0)

    def test_a_neighbours_massing_stays_with_the_neighbour(self):
        buildings = frame([
            {"bin": "1000001", "doitt_id": "1", "geometry": box(0, 0, 50, 100)},
            {"bin": "1000002", "doitt_id": "2", "geometry": box(50, 0, 100, 100)},
        ])
        massing = building_part_massing(
            self.parts([({"height": "300"}, box(45, 0, 100, 100))]), buildings)
        self.assertEqual(list(massing.owner_fid), [2])

    def test_no_parts_at_all_is_an_empty_answer_not_a_failure(self):
        empty = gpd.GeoDataFrame({"building:part": [], "tags": []},
                                 geometry=[], crs=2263)
        self.assertEqual(len(building_part_massing(empty, self.tower)), 0)
        self.assertEqual(len(building_part_massing(
            gpd.GeoDataFrame({"tags": []}, geometry=[], crs=2263), self.tower)), 0)

    def test_massing_is_judged_by_the_same_rule_as_the_survey(self):
        """A tower's parts must still reach half its recorded height to be used.

        Mapped massing left behind by a rebuild is no better evidence than a
        superseded survey, and is rejected the same way.
        """
        self.assertTrue(bool(survey_describes_building(397.0, 427.0)))
        self.assertFalse(bool(survey_describes_building(60.0, 427.0)))


if __name__ == "__main__":
    unittest.main()
