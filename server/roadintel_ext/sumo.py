"""Offline native OSM importer. Never invoked by a road-tile build."""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from . import VERSION

MAX_WAYS = 30000
MAX_NODES = 250000
MAX_OUTPUT = 128 * 1024 * 1024
REGION_DEG = 0.05
HALO_DEG = 0.02


def executable():
    import sumo
    path = Path(sumo.SUMO_HOME) / "bin" / ("netconvert.exe" if os.name == "nt" else "netconvert")
    if not path.is_file():
        raise FileNotFoundError(f"netconvert not found under sumo.SUMO_HOME: {path}")
    return path


def region(lat, lon):
    x, y = math.floor((lon + 180) / REGION_DEG), math.floor((lat + 90) / REGION_DEG)
    west, south = round(x * REGION_DEG - 180, 8), round(y * REGION_DEG - 90, 8)
    return f"{x}_{y}", (west, south, west + REGION_DEG, south + REGION_DEG)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def extract(source, bounds, target):
    """Two streaming passes; complete ways + member nodes + complete restriction relations.

    File-backed PyOsmium location index bounds RAM even for large input extracts.
    No fetching: downloading a licensed appropriately scoped extract is explicit.
    """
    import osmium
    west, south, east, north = bounds
    ways, relations, needed = {}, [], set()

    class Ways(osmium.SimpleHandler):
        def way(self, w):
            if "highway" not in w.tags:
                return
            valid = all(n.location.valid() for n in w.nodes)
            if not valid or len(w.nodes) < 2:
                return
            xy = [(n.lon, n.lat) for n in w.nodes]
            xs, ys = zip(*xy)
            if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
                return
            if len(ways) >= MAX_WAYS:
                raise ValueError("region exceeds road intelligence way budget")
            ways[str(w.id)] = {"tags": dict(w.tags), "version": w.version, "shape": xy,
                               "nodes": [n.ref for n in w.nodes]}
            needed.update(n.ref for n in w.nodes)
            if len(needed) > MAX_NODES:
                raise ValueError("region exceeds road intelligence node budget")

        def relation(self, r):
            if r.tags.get("type") == "restriction":
                relations.append((r.id, dict(r.tags), [(m.type, m.ref, m.role) for m in r.members]))
                if len(relations) > MAX_WAYS:
                    raise ValueError("extract exceeds restriction budget; use a smaller extract")

    idx = str(Path(target).with_suffix(".idx"))
    Ways().apply_file(str(source), locations=True, idx=f"sparse_file_array,{idx}")
    root = ET.Element("osm", version="0.6", generator="BeamNG.Earth roadintel")
    missing = set(needed)

    class Nodes(osmium.SimpleHandler):
        def node(self, n):
            if n.id in needed and n.location.valid():
                missing.discard(n.id)
                el = ET.SubElement(root, "node", id=str(n.id), lat=str(n.lat), lon=str(n.lon), version=str(n.version))
                for k, v in sorted(dict(n.tags).items()):
                    ET.SubElement(el, "tag", k=k, v=v)
    Nodes().apply_file(str(source))
    if missing:
        raise ValueError("extract has incomplete way node references")
    for wid, w in sorted(ways.items(), key=lambda x: int(x[0])):
        el = ET.SubElement(root, "way", id=wid, version=str(w["version"]))
        for nid in w["nodes"]:
            ET.SubElement(el, "nd", ref=str(nid))
        for k, v in sorted(w["tags"].items()):
            ET.SubElement(el, "tag", k=k, v=v)
    kept, incomplete = 0, 0
    for rid, tags, members in sorted(relations):
        way_members = [str(ref) for typ, ref, _ in members if typ == "w"]
        if not any(wid in ways for wid in way_members):
            continue
        if not all(wid in ways for wid in way_members) or any(typ == "n" and ref not in needed for typ, ref, _ in members):
            incomplete += 1
            continue
        el = ET.SubElement(root, "relation", id=str(rid))
        for typ, ref, role in members:
            ET.SubElement(el, "member", type={"w": "way", "n": "node", "r": "relation"}[typ], ref=str(ref), role=role)
        for k, v in sorted(tags.items()):
            ET.SubElement(el, "tag", k=k, v=v)
        kept += 1
    ET.ElementTree(root).write(target, encoding="utf-8", xml_declaration=True)
    return ways, {"restrictionRelations": kept, "incompleteRestrictions": incomplete}


def convert(osm, output, left_hand=False, timeout=90, runner=subprocess.run):
    cmd = [str(executable()), "--osm-files", str(osm), "--output-file", str(output),
           "--osm.turn-lanes", "true", "--osm.lane-access", "true", "--osm.all-attributes", "true",
           "--geometry.remove", "false", "--junctions.join", "false", "--roundabouts.guess", "false",
           "--no-turnarounds", "true", "--lefthand", str(bool(left_hand)).lower(), "--no-warnings", "false"]
    # File logs bound pipe RAM; subprocess has no shell and an explicit deadline.
    with tempfile.TemporaryFile() as log:
        try:
            result = runner(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False,
                            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"netconvert timed out after {timeout}s") from exc
        log.seek(0)
        report = log.read(32768).decode("utf-8", "replace")
    if result.returncode or not Path(output).is_file():
        raise ValueError(f"netconvert failed ({result.returncode}): {report[-4000:]}")
    if Path(output).stat().st_size > MAX_OUTPUT:
        raise ValueError("netconvert output exceeds region budget")
    return report


def read_network(path, ways):
    import sumolib
    net = sumolib.net.readNet(str(path), withInternal=False)
    edges = {}
    for edge in sorted(net.getEdges(), key=lambda x: x.getID()):
        eid = edge.getID()
        wid = eid.lstrip("-").split("#")[0]
        if wid not in ways:
            continue
        lanes = [lane for lane in edge.getLanes() if lane.allows("passenger")]
        if not lanes:
            continue
        edges[eid] = {"way": wid, "from": edge.getFromNode().getID(), "to": edge.getToNode().getID(),
                      "shape": [net.convertXY2LonLat(*p[:2]) for p in edge.getRawShape()],
                      "lanes": len(lanes), "speed": edge.getSpeed(), "priority": edge.getPriority()}
    connections = []
    for eid in sorted(edges):
        edge = net.getEdge(eid)
        for dest, conns in edge.getOutgoing().items():
            if dest.getID() not in edges:
                continue
            for c in conns:
                if c.getFromLane().allows("passenger") and c.getToLane().allows("passenger"):
                    connections.append({"from": eid, "to": dest.getID(), "fromLane": c.getFromLane().getIndex(),
                                        "toLane": c.getToLane().getIndex(), "dir": c.getDirection(), "state": c.getState()})
    if not edges:
        raise ValueError("netconvert produced no traceable passenger edges")
    return edges, sorted(connections, key=lambda c: (c["from"], c["to"], c["fromLane"], c["toLane"]))


def preprocess(source, directory, lat, lon, *, left_hand=False, timeout=90):
    key, core = region(lat, lon)
    bounds = (core[0] - HALO_DEG, core[1] - HALO_DEG, core[2] + HALO_DEG, core[3] + HALO_DEG)
    identity = {"version": VERSION, "sumo": "1.28.0", "sourceSha256": digest(source), "core": core,
                "halo": HALO_DEG, "leftHand": left_hand, "join": False}
    revision = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"region_{key}_{revision}.json"
    dest = directory / name
    if dest.is_file():
        return dest, True
    with tempfile.TemporaryDirectory(prefix="roadintel-") as work:
        osm, output = Path(work) / "region.osm.xml", Path(work) / "region.net.xml"
        ways, diagnostics = extract(source, bounds, osm)
        report = convert(osm, output, left_hand, timeout)
        edges, connections = read_network(output, ways)
        data = dict(identity, revision=revision, ways=ways, edges=edges, connections=connections,
                    diagnostics=dict(diagnostics, netconvertLog=report))
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > MAX_OUTPUT:
            raise ValueError("region cache exceeds budget")
        tmp = directory / (name + f".{os.getpid()}.tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, dest)
    # One atomic pointer per deterministic core. Old content-addressed revisions can be pruned offline.
    pointer = directory / f"region_{key}.ref"
    tmp = pointer.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(name, encoding="utf-8")
    os.replace(tmp, pointer)
    return dest, False
