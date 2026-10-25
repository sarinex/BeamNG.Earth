"""Immutable regional snapshots and anchor-scoped adapters into existing producers."""
from collections import OrderedDict
from dataclasses import replace
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from functools import wraps
import weakref

import numpy as np

from . import VERSION
from .sumo import MAX_OUTPUT, region

_datasets = weakref.WeakValueDictionary()


def open_dataset(directory):
    candidate = Dataset(directory)
    key = (str(candidate.directory), candidate.identity)
    previous = _datasets.get(key)
    if previous is not None:
        return previous
    _datasets[key] = candidate
    return candidate


def project(points, line):
    """Distances and normalized arc positions; local metric, stable under world rebases."""
    points, line = np.asarray(points, float), np.asarray(line, float)
    scale = np.array([111320 * math.cos(math.radians(float(line[0, 1]))), 111320])
    q, p = (points - line[0]) * scale, (line - line[0]) * scale
    d = np.diff(p, axis=0)
    lengths = np.hypot(d[:, 0], d[:, 1])
    arcs = np.r_[0, np.cumsum(lengths)]
    t = np.clip(((q[:, None] - p[None, :-1]) * d[None]).sum(2) /
                np.maximum((d * d).sum(1), 1e-12), 0, 1)
    dist = np.linalg.norm(q[:, None] - (p[None, :-1] + t[..., None] * d[None]), axis=2)
    k = dist.argmin(1)
    return dist[np.arange(len(q)), k], (arcs[k] + t[np.arange(len(q)), k] * lengths[k]) / max(arcs[-1], 1e-9)


class Dataset:
    """Pin .ref pointers at selection time; updates require an explicit new selection.

    At most four region payloads in memory. Missing coverage is explicit per-feature fallback.
    No SUMO import or network access is performed here.
    """
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.refs = {}
        for path in sorted(self.directory.glob("region_*.ref")):
            content = path.read_text(encoding="utf-8").split()
            if len(content) != 2:
                raise ValueError("invalid road intelligence cache pointer")
            name, checksum = content
            if Path(name).name != name or not name.endswith(".json"):
                raise ValueError("invalid road intelligence cache pointer")
            self.refs[path.stem[7:]] = (name, checksum)
        if not self.refs:
            raise ValueError("No prepared regions; run tools/prepare_roadintel.py and set BNGE_ROADINTEL_DIR")
        self.identity = hashlib.sha256(json.dumps(self.refs, sort_keys=True).encode()).hexdigest()
        self.loaded, self.lock = OrderedDict(), threading.RLock()
        self.stats = {"hits": 0, "misses": 0, "matched": 0, "fallbacks": 0}

    def get(self, lat, lon):
        key, _ = region(lat, lon)
        with self.lock:
            if key in self.loaded:
                self.stats["hits"] += 1
                self.loaded.move_to_end(key)
                return self.loaded[key]
            self.stats["misses"] += 1
            ref = self.refs.get(key)
            if not ref:
                return None
            name, checksum = ref
            path = self.directory / name
            if path.stat().st_size > MAX_OUTPUT:
                raise ValueError("region cache exceeds budget")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != checksum:
                raise ValueError("road intelligence cache checksum mismatch")
            data = json.loads(raw)
            if data.get("version") != VERSION or data.get("sumo") != "1.28.0" or name != f"region_{key}_{data.get('revision')}.json":
                raise ValueError("unsupported or mismatched road intelligence cache")
            self.loaded[key] = data
            while len(self.loaded) > 4:
                self.loaded.popitem(last=False)
            return data

    def match(self, feats, i, left_hand=False):
        import osmtags
        record = feats.attrs.get("src_record")
        wid = osmtags.way_id(str(record[i])) if record is not None else None
        if wid is None:
            raise ValueError("missing Overture-to-OSM way association")
        parts = feats.parts(i)
        if len(parts) != 1 or len(parts[0]) != 1 or len(parts[0][0]) < 2:
            raise ValueError("ambiguous multi-part feature")
        ll = np.asarray(parts[0][0], float)
        mid = ll[len(ll) // 2]
        data = self.get(float(mid[1]), float(mid[0]))
        if data is None or str(wid) not in data["ways"]:
            raise ValueError("no full-tag extract coverage")
        if bool(data["leftHand"]) != bool(left_hand):
            raise ValueError("extract traffic handedness disagrees with country rules")
        source = data["ways"][str(wid)]
        dist, at = project(ll, source["shape"])
        source_line = np.asarray(source["shape"], float)
        closed = np.allclose(ll[0], ll[-1], atol=1e-10, rtol=0) and np.allclose(source_line[0], source_line[-1], atol=1e-10, rtol=0)
        if closed:
            def area(line):
                p = line - line[0]
                return float(np.sum(p[:-1, 0] * p[1:, 1] - p[1:, 0] * p[:-1, 1]))
            reverse = area(ll) * area(source_line) < 0
            at = np.unwrap(at * 2 * np.pi) / (2 * np.pi)
            at[-1] = at[0] + (-1 if reverse else 1)
        if float(dist.max()) > 6 or abs(at[-1] - at[0]) < 1e-5:
            raise ValueError("OSM way geometry does not match Overture feature")
        reverse = at[-1] < at[0]
        if np.any(np.diff(at) * (-1 if reverse else 1) < -0.01):
            raise ValueError("non-monotonic OSM/Overture correspondence")
        tags = source["tags"]
        if tags.get("placement", "center") not in ("center", ""):
            raise ValueError("mapped placement requires an independently validated geometry correction")
        return {"way": str(wid), "tags": tags, "reverse": bool(reverse), "region": data,
                "provenance": f"OSM w{wid}@{source['version']} / {data['sourceSha256']}"}


class MapView:
    """Copy only feature containers, never mutate upstream cached cells or geometry."""
    def __init__(self, base, dataset, left_hand=False):
        self.base, self.dataset, self.left_hand = base, dataset, bool(left_hand)
        self.memo, self.lock = OrderedDict(), threading.RLock()

    def __getattr__(self, name):
        return getattr(self.base, name)

    def cell(self, *args, **kwargs):
        cd = self.base.cell(*args, **kwargs)
        f = cd.layers.get("roads")
        if f is None:
            return cd
        key = id(f)
        with self.lock:
            cached = self.memo.get(key)
            if cached is None or cached[0] is not f:
                clone = replace(f, attrs=dict(f.attrs), rules=dict(f.rules))
                clone._roadintel = []
                for i in range(len(f)):
                    try:
                        rec = self.dataset.match(f, i, self.left_hand)
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        rec = {"error": str(exc)}
                    clone._roadintel.append(rec)
                cached = self.memo[key] = (f, clone)
                while len(self.memo) > 32:
                    self.memo.popitem(last=False)
            self.memo.move_to_end(key)
        result = copy.copy(cd)
        result.layers = dict(cd.layers, roads=cached[1])
        return result


def effective_specs(feats, specs):
    records = getattr(feats, "_roadintel", None)
    if records is None:
        return specs
    from .model import normalize
    out = list(specs)
    for i, (spec, record) in enumerate(zip(specs, records)):
        if spec is None or "error" in record:
            continue
        try:
            section = normalize(record["tags"], spec, reverse=record["reverse"],
                                left_hand=record["region"]["leftHand"], provenance=record["provenance"])
            import roads
            if spec.median and section.envelope_width > roads.dual_cap(spec.median, section.envelope_width) + .05:
                raise ValueError("mapped section overlaps the adjacent carriageway; source geometry/width conflict")
            for eid, native in record["region"].get("edges", {}).items():
                if native["way"] != record.get("way"):
                    continue
                forward = not eid.startswith("-")
                if record["reverse"]:
                    forward = not forward
                expected = section.forward if forward else section.backward
                if native["lanes"] != expected:
                    raise ValueError("SUMO passenger lanes disagree with mapped section (reserved lanes or lane transition)")
            from .navigation import endpoint
            ll = np.asarray(feats.parts(i)[0][0], float)
            glyphs = {"left": "l", "through": "s", "right": "r"}
            def arrow_ok(turns, end, adjacent):
                if not turns:
                    return False
                diagnostic = record["region"].get("diagnostics", {})
                if diagnostic.get("incompleteRestrictions") or diagnostic.get("conditionalRestrictions"):
                    return False
                try:
                    eid, _ = endpoint(record, ll[end], True, ll[adjacent])
                except (ValueError, KeyError):
                    return False
                allowed = {c["dir"] for c in record["region"].get("connections", []) if c["from"] == eid}
                return all(glyphs.get(m) in allowed for lane in turns for m in lane)
            section = replace(section, arrows_forward=arrow_ok(section.turns_forward, -1, -2),
                              arrows_backward=arrow_ok(section.turns_backward, 0, 1))
            raw_speed = record["tags"].get("maxspeed", "")
            try:
                speed = float(raw_speed[:-3].strip()) * 1.609344 if raw_speed.endswith("mph") else float(raw_speed)
            except ValueError:
                speed = spec.kmh
            surface = record["tags"].get("surface")
            out[i] = spec._replace(width=section.envelope_width, lanes=section.forward + section.backward,
                                  oneway=section.direction != 0, dir=section.direction, intel=section,
                                  kmh=speed if speed is not None and 5 <= speed <= 200 else spec.kmh,
                                  kind=roads.surface_kind(spec.cls, surface) if surface else spec.kind,
                                  surf=roads.surface_token(surface) if surface else spec.surf)
        except ValueError as exc:
            # This clone belongs to this immutable profile; store diagnostics, not guesses.
            record["layoutFallback"] = str(exc)
    return out


def diagnostics(cells, specs_by_id):
    sections, fallbacks = {}, {}
    for feats in cells:
        records = getattr(feats, "_roadintel", None)
        if records is None:
            continue
        for i, record in enumerate(records):
            fid = str(feats.ids[i])
            if fid not in specs_by_id:
                continue
            spec = specs_by_id[fid]
            if spec.intel is not None:
                sections[fid] = spec.intel.diagnostic()
            else:
                fallbacks[fid] = record.get("error", record.get("layoutFallback", "unsupported section"))
    return {"backend": "sumo", "sections": sections, "fallbacks": fallbacks}


def bind_anchor(server, anchor, dataset):
    """Fresh anchor identity invalidates terrain, masks, meshes, plans and AI together.

    The view uses upstream thread builders for the experimental profile; unmodified
    process pools do not receive mutable flags or accidentally build legacy terrain.
    All raw network/cache services and bounded worker pools remain shared.
    """
    if dataset is None:
        return
    view = copy.copy(server)
    view._roadintel_root = server
    maps = getattr(server, "_roadintel_maps", None)
    if maps is None:
        maps = server._roadintel_maps = weakref.WeakValueDictionary()
    key = (dataset.identity, bool(server.left_hand(anchor)))
    view.mapdata = maps.get(key)
    if view.mapdata is None:
        view.mapdata = maps[key] = MapView(server.mapdata, dataset, key[1])
    view.block_pools, view.road_pool, view.far_pool, view.far_bld_pool = [], None, None, None
    view.surface = copy.copy(server.surface) if server.surface is not None else None
    if view.surface is not None:
        view.surface.md = view.mapdata
    anchor.roadintel_view = view
    anchor.roadintel_identity = dataset.identity


def server_for(server, anchor):
    return getattr(anchor, "roadintel_view", getattr(server, "_roadintel_root", server))


def routed(fn):
    """Small upstream hook: select immutable anchor inputs before any cached work."""
    @wraps(fn)
    def call(server, anchor_or_id, *args, **kwargs):
        anchor = server.anchors.get(anchor_or_id) if isinstance(anchor_or_id, int) else anchor_or_id
        view = server_for(server, anchor) if anchor is not None else server
        return fn(view, anchor_or_id, *args, **kwargs)
    return call
