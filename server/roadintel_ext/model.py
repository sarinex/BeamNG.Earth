"""Deterministic cross-sections shared by grading, painting, meshes and navigation.

Offsets are positive to the left of feature geometry. Mapped values and estimates
are separate; conflicts fail closed, never turn an ambiguous count into a fact.
"""
from dataclasses import dataclass, asdict
import math
import re


def count(value):
    text = str(value or "")
    return int(text) if re.fullmatch(r"[0-9]+", text) and 0 <= int(text) <= 12 else None


def metres(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m|ft|')?\s*", str(value or ""))
    if not match:
        return None
    result = float(match[1]) * (0.3048 if match[2] in ("ft", "'") else 1)
    return result if math.isfinite(result) and 0 <= result <= 40 else None


def turn_lanes(value, n):
    if not value:
        return ()
    lanes = str(value).split("|")
    if len(lanes) != n:
        raise ValueError("turn:lanes count disagrees with directional lanes")
    known = {"left", "slight_left", "sharp_left", "through", "right", "slight_right",
             "sharp_right", "reverse", "merge_to_left", "merge_to_right", "none", ""}
    result = tuple(tuple(part.split(";")) for part in lanes)
    if any(turn not in known for lane in result for turn in lane):
        raise ValueError("unsupported turn:lanes token")
    return result


@dataclass(frozen=True)
class CrossSection:
    forward: int
    backward: int
    direction: int
    lane_widths: tuple
    lane_offsets: tuple
    divider_offsets: tuple
    centre: object
    drive_left: float
    drive_right: float
    parking_left: float
    parking_right: float
    shoulder_left: float
    shoulder_right: float
    turns_forward: tuple
    turns_backward: tuple
    confidence: str
    provenance: str
    notes: tuple
    mapped_width: object
    envelope_width: float

    @property
    def driving_width(self):
        return self.drive_left - self.drive_right

    def diagnostic(self):
        return asdict(self)


def normalize(tags, spec, *, reverse=False, left_hand=False, provenance="OSM extract"):
    """Return supported section or raise ValueError with an explicit fallback reason.

    Native OSM direction is reversed only after geometric correspondence is proven.
    Both-ways lanes/conditional access need a richer engine contract: reject here.
    """
    tags = dict(tags)
    if any(":" in k and ("conditional" in k or k.startswith(("bus:lanes", "psv:lanes", "access:lanes"))) for k in tags):
        raise ValueError("conditional or reserved lane access is not representable")
    if count(tags.get("lanes:both_ways")) not in (None, 0):
        raise ValueError("shared both-ways lane is not representable by BeamNG lanes")
    if tags.get("motor_vehicle", tags.get("vehicle", tags.get("access"))) in ("no", "private"):
        raise ValueError("passenger access restricted")
    oneway = tags.get("oneway", "yes" if tags.get("junction") == "roundabout" else "")
    if oneway not in ("", "no", "0", "false", "yes", "1", "true", "-1"):
        raise ValueError("ambiguous oneway")
    direction = -1 if oneway == "-1" else 1 if oneway in ("yes", "1", "true") else 0
    total, f, b = (count(tags.get(k)) for k in ("lanes", "lanes:forward", "lanes:backward"))
    for k in ("lanes", "lanes:forward", "lanes:backward", "lanes:both_ways"):
        if k in tags and count(tags[k]) is None:
            raise ValueError(f"ambiguous {k}")
    notes = []
    if direction:
        n = total or (b if direction < 0 else f)
        if not n or (f is not None and f != (n if direction > 0 else 0)) or (b is not None and b != (n if direction < 0 else 0)):
            raise ValueError("incomplete or conflicting one-way lanes")
        f, b = (n, 0) if direction > 0 else (0, n)
    else:
        if total is not None:
            if f is not None and b is None:
                b = total - f
            elif b is not None and f is None:
                f = total - b
            elif f is None and b is None and total % 2 == 0:
                f = b = total // 2
                notes.append("equal directional split estimated from even total")
        if f is None or b is None or f <= 0 or b <= 0 or (total is not None and total != f + b):
            raise ValueError("incomplete or conflicting directional lanes")
    total = f + b
    if not 1 <= total <= 12:
        raise ValueError("lane count outside supported bounds")
    tf = turn_lanes(tags.get("turn:lanes:forward", tags.get("turn:lanes") if direction > 0 else None), f)
    tb = turn_lanes(tags.get("turn:lanes:backward", tags.get("turn:lanes") if direction < 0 else None), b)
    parking, shoulder = [], []
    for side in ("left", "right"):
        p = tags.get(f"parking:{side}", tags.get("parking:both", tags.get(f"parking:lane:{side}", tags.get("parking:lane:both"))))
        mapped = metres(tags.get(f"parking:{side}:width", tags.get("parking:both:width")))
        parking.append((mapped if mapped is not None else 2.3) if p in ("lane", "parallel", "diagonal", "perpendicular") else 0.0)
        if parking[-1] and mapped is None:
            notes.append(f"{side} parking width estimated")
        shoulder.append(metres(tags.get(f"shoulder:{side}:width", tags.get("shoulder:width"))) or 0.0)
    widths = None
    raw = tags.get("width:lanes")
    if raw:
        widths = tuple(metres(x) for x in raw.split("|"))
        if len(widths) != total or any(x is None or not 1.8 <= x <= 5.5 for x in widths):
            raise ValueError("invalid width:lanes")
    elif tags.get("width:lanes:forward") or tags.get("width:lanes:backward"):
        wf = tuple(metres(x) for x in tags.get("width:lanes:forward", "").split("|")) if f else ()
        wb = tuple(metres(x) for x in tags.get("width:lanes:backward", "").split("|")) if b else ()
        if len(wf) != f or len(wb) != b or any(x is None or not 1.8 <= x <= 5.5 for x in wf + wb):
            raise ValueError("incomplete directional lane widths")
        widths = wf + wb if left_hand else tuple(reversed(wb)) + wf
    import roads
    mapped_width = metres(tags.get("width"))
    available = (mapped_width - sum(parking) - sum(shoulder)) if mapped_width else None
    if widths is None:
        lane_width = available / total if available is not None else roads.LANE_W.get(spec.cls, roads.LANE_W_DEFAULT)
        widths = (lane_width,) * total
        notes.append("lane widths allocated from mapped total width" if mapped_width else "class lane width estimated")
    elif available is not None and abs(sum(widths) - available) > 0.5:
        raise ValueError("mapped lane widths conflict with mapped total width")
    if any(w < 1.8 or w > 5.5 for w in widths):
        raise ValueError("lane count does not fit existing pavement envelope")
    if reverse:
        f, b, direction = b, f, -direction
        parking.reverse(); shoulder.reverse()
        widths = tuple(reversed(widths))
        tf, tb = tb, tf
    # OSM width:lanes is ordered left-to-right viewed in travel direction; local physical order below is left-to-right.
    left_count = f if left_hand else b
    drive = sum(widths)
    shift = ((parking[1] + shoulder[1]) - (parking[0] + shoulder[0])) / 2
    l, r = drive / 2 + shift, -drive / 2 + shift
    offsets, boundaries, pos = [], [], l
    for w in widths:
        offsets.append(pos - w / 2)
        pos -= w
        boundaries.append(pos)
    centre = boundaries[left_count - 1] if f and b else None
    dividers = tuple(v for j, v in enumerate(boundaries[:-1]) if j + 1 != left_count or centre is None)
    envelope = sum(widths) + sum(parking) + sum(shoulder)
    return CrossSection(f, b, direction, tuple(widths), tuple(offsets), dividers, centre, l, r,
                        *parking, *shoulder, tf, tb, "mapped-counts/estimated-widths" if notes else "mapped",
                        provenance, tuple(notes), mapped_width, envelope)


def markings(spec, pts, lon, left_hand=False):
    """Stock ribbons; keep upstream sidewalks/gutters, replace only lane/edge/centre paint."""
    import roads
    section = spec.intel
    base = roads.extras(spec._replace(intel=None), pts, lon, left_hand)
    result = [row for row in base if row[0].get("side") is not None]
    if spec.kind != "paved":
        return result
    for offset in section.divider_offsets:
        result.append((roads.LANE_LINE, roads.LANE_LINE["w"], roads.offset_polyline(pts, offset)))
    if section.centre is not None:
        if roads.centre_colour(lon) == "line_white":
            result.append((roads.LANE_LINE, roads.LANE_LINE["w"], roads.offset_polyline(pts, section.centre)))
        else:
            for side in (-1, 1):
                result.append(({"m": roads.centre_colour(lon), "tl": 12, "p": 6}, roads.CENTRE["w"],
                               roads.offset_polyline(pts, section.centre + side * roads.CENTRE["gap"])))
    if spec.cls in roads.EDGE_LINE_CLASSES:
        for offset in (section.drive_left, section.drive_right):
            result.append((roads.EDGE_LINE, roads.EDGE_LINE["w"], roads.offset_polyline(pts, offset)))
    return result
