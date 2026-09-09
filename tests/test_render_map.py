import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import numba

from render_map import camera,render


def quad(x0,y0,x1,y1,z):
    """Two triangles covering an axis-aligned rectangle at a fixed height."""
    corners=[(x0,y0,z),(x1,y0,z),(x1,y1,z),(x0,y1,z)]
    return np.array([[corners[0],corners[1],corners[2]],
                     [corners[0],corners[2],corners[3]]],np.float32)


def scene():
    """A wide low slab with a narrower slab floating above its middle."""
    low=quad(60,60,140,140,1.);high=quad(85,85,115,115,6.)
    green=np.broadcast_to(np.array([.37,.67,.45],np.float32),(2,3))
    blue=np.broadcast_to(np.array([.66,.84,.87],np.float32),(2,3))
    return (np.ascontiguousarray(np.concatenate([low,high])),
        np.ascontiguousarray(np.concatenate([green,blue])))


# Straight down, so a pixel's colour follows from its x/y alone.
TOP=dict(az=-90,el=89.99,center=(100,100,4),span=220,w=192,h=128)


class RenderMapTests(unittest.TestCase):
    def test_band_height_does_not_change_the_image(self):
        # Bands partition the scanlines across threads.  A triangle spanning a
        # boundary must be drawn by every band it reaches and clipped to each,
        # so the result cannot depend on where the boundaries fall.
        P,colors=scene();cam=camera(**TOP)
        coarse=render(P,colors,cam,band=128)
        for band in (8,16,48):
            np.testing.assert_array_equal(render(P,colors,cam,band=band),coarse)

    def test_thread_count_does_not_change_the_image(self):
        # Each band owns a disjoint slab of rows, so the shared depth and image
        # buffers are never written by two threads at the same pixel.
        P,colors=scene();cam=camera(**TOP)
        threads=numba.get_num_threads()
        try:
            numba.set_num_threads(1)
            serial=render(P,colors,cam,band=8)
            numba.set_num_threads(max(2,threads))
            np.testing.assert_array_equal(render(P,colors,cam,band=8),serial)
        finally:
            numba.set_num_threads(threads)

    def test_nearer_surface_wins_over_the_one_below_it(self):
        P,colors=scene();cam=camera(**TOP)
        image=render(P,colors,cam)
        middle=image[TOP['h']//2,TOP['w']//2]
        skirt=image[TOP['h']//2,int(TOP['w']*.42)]
        corner=image[0,0]
        self.assertEqual(int(np.argmax(middle)),2,'high slab reads blue')
        self.assertEqual(int(np.argmax(skirt)),1,'low slab reads green')
        # Beyond the slabs is the cream floor the renderer always lays down, not
        # the background; only a view reaching past its edge would show that.
        self.assertEqual(int(np.argmax(corner)),0,'floor reads cream')
        self.assertGreater(int(corner.min()),int(skirt.max()),'floor is the brightest')


if __name__=='__main__':
    unittest.main()
