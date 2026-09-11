"""A stage cache key has to carry the keys of the stages it reads.

The reported failure: bumping the field version reran ``build_fields`` and
nothing else, so every plate kept the meshes cut from the old fields, shipped
the same 3MF, and looked like the fix had been ignored.  Stage caching hashes
the config and the key only -- never the code -- so the key is the only place a
dependency can be declared.
"""

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_3mf  # noqa: E402
from generate_3mf import stage_variants  # noqa: E402


SOURCES = ("nyc_planimetrics_2022", "nyc_building_footprints", "nyc_parks_trails",
           "nyc_3d_buildings_2014", "nyc_land_cover_2021", "new_york_osm",
           "nyc_parks_structures", "mta_subway_entrances_2024")
# Stages built from the fused fields, directly or through the mesh and the 3MF.
DERIVED_FROM_FIELDS = ("build_fields", "validate_crossing_fields", "build_meshes",
                       "render_preview", "package_3mf", "validate_3mf", "slice")
FEEDS_THE_FIELDS = ("prepare_vectors", "extract_citygml", "prepare_landcover",
                    "extract_osm", "prepare_details")


def keys(**cache_versions):
    """Stage keys as one identity per source, so a changed source is visible."""
    return stage_variants(lambda name: {"cache": cache_versions.get(name, name)})


class StageDependencyTests(unittest.TestCase):
    def test_a_field_version_bump_restales_everything_built_from_the_fields(self):
        """The reported failure, stated as the rule it broke."""
        before = keys()
        with patch.object(generate_3mf, "FIELD_PIPELINE_VERSION",
                          generate_3mf.FIELD_PIPELINE_VERSION + 1):
            after = keys()
        for stage in DERIVED_FROM_FIELDS:
            with self.subTest(stage=stage):
                self.assertNotEqual(before[stage], after[stage])

    def test_a_field_version_bump_leaves_the_stages_that_feed_it_alone(self):
        """The symmetric case: re-extracting sources on every bump is waste."""
        before = keys()
        with patch.object(generate_3mf, "FIELD_PIPELINE_VERSION",
                          generate_3mf.FIELD_PIPELINE_VERSION + 1):
            after = keys()
        for stage in FEEDS_THE_FIELDS + ("prepare_lidar",):
            with self.subTest(stage=stage):
                self.assertEqual(before[stage], after[stage])

    def test_a_mesh_version_bump_reaches_the_model_but_not_the_fields(self):
        before = keys()
        with patch.object(generate_3mf, "MESH_PIPELINE_VERSION",
                          generate_3mf.MESH_PIPELINE_VERSION + 1):
            after = keys()
        for stage in ("build_meshes", "render_preview", "package_3mf", "validate_3mf", "slice"):
            with self.subTest(stage=stage):
                self.assertNotEqual(before[stage], after[stage])
        self.assertEqual(before["build_fields"], after["build_fields"])

    def test_a_package_version_bump_reaches_validation_and_slicing(self):
        before = keys()
        with patch.object(generate_3mf, "PACKAGE_PIPELINE_VERSION",
                          generate_3mf.PACKAGE_PIPELINE_VERSION + 1):
            after = keys()
        for stage in ("package_3mf", "validate_3mf", "slice"):
            with self.subTest(stage=stage):
                self.assertNotEqual(before[stage], after[stage])
        self.assertEqual(before["build_meshes"], after["build_meshes"])

    def test_a_rebuilt_source_cache_restales_the_same_chain(self):
        """A source rebuild is the other way the fused fields change."""
        for source in SOURCES:
            with self.subTest(source=source):
                before = keys()
                after = keys(**{source: "rebuilt"})
                for stage in DERIVED_FROM_FIELDS:
                    self.assertNotEqual(before[stage], after[stage])

    def test_an_unrelated_source_leaves_a_stage_that_never_reads_it_alone(self):
        """Keys have to discriminate, or caching stops meaning anything."""
        before = keys()
        after = keys(new_york_osm="rebuilt")
        self.assertEqual(before["extract_citygml"], after["extract_citygml"])
        self.assertEqual(before["prepare_vectors"], after["prepare_vectors"])
        self.assertNotEqual(before["extract_osm"], after["extract_osm"])

    def test_every_cacheable_stage_the_pipeline_runs_declares_a_key(self):
        """A stage wired without an entry here would cache on its config alone."""
        source = (Path(generate_3mf.__file__)).read_text()
        wired = set(re.findall(r'self\.stage\(\s*\n?\s*"([a-z0-9_]+)"', source))
        # ``download_sources`` revalidates every run and is explicitly uncacheable.
        self.assertEqual(wired - {"download_sources"}, set(keys()))


if __name__ == "__main__":
    unittest.main()
