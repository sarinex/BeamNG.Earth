"""SUMO turn connectivity projected onto the existing BeamNG custom graph.

BeamNG links express directional lane counts, speed and drivability. They have no
lane-to-lane turn mask. Separate directed approach portals express road-level
turn restrictions; SUMO lane connections stay diagnostic. Upstream signal/stop
positions and controllers are retained, with graph references remapped to portals.
"""
import hashlib
import math
from types import SimpleNamespace

import navgraph


class Tracker(navgraph.Tracker):
    def __init__(self, backend="legacy", epoch=0):
        super().__init__()
        self.backend, self.epoch, self.closed = backend, epoch, False
        self.serial = epoch * 1000000

    def close(self):
        self.closed = True
        super().close()

    def _built(self, conn, *args):
        if self.closed:
            return
        def send(msg):
            if not self.closed:
                conn.send(dict(msg, roadBackend=self.backend, roadEpoch=self.epoch))
        super()._built(SimpleNamespace(send=send), *args)


def endpoint(record, point, incoming, other=None):
    """Unique native edge at a junction, matched by way id AND endpoint geometry."""
    from .runtime import project, native_edges
    candidates = []
    for eid, edge in native_edges(record).items():
        if edge["way"] != record["way"] or len(edge["shape"]) < 2:
            continue
        q = edge["shape"][-1 if incoming else 0]
        # Reuse metric projection against a tiny local line for distance only.
        scale = math.cos(math.radians(point[1]))
        d = math.hypot((q[0] - point[0]) * scale, q[1] - point[1]) * 111320
        on_arm = True
        if other is not None:
            distance, _ = project([other], edge["shape"])
            on_arm = float(distance[0]) <= 6
        if d <= 6 and on_arm:
            candidates.append((eid, edge))
    if len(candidates) != 1:
        raise ValueError("ambiguous SUMO junction-edge match")
    return candidates[0]


def apply(graph, frame, edge_records):
    """Only replace a junction if every incident arm matches one native SUMO node.

    Topology never joins roads by XY proximity. Overture connector IDs remain the
    authority, including different levels. No partial replacements at region edges.
    """
    nodes, edges = graph["nodes"], graph["edges"]
    junctions = {}
    for e in edges:
        for name in e[:2]:
            if name.startswith("c"):
                junctions.setdefault(name, []).append(e)
    controlled = {s["j"]: s for s in graph["signals"]}
    plans, diagnostics = {}, {"sumoJunctions": [], "fallbackJunctions": {}, "laneConnections": 0,
                              "laneTurnMasksEnforced": False}
    for junction, incident in sorted(junctions.items()):
        try:
            if len(incident) < 2:
                raise ValueError("incomplete junction context")
            la, lo, _ = frame.enu_to_geodetic(*nodes[junction][:3])
            arms, native_nodes, revisions = [], set(), set()
            for e in incident:
                record = edge_records.get(id(e))
                if record is None or "error" in record or record.get("layoutFallback"):
                    raise ValueError("unmatched or unsupported incident arm")
                for incoming in (True, False):
                    legal = (e[1] == junction if incoming else e[0] == junction) or not e[2]
                    if not legal:
                        continue
                    other = nodes[e[1] if e[0] == junction else e[0]]
                    ola, olo, _ = frame.enu_to_geodetic(*other[:3])
                    eid, native = endpoint(record, (float(lo), float(la)), incoming, (float(olo), float(ola)))
                    native_nodes.add(native["to" if incoming else "from"])
                    revisions.add(record["region"]["revision"])
                    token = hashlib.sha256(f"{junction}|{eid}|{incoming}".encode()).hexdigest()[:20]
                    arms.append((e, incoming, eid, "ri" + token, native, record["region"]))
            if len(native_nodes) != 1 or len(revisions) != 1:
                raise ValueError("incident arms disagree on SUMO junction or region revision")
            if len({(id(a[0]), a[1]) for a in arms}) != len(arms):
                raise ValueError("duplicate approach mapping")
            if len({(a[2], a[1]) for a in arms}) != len(arms):
                raise ValueError("multiple Overture arms match one SUMO edge ambiguously")
            if junction in controlled:
                for app in controlled[junction]["app"]:
                    if not any(a[1] and (a[0][1] if a[0][0] == junction else a[0][0]) == app[0] for a in arms):
                        raise ValueError("upstream signal approach cannot be matched to a SUMO portal")
            # Boundary restrictions may be incomplete: never claim these are authoritative.
            if any(a[5]["diagnostics"].get("incompleteRestrictions") or a[5]["diagnostics"].get("conditionalRestrictions") for a in arms):
                raise ValueError("extract contains incomplete or conditional turn restrictions")
            plans[junction] = arms
            diagnostics["sumoJunctions"].append(junction)
        except (ValueError, KeyError, TypeError) as exc:
            diagnostics["fallbackJunctions"][junction] = str(exc)
    replacement = {}
    turns = []
    for junction, arms in plans.items():
        origin = nodes[junction]
        for e, incoming, eid, name, native, data in arms:
            other = nodes[e[1] if e[0] == junction else e[0]]
            length = math.hypot(other[0] - origin[0], other[1] - origin[1])
            t = min(0.25, 5 / max(length, 1e-9))
            nodes[name] = [round(origin[k] + t * (other[k] - origin[k]), 3) for k in range(3)] + [e[7]]
            replacement[(id(e), junction, incoming)] = name
        from .runtime import native_connections
        conns = native_connections(arms[0][5], [a[2] for a in arms if a[1]])
        by_pair = {}
        for c in conns:
            by_pair.setdefault((c["from"], c["to"]), []).append(c)
        for src in arms:
            if not src[1]:
                continue
            for dst in arms:
                if dst[1] or (src[2], dst[2]) not in by_pair:
                    continue
                connections = by_pair[(src[2], dst[2])]
                diagnostics["laneConnections"] += len(connections)
                count = min(src[0][3] if src[0][1] == junction else src[0][4],
                            dst[0][3] if dst[0][0] == junction else dst[0][4])
                # SUMO state M/m identifies priority/yield. Stock AI uses drivability as priority.
                driv = min(src[0][6], dst[0][6], 1.0 if any(c["state"] == "M" for c in connections) else 0.8)
                turns.append([src[3], dst[3], 1, max(1, count), 0, min(src[0][5], dst[0][5], 8.0), driv,
                              min(src[0][7], dst[0][7])])
    result = []
    for e in edges:
        if not any(n in plans for n in e[:2]):
            result.append(e)
            continue
        for backwards in (False, True):
            if backwards and (e[2] or not e[4]):
                continue
            a, b = (e[1], e[0]) if backwards else (e[0], e[1])
            a = replacement.get((id(e), a, False), a)
            b = replacement.get((id(e), b, True), b)
            result.append([a, b, 1, e[4] if backwards else e[3], 0, *e[5:]])
    result.extend(turns)
    used = {n for e in result for n in e[:2]}
    graph["edges"], graph["nodes"] = result, {k: v for k, v in nodes.items() if k in used}
    for signal in graph["signals"]:
        arms = plans.get(signal["j"])
        if not arms:
            continue
        incoming = [a for a in arms if a[1]]
        signal["controlNode"] = incoming[0][3]
        for app in signal["app"]:
            app[0] = next(a[3] for a in incoming if (a[0][1] if a[0][0] == signal["j"] else a[0][0]) == app[0])
    graph["roadIntelligence"] = diagnostics
    return graph
