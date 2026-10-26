# Group 4 road intelligence (experimental)

Implementation starts from the user's working Group 1 commit `e3b1dab`. Legacy geometry, GEOS/Earcut, their dependency files, and the Legacy intelligence default are preserved. No BeamNG.drive session was launched, controlled, or automated during this implementation. **Every in-game result is NOT YET VALIDATED.** The single user checklist is [ROADINTEL_EVALUATION.md](ROADINTEL_EVALUATION.md).

## Reproducible installation

Use the repository environment, not a system Python. Group 1's `requirements-roadgeom-ext.txt` and the original `requirements.txt` are unchanged. Road intelligence preprocessing is an explicit optional installation:

```powershell
.venv/Scripts/python.exe -m pip install -r server/requirements-roadintel-ext.txt
.venv/Scripts/python.exe -c "import sumo; from pathlib import Path; print(Path(sumo.SUMO_HOME) / 'bin' / 'netconvert.exe')"
```

Linux equivalents:

```sh
.venv/bin/python -m pip install -r server/requirements-roadintel-ext.txt
.venv/bin/python -c "import sumo; from pathlib import Path; print(Path(sumo.SUMO_HOME) / 'bin' / 'netconvert')"
```

The pins are `eclipse-sumo==1.28.0`, `sumolib==1.28.0`, and `osmium==4.3.2`. The first package supplies SUMO and `netconvert`; preprocessing locates it through `sumo.SUMO_HOME`, never PATH. The prepared Windows environment has these versions installed already. Preprocessing checks the versions. Normal server operation reads prepared JSON with the standard library and NumPy and needs no running SUMO simulator. A source checkout is required for the preparation CLI; this change does not bundle the large optional SUMO distribution into the existing frozen release.

Supply an appropriately scoped, complete OSM `.osm.pbf` or `.osm.xml` extract. For example, obtain a current regional extract from [Geofabrik](https://download.geofabrik.de/) and retain the download date, license, and SHA-256. Large country extracts may need an offline complete-ways crop first. The importer rejects oversized inputs with a diagnostic; it never silently downloads a larger region. A clipped file must retain member nodes and restriction relations. OSM attribution/ODbL obligations apply to retained or redistributed extracts and derived datasets.

```powershell
# Both requested Baltimore sites are in this same deterministic core.
.venv/Scripts/python.exe tools/prepare_roadintel.py D:/OSM/baltimore.osm.pbf --out build/roadintel-cache --lat 39.28188 --lon -76.59114
$env:BNGE_ROADINTEL_DIR = (Resolve-Path build/roadintel-cache).Path
# Start the companion server normally from this shell; launch the game yourself later.
```

```sh
.venv/bin/python tools/prepare_roadintel.py /data/baltimore.osm.pbf --out build/roadintel-cache --lat 39.28188 --lon -76.59114
export BNGE_ROADINTEL_DIR="$PWD/build/roadintel-cache"
```

Repeat preparation for cores covering the intended route; use `--left-hand` for Japan and other left-driving regions. For example, prepare Shibuya with `--lat 35.6595 --lon 139.7005 --left-hand`. Keep the exact input extract and generated `.ref`/`.json` files to reproduce a run on another machine. `--timeout` is 90 seconds per extraction/conversion stage by default, bounded to 1–300 seconds by the CLI. Preparation runs outside the game/server in a single offline job with a separate extraction process. It is never called from a tile build. No Overpass or runtime tile query API is used.

Selecting SUMO requires the server's roads and terrain surfaces to be enabled. Selection without prepared regions is rejected and preserves the previous accepted backend. Missing coverage, bad checksums, mismatched driving side, incomplete lane counts or ambiguous correspondence fall back per feature/junction and are reported. Legacy selection does not scan or load the regional dataset. A missing preprocessing executable produces an explicit error; existing prepared data can still be used without installing SUMO on a runtime-only server.

## Audit of the existing system

| Existing component | Existing responsibility and assumptions | Extension boundary |
| --- | --- | --- |
| `roads.py` | Class-based pavement widths, Layercake total lanes/width, building-measured streets, inferred paired carriageways, parking/shoulders, equally split lane markings | One optional `Spec.intel` and final spec adapter; mapped cross-sections precede every consumer |
| `roadwidth.py` | Memoized building-to-building widths and sidewalk split | Unchanged fallback; mapped intelligence supersedes an estimate only in its own anchor |
| `osmtags.py` | ETag-pinned Layercake range reads; parsed total lanes, width, sidewalk, speed, surface, one-way and layer | Unchanged Legacy source; richer tags are read from explicit full-tag extracts |
| `overture.py` / `mapdata.py` | Full Overture feature geometry, source way association, linear-referenced connectors, level/access/speed rules, raw and decoded caches | Reused through `MapView`; cached upstream features are never mutated |
| `junctions.py` | Existing corner, sidewalk, crosswalk/stop-line generation and marking cuts from carriageway widths | Unchanged algorithm receives effective widths. Inferred parking bulb-outs are disabled for enriched sections |
| `roadgeom_ext/` | Optional GEOS pavement union and Earcut export, legacy fallback, safe epochs | Interfaces retained; consumes the same effective Specs as original geometry |
| `surface.py` / `grading.py` | Imagery paint, terrain friction, graded profiles, bridge/tunnel holes | Receive the same immutable feature view as road tiles through a fresh anchor |
| `navgraph.py` | Existing Overture connectors, equal lane split, class widths, speed profiles, inferred rings, signals/stops | Enriched directional counts/driving width/offset, verified SUMO movement portals; existing BeamNG AI retained |
| Lua `navgraph.lua` | Stock custom-map import, lane strings, road rules, traffic signals, vehicle refresh and GPS | Epoch gate and loaded-graph settlement; existing signal positions/controllers refer to new approach portals |
| Lua settings / streaming | Persisted geometry choice, safety probes, stationary hold, epochs, pooled shape retirement, timeout/recovery | Independent intelligence preference uses this same transaction; terrain and AI are completion barriers |

The two requested research notes were reviewed. Overture connector IDs and fractions already reach the decoded feature model and remain the spatial/topological authority. `prohibited_transitions` exists in the [Overture segment schema](https://docs.overturemaps.org/schema/reference/transportation/segment/), but the current `overture.LAYERS['roads']` projection/decoder does not retain it. Using it would require a separately versioned projection/decoder change and resolution of conditional/multi-segment restrictions. This implementation obtains turn restrictions from complete OSM relations instead of pretending that the current arrays contain Overture restrictions. An OSM way can correspond to many Overture segments and many SUMO edges; equality of those objects is never assumed.

## Data/model and SUMO tradeoff

The effective model combines existing immutable Features (stable GERS IDs, source way IDs, connectors, full centerline, levels, surfaces) with a normalized CrossSection and a pinned regional SUMO network (stable native edge/node IDs, priorities, passenger lane counts and lane-to-lane connections). CrossSection distinguishes mapped counts/widths from estimated lane widths, parking uncertainty and inferred directional splits. Odd totals without directional information, contradictory totals, unsupported shared center lanes, conditional/reserved-lane ambiguity and non-center placement are explicit fallbacks. Widths are metres; supported speed strings include km/h numeric values and mph. Original bridge/tunnel level ranges and their elevation builders remain in charge.

Full tags are retained, including forward/backward/both-ways lanes, turn lanes, widths, placement, oneway, roundabouts, access, bus/cycle lanes, parking, sidewalks and speed. Not every retained tag is implemented: diagnostics and the limitations below distinguish supported layouts from retained evidence. Missing parking mapping does not create a mapped parking fact. Class lane widths and even-total directional splits are explicitly labelled estimates.

Each copied feature records one canonical Legacy baseline per ground/bridge/tunnel layer, using the same urban and measured-width inputs as the server. Every consumer normalizes against that baseline. Without a mapped pavement width, additional existing pavement margins remain explicitly estimated non-driving space. This prevents a narrower mesh or AI layout when a caller lacks painting's urban context. The AI samples bridge elevation at the authoritative centerline before applying a lateral driving-center offset.

Native OSM import was chosen because complete ways/nodes/relations let [netconvert's OSM importer](https://sumo.dlr.de/docs/Networks/Import/OpenStreetMap.html) interpret turning lanes, restrictions and roundabouts directly. Plain XML import of Overture centerlines would require reconstructing relation matching and lane connectivity ourselves. [Plain XML export](https://sumo.dlr.de/docs/Networks/PlainXML.html) supplies geographic reference shapes without adding a hidden `pyproj` dependency; it is not used to replace Overture geometry.

Options enable OSM turn lanes/lane access and explicit traffic handedness. Junction joining, geometry-node removal and guessed roundabouts are disabled to preserve traceability. Turning arrows are parsed according to [OSM turn-lane notation](https://wiki.openstreetmap.org/wiki/Key:turn:lanes). Only supported simple glyphs at verified native approaches are drawn, using generated ribbons with the existing stock `line_white` material. No new external asset is required.

Matching first requires the existing Overture OSM way association, then bounded geometric distance (6 m), monotonic arc correspondence and unambiguous direction. Closed rings use orientation and unwrapped arc position. Native endpoints are independently traced back to original OSM node references: SUMO can merge coincident nodes, so cross-level transitions between distinct OSM references are rejected. Each junction is replaced only when all incident arms resolve to one source node and one region revision. Partial/ambiguous junctions retain original connectivity with a reason. Source turn restrictions genuinely change the exported BeamNG graph; this is not an unused `.net.xml` output.

## Terrain and switching contract

All four combinations are supported and covered offline: Legacy/GEOS geometry × Legacy/SUMO intelligence. Both defaults remain Legacy. `Road Intelligence` is a separate persisted setting beside `Road Geometry Generator`.

An intelligence change is accepted under the existing stationary, single-player, terrain-supported switch hold. The server increments the existing road epoch, closes road/nav trackers, and waits for a **fresh anchor at the identical geographic origin**. That anchor captures the immutable dataset identity and owns fresh grading plans, terrain chunks, bridge/tunnel profiles and output names. The client imports its terrain/masks/collision through the existing identity rebase path without moving the vehicle. Existing far tiles retire through the usual streaming mechanism. A completed switch requires current terrain, roads/collision, retired far tiles and an actually applied current AI graph. The UI distinguishes desired, accepted, loaded, pending and incomplete states.

Anchor-specific server views route the original terrain, base-photo, far-tile, grading, road and AI producers to the same MapView. SUMO anchors currently use the existing bounded **thread** builders rather than unmodified process workers; Legacy retains its existing process path. This avoids mutable worker flags and profile-contaminated caches. Raw network caches, DEMs, assets and worker budgets remain shared. Grading's persistent plan cache is disabled only for enriched anchors; their in-memory plans are anchor-specific. Repeated selections reuse equivalent immutable dataset/feature views through weak caches.

Delayed road/nav results are gated by epoch, closed trackers suppress announcements, and prepared terrain is gated by request ID, epoch and the live hold. Graph file serials include the epoch. Coordinated imports require complete usable surface masks/materials and never retry without them. A failed terrain import attempts to restore the prior terrain and collision before reporting incomplete. Ordinary rejection, failure or a 120-second timeout releases this operation's hold; it never claims a complete backend rollback. If terrain restoration itself fails, the simulation remains held with an explicit restoration-pending state, including across disconnects/timeouts. Choose Legacy to request a fresh recovery under that hold; a successful terrain import clears the block, and the full road/AI settlement releases the hold. Leaving the mission cancels it. Switching while driving or in multiplayer remains pending. Engine behavior of this transition is **NOT YET VALIDATED**.

## Bounds, invalidation and diagnostics

Regions have deterministic 0.05° cores with a 0.02° halo on every side. This is a geographic grid, not a fixed metric size; the supported preparation latitude is ±85°. Full ways and referenced nodes are retained across the halo. Prepare neighboring cores for long drives. A model is selected by the full feature's midpoint, consistently across tiles; very long features whose midpoint core is absent fall back.

Cache identity includes format version (currently **2**), input extract SHA-256, SUMO version, core, halo, handedness and import policy. Changing policy requires bumping `roadintel_ext.VERSION`; reprepare old regions after a version change. Atomic `.ref` pointers include a payload checksum; running selections pin those pointers. Updated extracts become visible on a new SUMO selection/rebuild or server restart, never halfway through an existing anchor. Overture/map-source versioning remains upstream's. Session outputs use fresh anchors and the existing geometry namespace/epoch; old files obey the original held-reference/prune lifecycle.

Budgets: one extraction/conversion job; ≤512 MiB input; ≤10 million input nodes; ≤30,000 retained ways/250,000 referenced nodes; extraction working set ≤768 MiB and temporary files ≤512 MiB; netconvert working set ≤2 GiB, temporary files ≤512 MiB and diagnostic log ≤16 MiB; payload ≤32 MiB; four loaded regional payloads and 32 copied feature containers per shared view; persistent region store ≤2 GiB (new work is refused when full). RSS/disk guards poll every 50 ms, so short transient overshoot is possible. These are extension limits, not a whole-server RSS quota. Prune unused regional versions only while the server is stopped; no automatic deletion can invalidate a running pinned selection. Generated world outputs continue to use upstream session/anchor pruning.

Native edges are indexed by source way and connections by incoming edge when a region loads, avoiding a full regional scan per feature or junction. Feature records retain lightweight region references instead of payload dictionaries, so retained feature containers do not defeat the four-region LRU limit.

Road tile JSON exposes `roadGeometry.roadIntelligence.sections` (counts, widths, offsets, provenance, confidence, notes) and `fallbacks` (feature ID → reason). Navgraph JSON exposes `roadIntelligence.sumoJunctions`, `fallbackJunctions`, `laneConnections`, and `laneTurnMasksEnforced=false`. The structured `beamngEarth_core.roadIntelligenceState()` includes navigation diagnostics; the settings line reports source/layout fallback counts. Dataset cache hits/misses and preparation timing are included in the offline benchmark.

## Limits requiring an honest comparison

- BeamNG's custom links express one-way direction, lane count/order, total driving radius, speed and drivability. They do **not** expose SUMO's individual lane-to-lane turn masks, signal conflict matrices or exact per-lane widths. Approach portals express road-level allowed movements; lane-specific enforcement and stock AI behavior remain unvalidated. Unequal mapped lane widths are visible, while the AI uses the total driving width and counts.
- SUMO major/minor connection state is translated to a conservative drivability bias on movement links. This is a routing hint, not an imported right-of-way or yield controller. Existing stop/signal controllers remain responsible for their supported behavior; their in-game obedience is NOT YET VALIDATED.
- Source widths change the entire coordinated terrain footprint. Unsupported placement corrections, shared both-ways lanes, conditional/reserved-lane layouts, incompatible native passenger counts and ambiguous matches fall back. Bus/cycle evidence is retained, not drawn as invented passenger lanes. Diagonal/perpendicular parking is currently represented by its mapped occupied width; stock parked-car placement remains conservative.
- Lane counts/widths can change between mapped features and SUMO supplies merge/split connectivity. Arbitrary painted taper lengths inside a feature are not invented. Complex merge/reverse/sharp arrow glyphs are omitted. Crosswalks and stop lines reuse the existing width-aware generator and controls; there is no new crossing-inference system.
- Incomplete or conditional restriction relations cause junction connectivity fallback for that region. Missing source data can therefore leave an entire experimental region on original topology; diagnostics identify it. No claim of turning-restriction enforcement is made for a fallback junction.
- This is an optional local-extract workflow, not a worldwide full-tag download service. No real-world screenshot, frame-time, streaming-latency or driving result has been collected. Windows was exercised here; Linux code paths and packaged distribution still require platform smoke tests.

## Automated checkpoints and reproduction

Checkpoint 4A introduced the shared layout/input adapter in commit `8f2427f`; the initial road/model/GEOS/nav regression run passed 100 tests. Checkpoint 4B added real native imports, restrictions, junction portals, cache validation and coordinated terrain routing in `29019c2`. Checkpoint 4C added the shared selector/terrain/AI completion gates and mapped markings in `34dee21`. Final validation fixes cover canonical layouts, indexed regional matching, offset bridge elevations and strict terrain/collision restoration. All engine checkpoints remain pending user validation.

```powershell
.venv/Scripts/python.exe tools/run_tests.py --par 4 --timeout 1500
.venv/Scripts/python.exe tools/run_lua_tests.py --runtime-dir build/lua-runtime
.venv/Scripts/python.exe tools/benchmark_roadintel.py --out build/roadintel-benchmark.json --repeats 7
```

The Lua runner can use standalone LuaJIT or the developer-only Lupa LuaJIT runtime already prepared under `build/lua-runtime`. To reproduce the latter, install with `.venv/Scripts/python.exe -m pip install --target build/lua-runtime lupa==2.8`; this is not a new runtime dependency of the mod. The full Python suite has documented platform/data/tool skips; `test_integration_drive` is a simulated client and can use network on a cold DEM cache. The Group 4 benchmark and tests use synthetic OSM/DEM/landcover and real netconvert, with no game or downloads. [roadintel-benchmark.json](roadintel-benchmark.json) contains the measurements; the final verification record is in [ROADINTEL_EVALUATION.md](ROADINTEL_EVALUATION.md).
