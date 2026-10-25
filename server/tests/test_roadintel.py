"""Offline Group 4 contracts, including real pinned netconvert (no simulator/game)."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from roadintel_ext.model import normalize, turn_lanes
from roadintel_ext.runtime import Dataset, MapView, effective_specs
from roadintel_ext.sumo import preprocess, convert, executable, region
import navgraph
import roads
import surface
from test_roads import features, FRAME, flat, rules


def spec(width=10):
    return roads.Spec(width, "paved", False, "primary")


class Layout(unittest.TestCase):
    def test_asymmetric_counts_widths_and_markings(self):
        s = normalize({"lanes": "3", "lanes:forward": "2", "lanes:backward": "1", "width": "9"}, spec())
        self.assertEqual((s.forward, s.backward, s.centre), (2, 1, 1.5))
        self.assertEqual(s.divider_offsets, (-1.5,))
        self.assertEqual(s.envelope_width, 9)
        self.assertEqual(s.driving_width, 9)
        road = spec()._replace(width=s.envelope_width, intel=s)
        self.assertEqual(navgraph.driving_width(road), 9)
        marks = roads.extras(road, np.array([[0., 0., 0.], [50., 0., 0.]]), -76)
        self.assertTrue(any(abs(row[2][0, 1] - 1.62) < 1e-6 for row in marks))

    def test_left_hand_and_reverse(self):
        tags = {"lanes:forward": "2", "lanes:backward": "1", "width": "9"}
        a = normalize(tags, spec(), left_hand=True)
        self.assertEqual(a.centre, -1.5)
        b = normalize(tags, spec(), reverse=True)
        self.assertEqual((b.forward, b.backward, b.centre), (1, 2, -1.5))
        for direction, reverse, expected in (("yes", False, 1), ("-1", False, -1), ("yes", True, -1)):
            a = normalize({"lanes": "2", "oneway": direction}, spec(), reverse=reverse)
            self.assertEqual(a.direction, expected)
            self.assertEqual(a.forward + a.backward, 2)
            self.assertIsNone(a.centre)

    def test_turn_lanes_and_lane_widths(self):
        a = normalize({"oneway": "yes", "lanes": "3", "turn:lanes": "left|through|through;right",
                       "width:lanes": "3|3.2|3.4"}, spec())
        self.assertEqual(a.turns_forward[2], ("through", "right"))
        self.assertAlmostEqual(a.envelope_width, 9.6)
        self.assertEqual(a.confidence, "mapped")
        with self.assertRaises(ValueError):
            turn_lanes("left|through", 3)

    def test_parking_shoulders_width_and_missing(self):
        a = normalize({"lanes": "2", "parking:right": "lane", "parking:right:width": "2",
                       "shoulder:left:width": "1", "width": "9"}, spec())
        self.assertEqual((a.parking_right, a.shoulder_left, a.driving_width), (2, 1, 6))
        self.assertEqual((a.drive_left, a.drive_right), (3.5, -2.5))
        self.assertIn("estimated", a.confidence)
        for tags in ({}, {"lanes": "3"}, {"lanes": "2;3"}, {"lanes": "4", "lanes:forward": "3", "lanes:backward": "2"},
                     {"lanes": "3", "lanes:both_ways": "1"}, {"lanes": "2", "access": "no"},
                     {"lanes": "2", "oneway": "reversible"}):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                normalize(tags, spec())

    def test_all_geometry_combinations_and_shared_surface(self):
        f = features([[(20, 100), (240, 100)]], ["primary"])
        original = roads.road_specs(f)[0]
        tagged = replace(f, attrs=dict(f.attrs), rules=dict(f.rules))
        tagged._roadintel = [{"tags": {"lanes": "3", "lanes:forward": "2", "width": "9"},
                              "reverse": False, "provenance": "fixture", "region": {"leftHand": False}}]
        for intelligence, feats in (("legacy", f), ("sumo", tagged)):
            expected = original.width if intelligence == "legacy" else 9
            for geometry in ("legacy", "geos"):
                chunks = roads.build_tile(FRAME, 0, 0, [feats], flat, backend=geometry)
                self.assertTrue(chunks)
                self.assertEqual(roads.road_specs(feats)[0].width, expected)
            paint = surface._road_segments(FRAME, 0, 0, 256, 256, 256, [feats])
            self.assertTrue(np.allclose(paint[1], expected / 2))
        self.assertIsNone(roads.road_specs(f)[0].intel)
        self.assertEqual(roads.road_specs(f)[0].width, original.width)


if __name__ == "__main__":
    unittest.main()
