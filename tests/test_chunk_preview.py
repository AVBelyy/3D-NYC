"""The preview page is sized by the map, not by the raster budget."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chunk_preview as preview  # noqa: E402
from test_chunk_cost import a_surface  # noqa: E402


def a_page(cells, pixels=preview.DEFAULT_PIXELS, resolution_m=4.0):
    surface = a_surface(np.zeros(cells, dtype=np.float32), resolution_m=resolution_m)
    image = np.zeros((*cells, 3), dtype=np.float32)
    figure, _, _ = preview._figure(surface, image, "title", pixels)
    try:
        return figure.get_size_inches(), figure.dpi
    finally:
        preview.plt.close(figure)


class PageSizeTests(unittest.TestCase):
    def test_a_page_is_as_big_as_the_map_it_shows(self):
        (small, _), (large, _) = a_page((800, 800)), a_page((1600, 1600))
        self.assertAlmostEqual(large[0] / small[0], 2.0, places=6)
        self.assertAlmostEqual(large[1] / small[1], 2.0, places=6)

    def test_the_raster_budget_does_not_change_the_page(self):
        """The defect: every plan opened at one size whatever it covered."""
        coarse, fine = a_page((800, 800), pixels=3000), a_page((800, 800), pixels=9000)
        self.assertEqual(tuple(coarse[0]), tuple(fine[0]))
        # It buys resolution instead, which is what it is documented to do.
        self.assertAlmostEqual(fine[1] / coarse[1], 3.0, places=6)

    def test_the_basemap_still_gets_the_pixels_it_asked_for(self):
        for cells, pixels in (((400, 900), 6000), ((900, 400), 3000), ((200, 200), 6000)):
            inches, dpi = a_page(cells, pixels=pixels)
            self.assertAlmostEqual(max(inches) * dpi, max(pixels, *cells), places=3)

    def test_one_pixel_per_cost_cell_is_the_floor(self):
        inches, dpi = a_page((9000, 4000), pixels=1000)
        self.assertAlmostEqual(max(inches) * dpi, 9000, places=3)

    def test_a_tiny_target_still_opens_big_enough_to_read(self):
        inches, _ = a_page((20, 20))
        self.assertAlmostEqual(max(inches), preview.MINIMUM_PAGE_INCHES, places=6)


if __name__ == "__main__":
    unittest.main()
