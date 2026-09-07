import atexit
import os
import sys
import tempfile
import unittest
from pathlib import Path


# ``build_map_fields`` imports ``map_common``, which resolves and creates work
# directories at import time.  Point every one of them at a throwaway directory
# so the classifier tests never read or write a developer's real caches.
_SANDBOX = tempfile.TemporaryDirectory()
atexit.register(_SANDBOX.cleanup)
for _name in ["NYC_DATA_DIR", "NYC_RAW_DIR", "NYC_CACHE_DIR", "NYC_OUTPUT_DIR",
              "MAP_WORK_DIR", "NYC_VALID_DIR", "NYC_PROCESSED_DIR", "NYC_ANALYSIS_DIR"]:
    os.environ[_name] = str(Path(_SANDBOX.name) / _name.lower())

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_map_fields import (  # noqa: E402
    green_fallback_kind,
    recreation_material,
    tag_values,
)


class SemanticGreenFallbackTests(unittest.TestCase):
    def test_institutional_lawn_is_green_not_the_ivory_default(self):
        """Columbia's South Field: land cover calls it impervious, no PARK polygon."""
        for tags in [{"landuse": "recreation_ground", "name": "Butler Library West Commons"},
                     {"landuse": "recreation_ground", "name": "Butler Library East Commons"}]:
            with self.subTest(name=tags["name"]):
                self.assertEqual(green_fallback_kind(tags), "recreation_ground")

    def test_park_outside_the_city_park_layer_is_green(self):
        self.assertEqual(green_fallback_kind({"leisure": "park"}), "park")

    def test_previously_mapped_green_keeps_its_kind(self):
        self.assertEqual(green_fallback_kind({"leisure": "garden"}), "garden")
        self.assertEqual(green_fallback_kind({"leisure": "dog_park"}), "dog_park")
        self.assertEqual(green_fallback_kind({"landuse": "grass"}), "grass")

    def test_semicolon_alternatives_still_match(self):
        """OSM encodes alternative values on one key; an exact match misses them."""
        self.assertEqual(tag_values({"leisure": "garden;outdoor_seating;park"}, "leisure"),
                         ["garden", "outdoor_seating", "park"])
        self.assertEqual(green_fallback_kind({"leisure": "garden;outdoor_seating;park"}), "garden")
        self.assertEqual(green_fallback_kind({"leisure": "outdoor_seating;park"}), "park")
        self.assertEqual(recreation_material({"leisure": "pitch;fitness_station"}), 1)

    def test_tag_values_tolerates_missing_and_non_string_tags(self):
        self.assertEqual(tag_values({}, "leisure"), [])
        self.assertEqual(tag_values({"leisure": None}, "leisure"), [])
        self.assertEqual(tag_values({"leisure": 3}, "leisure"), [])
        self.assertEqual(tag_values({"leisure": "park;;"}, "leisure"), ["park"])
        self.assertIsNone(green_fallback_kind({}))

    def test_non_green_land_use_is_still_rejected(self):
        """The symmetric case: blanket block-level land uses must not turn green.

        ``landuse=residential`` covers whole superblocks including their streets
        and buildings, so honouring it would flood the plate with false lawn.
        """
        for tags in [{"landuse": "residential"}, {"landuse": "commercial"},
                     {"landuse": "retail"}, {"landuse": "industrial"},
                     {"landuse": "construction"}, {"amenity": "parking"},
                     {"amenity": "university", "name": "Columbia University"},
                     {"leisure": "swimming_pool"}, {"leisure": "sports_centre"},
                     {"building": "yes"}, {"leisure": "residential;commercial"}]:
            with self.subTest(tags=tags):
                self.assertIsNone(green_fallback_kind(tags))

    def test_recreation_ground_stays_in_the_low_priority_fallback(self):
        """Only surface-independent recreation outranks mapped paths and pavement.

        A lawn is green because nothing paved was surveyed over it, so it must
        yield to a measured sidewalk or plaza the way the other fallbacks do.
        """
        self.assertIsNone(recreation_material({"landuse": "recreation_ground"}))
        self.assertIsNone(recreation_material({"leisure": "park"}))
        self.assertEqual(recreation_material({"leisure": "playground"}), 1)
        self.assertEqual(recreation_material({"leisure": "pitch"}), 1)
        self.assertEqual(recreation_material({"leisure": "track"}), 1)


if __name__ == "__main__":
    unittest.main()
