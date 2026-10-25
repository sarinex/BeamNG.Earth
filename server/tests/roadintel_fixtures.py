"""Synthetic OSM fixtures around the existing offline road-test frame, never mapped facts."""
import xml.etree.ElementTree as ET

from test_roads import features, rules, M_LON, M_LAT, LON, LAT
import numpy as np
from types import SimpleNamespace


def network(path, kind="cross", restriction=True, left_hand=False):
    coords = {1: (20, 120), 2: (120, 120), 3: (240, 120), 4: (120, 20), 5: (120, 240)}
    ways = {10: [1, 2], 11: [2, 3], 20: [4, 2], 21: [2, 5]}
    if kind == "T":
        del ways[21]
    elif kind == "multiway":
        coords[6], coords[7] = (30, 30), (230, 230)
        ways[30], ways[31] = [6, 2], [2, 7]
    elif kind == "offset":
        coords[6] = (130, 120)
        ways[11], ways[21], ways[12] = [6, 3], [6, 5], [2, 6]
    elif kind == "ramp":
        coords[6] = (230, 200)
        ways[30] = [2, 6]
    elif kind == "roundabout":
        coords = {1: (20, 120), 2: (90, 120), 3: (120, 150), 4: (150, 120), 5: (120, 90), 6: (240, 120)}
        ways = {10: [1, 2], 11: [4, 6], 30: [2, 3, 4, 5, 2]}
        if not left_hand:
            ways[30].reverse()
    elif kind == "bridge":
        # Same XY crossing, distinct OSM/Overture connectors and level: never join.
        coords[6] = coords[2]
        ways[20], ways[21] = [4, 6], [6, 5]
    root = ET.Element("osm", version="0.6")
    for nid, (x, y) in coords.items():
        ET.SubElement(root, "node", id=str(nid), lon=str(LON + x / M_LON), lat=str(LAT + y / M_LAT), version="1")
    for wid, refs in ways.items():
        el = ET.SubElement(root, "way", id=str(wid), version="1")
        for nid in refs:
            ET.SubElement(el, "nd", ref=str(nid))
        tags = {"highway": "primary", "lanes": "3", "lanes:forward": "2", "lanes:backward": "1", "width": "9.9"}
        if kind == "roundabout" and wid == 30:
            tags = {"highway": "primary", "junction": "roundabout", "oneway": "yes", "lanes": "2", "width": "6.6"}
        if kind == "ramp":
            tags = {"highway": "motorway_link" if wid == 30 else "motorway", "oneway": "yes", "lanes": "1" if wid == 30 else "2"}
        if kind == "bridge" and wid in (20, 21):
            tags.update(bridge="yes", layer="1")
        for k, v in tags.items():
            ET.SubElement(el, "tag", k=k, v=v)
    if restriction and kind in ("cross", "T"):
        r = ET.SubElement(root, "relation", id="100", version="1")
        for typ, ref, role in (("way", 10, "from"), ("node", 2, "via"), ("way", 21 if kind == "cross" else 11, "to")):
            ET.SubElement(r, "member", type=typ, ref=str(ref), role=role)
        ET.SubElement(r, "tag", k="type", v="restriction")
        ET.SubElement(r, "tag", k="restriction", v="no_left_turn" if kind == "cross" else "no_straight_on")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    order = sorted(ways)
    f = features([[coords[n] for n in ways[w]] for w in order], ["primary"] * len(order),
                 levels=[(j, 1) for j, w in enumerate(order) if kind == "bridge" and w in (20, 21)])
    f.attrs["src_record"] = np.array([f"w{w}@1" for w in order])
    endpoints = {n for refs in ways.values() for n in (refs[0], refs[-1])}
    f.rules["connectors"] = rules([(i, str(ref), j / (len(ways[w]) - 1), j / (len(ways[w]) - 1))
                                   for i, w in enumerate(order) for j, ref in enumerate(ways[w]) if ref in endpoints])
    return f


class FixtureMap:
    release = "synthetic-offline"

    def __init__(self, feats):
        self.feats = feats

    def cell(self, key, layers=(), **kwargs):
        return SimpleNamespace(layers={name: self.feats if name == "roads" else None for name in layers})

    def has(self, *args):
        return True

    def prefetch(self, *args, **kwargs):
        return True


class FixtureSurface:
    def __init__(self, md):
        self.md = md

    def urban(self, la, lo):
        return np.zeros(np.shape(la), bool)

    def classes(self, la, lo):
        return np.full(np.shape(la), 30, np.uint8), True

    def roads(self, la, lo):
        return [self.md.cell(None, layers=("roads",)).layers["roads"]], True

    def lots(self, la, lo):
        return []


class FixtureDem:
    def sample(self, la, lo, zoom, progress=None):
        return 10 + (np.asarray(la) - LAT) * M_LAT * 0.03


def offline_server(work, feats):
    from earthd import Server
    import netfetch
    md = FixtureMap(feats)
    server = Server(str(work / "user"), str(work / "raw"), tile_workers=1, imagery=[],
                    near_visual="mesh", block=512, mapdata=md, surface=FixtureSurface(md),
                    grading=True, traffic=True, net=netfetch.Fetcher(offline=True))
    server.dem = FixtureDem()
    return server


def close_server(server):
    from concurrent.futures import Executor
    for value in vars(server).values():
        if isinstance(value, Executor):
            value.shutdown(wait=True, cancel_futures=True)
