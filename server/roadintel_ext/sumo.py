"""Offline native OSM importer. Never invoked by a road-tile build."""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import xml.sax
import multiprocessing

from . import VERSION

MAX_WAYS = 30000
MAX_NODES = 250000
MAX_SOURCE_NODES = 10000000
MAX_OUTPUT = 32 * 1024 * 1024
REGION_DEG = 0.05
HALO_DEG = 0.02


def _rss(pid):
    """Working-set bound without adding a runtime monitoring dependency."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (n, ctypes.c_size_t) for n in ("peak", "working", "peakPaged", "paged", "peakNonpaged", "nonpaged", "pagefile", "peakPagefile")]
        open_process = ctypes.windll.kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        close = ctypes.windll.kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        handle = open_process(0x0400 | 0x0010, False, pid)
        if not handle:
            return 0
        try:
            c = Counters()
            c.cb = ctypes.sizeof(c)
            info = ctypes.windll.psapi.GetProcessMemoryInfo
            info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            info.restype = wintypes.BOOL
            return c.working if info(handle, ctypes.byref(c), c.cb) else 0
        finally:
            close(handle)
    try:
        return int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return 0


def _run_bounded(cmd, *, stdout, stderr, timeout, check=False, **kwargs):
    process = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, **kwargs)
    deadline = time.monotonic() + timeout
    work = Path(cmd[cmd.index("--output-file") + 1]).parent
    try:
        while process.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd, timeout)
            if _rss(process.pid) > 2 * 1024**3:
                raise ValueError("netconvert exceeded 2 GiB memory budget")
            if os.fstat(stdout.fileno()).st_size > 16 * 1024**2:
                raise ValueError("netconvert exceeded diagnostic log budget")
            if sum(p.stat().st_size for p in work.iterdir() if p.is_file()) > 512 * 1024**2:
                raise ValueError("netconvert exceeded 512 MiB temporary disk budget")
            time.sleep(.05)
        return process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


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
        seen_nodes = 0

        def node(self, n):
            self.seen_nodes += 1
            if self.seen_nodes > MAX_SOURCE_NODES:
                raise ValueError("extract exceeds node budget; use a smaller complete extract")

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
    # Own the index and Reader lifetimes: SimpleHandler.apply_file's implicit
    # location handler can retain the mmap until GC on Windows.
    handler = Ways()
    location_index = osmium.index.create_map(f"sparse_file_array,{idx}")
    locations = osmium.NodeLocationsForWays(location_index)
    locations.ignore_errors()
    with osmium.io.Reader(str(source)) as reader:
        osmium.apply(reader, locations, handler)
    del locations, location_index, handler
    import gc
    gc.collect()
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
    kept, incomplete, conditional = 0, 0, 0
    for rid, tags, members in sorted(relations):
        way_members = [str(ref) for typ, ref, _ in members if typ == "w"]
        if not any(wid in ways for wid in way_members):
            continue
        if not all(wid in ways for wid in way_members) or any(typ == "r" or (typ == "n" and ref not in needed) for typ, ref, _ in members):
            incomplete += 1
            continue
        if any("conditional" in k for k in tags):
            conditional += 1
        el = ET.SubElement(root, "relation", id=str(rid))
        for typ, ref, role in members:
            ET.SubElement(el, "member", type={"w": "way", "n": "node", "r": "relation"}[typ], ref=str(ref), role=role)
        for k, v in sorted(tags.items()):
            ET.SubElement(el, "tag", k=k, v=v)
        kept += 1
    ET.ElementTree(root).write(target, encoding="utf-8", xml_declaration=True)
    return ways, {"restrictionRelations": kept, "incompleteRestrictions": incomplete, "conditionalRestrictions": conditional}


def convert(osm, output, left_hand=False, timeout=90, runner=None):
    runner = runner or _run_bounded
    cmd = [str(executable()), "--osm-files", str(osm), "--output-file", str(output),
           "--plain-output-prefix", str(output) + ".plain", "--proj.plain-geo", "true",
           "--osm.turn-lanes", "true", "--osm.lane-access", "true", "--osm.all-attributes", "true",
           "--geometry.remove", "false", "--junctions.join", "false", "--roundabouts.guess", "false",
           "--osm.skip-duplicates-check", "true",
           "--junctions.join-same", "-1",
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
    try:
        return _read_network(path, ways)
    except (KeyError, TypeError, ET.ParseError, xml.sax.SAXParseException) as exc:
        raise ValueError(f"invalid SUMO network output: {type(exc).__name__}: {exc}") from exc


def _read_network(path, ways):
    import sumolib
    net = sumolib.net.readNet(str(path), withInternal=False)
    prefix = str(path) + ".plain"
    points = {el.attrib["id"]: (float(el.attrib["x"]), float(el.attrib["y"]))
              for el in ET.parse(prefix + ".nod.xml").getroot().findall("node")}
    geo_edges = {}
    for el in ET.parse(prefix + ".edg.xml").getroot().findall("edge"):
        a = el.attrib
        geo_edges[a["id"]] = ([tuple(map(float, p.split(",")[:2])) for p in a["shape"].split()]
                              if a.get("shape") else [points[a["from"]], points[a["to"]]])
    edges = {}
    for edge in sorted(net.getEdges(), key=lambda x: x.getID()):
        eid = edge.getID()
        wid = eid.lstrip("-").split("#")[0]
        if wid not in ways:
            continue
        lanes = [lane for lane in edge.getLanes() if lane.allows("passenger")]
        if not lanes:
            continue
        # SUMO can merge coincident OSM nodes even with junction joining disabled.
        # Recover the original node references and reject such cross-level turns.
        original = ways[wid]
        def source_node(point):
            distances = [math.hypot((q[0] - point[0]) * math.cos(math.radians(point[1])), q[1] - point[1]) * 111320
                         for q in original["shape"]]
            best = min(distances)
            ids = {str(original["nodes"][j]) for j, d in enumerate(distances) if d <= best + 0.01}
            if best > 0.5 or len(ids) != 1:
                raise ValueError("SUMO edge endpoint cannot be traced uniquely to an OSM node")
            return ids.pop()
        shape = geo_edges[eid]
        edges[eid] = {"way": wid, "from": source_node(shape[0]), "to": source_node(shape[-1]),
                      "nativeFrom": edge.getFromNode().getID(), "nativeTo": edge.getToNode().getID(),
                      "shape": geo_edges[eid],
                      "lanes": len(lanes), "speed": edge.getSpeed(), "priority": edge.getPriority()}
    connections = []
    for eid in sorted(edges):
        edge = net.getEdge(eid)
        for dest, conns in edge.getOutgoing().items():
            if dest.getID() not in edges:
                continue
            if edges[eid]["to"] != edges[dest.getID()]["from"]:
                continue
            for c in conns:
                if c.getFromLane().allows("passenger") and c.getToLane().allows("passenger"):
                    connections.append({"from": eid, "to": dest.getID(), "fromLane": c.getFromLane().getIndex(),
                                        "toLane": c.getToLane().getIndex(), "dir": c.getDirection(), "state": c.getState()})
    if not edges:
        raise ValueError("netconvert produced no traceable passenger edges")
    return edges, sorted(connections, key=lambda c: (c["from"], c["to"], c["fromLane"], c["toLane"]))


def _extract_worker(source, bounds, osm, result):
    try:
        ways, diagnostics = extract(source, bounds, osm)
        Path(result).write_text(json.dumps({"ways": ways, "diagnostics": diagnostics}), encoding="utf-8")
    except Exception as exc:
        Path(result).write_text(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), encoding="utf-8")


def _write_pointer(directory, key, dest):
    pointer = directory / f"region_{key}.ref"
    tmp = pointer.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(dest.name + " " + digest(dest), encoding="utf-8")
    os.replace(tmp, pointer)


def preprocess(source, directory, lat, lon, *, left_hand=False, timeout=90):
    from importlib.metadata import version
    for package, expected in (("eclipse-sumo", "1.28.0"), ("sumolib", "1.28.0"), ("osmium", "4.3.2")):
        if version(package) != expected:
            raise ValueError(f"{package} must be {expected}; use requirements-roadintel-ext.txt")
    if Path(source).stat().st_size > 512 * 1024 * 1024:
        raise ValueError("extract exceeds 512 MiB input budget; supply a smaller extract")
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
        _write_pointer(directory, key, dest)
        return dest, True
    if sum(p.stat().st_size for p in directory.glob("*.json")) + MAX_OUTPUT > 2 * 1024**3:
        raise ValueError("road intelligence disk budget (2 GiB) reached; prune unused regions while server is stopped")
    with tempfile.TemporaryDirectory(prefix="roadintel-") as work:
        osm, output = Path(work) / "region.osm.xml", Path(work) / "region.net.xml"
        # Process lifetime closes libosmium's file-backed index on Windows too.
        # One bounded worker, not a persistent runtime/tile conversion service.
        result = Path(work) / "extract.json"
        worker = multiprocessing.get_context("spawn").Process(target=_extract_worker, args=(source, bounds, osm, result))
        worker.start()
        deadline = time.monotonic() + timeout
        exceeded = None
        while worker.is_alive() and time.monotonic() < deadline:
            if _rss(worker.pid) > 768 * 1024**2:
                exceeded = "OSM extraction exceeded 768 MiB memory budget"
                break
            if sum(p.stat().st_size for p in Path(work).iterdir() if p.is_file()) > 512 * 1024**2:
                exceeded = "OSM extraction exceeded 512 MiB temporary disk budget"
                break
            worker.join(.05)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
            if worker.is_alive():
                worker.kill()
                worker.join()
            raise ValueError(exceeded or f"OSM extraction timed out after {timeout}s")
        if worker.exitcode or not result.is_file():
            raise ValueError(f"OSM extraction failed ({worker.exitcode})")
        if result.stat().st_size > MAX_OUTPUT:
            raise ValueError("extracted full-tag payload exceeds region memory budget")
        payload = json.loads(result.read_text(encoding="utf-8"))
        if "error" in payload:
            raise ValueError(payload["error"])
        ways, diagnostics = payload["ways"], payload["diagnostics"]
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
    _write_pointer(directory, key, dest)
    return dest, False
