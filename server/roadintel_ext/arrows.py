"""Mapped turn arrows from generated ribbons using the existing stock line_white material.

Only complete, geometrically matched native approaches are eligible. Unknown,
merge, reverse and sharp-turn glyphs are omitted rather than guessed.
"""
import numpy as np


def ribbons(section, pts, left_hand=False):
    pts = np.asarray(pts, float)
    arcs = np.r_[0, np.cumsum(np.linalg.norm(np.diff(pts[:, :2], axis=0), axis=1))]
    if arcs[-1] < 35:
        return []
    look = {"m": "line_white", "tl": 6.4, "p": 2, "intelArrow": True}
    result = []
    forward_indices = list(range(section.forward)) if left_hand else list(range(section.backward, len(section.lane_offsets)))
    backward_indices = list(range(section.forward, len(section.lane_offsets))) if left_hand else list(range(section.backward))
    for forward, enabled, turns, indices in ((True, section.arrows_forward, section.turns_forward, forward_indices),
                                             (False, section.arrows_backward, section.turns_backward, list(reversed(backward_indices)))):
        if not enabled or len(turns) != len(indices):
            continue
        at = arcs[-1] - 20 if forward else 20
        pos = np.array([np.interp(at, arcs, pts[:, d]) for d in range(3)])
        k = min(len(pts) - 2, int(np.searchsorted(arcs, at, side="right") - 1))
        tangent = pts[k + 1, :2] - pts[k, :2]
        tangent /= max(np.linalg.norm(tangent), 1e-9)
        physical_left = np.array([-tangent[1], tangent[0], 0.])
        travel = np.r_[tangent if forward else -tangent, 0.]
        left = np.array([-travel[1], travel[0], 0.])
        for movements, index in zip(turns, indices):
            if not movements or any(m not in ("left", "through", "right") for m in movements):
                continue
            centre = pos + physical_left * section.lane_offsets[index]
            def stroke(a, b):
                result.append((look, 0.18, np.array([a, b])))
            stroke(centre - 2 * travel, centre)
            for movement in movements:
                direction = travel if movement == "through" else left * (1 if movement == "left" else -1)
                tip = centre + (2 * travel if movement == "through" else 0.5 * travel + 1.1 * direction)
                normal = np.array([-direction[1], direction[0], 0.])
                stroke(centre, tip)
                stroke(tip, tip - .6 * direction + .35 * normal)
                stroke(tip, tip - .6 * direction - .35 * normal)
    return result
