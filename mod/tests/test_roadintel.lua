-- Pure shared-state tests: no game is launched or controlled.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
package.loaded['beamngEarth/roadgeom'] = nil
local m = require('beamngEarth/roadgeom')
m.hello({version=1, active='legacy', intelligence='legacy', epoch=0, requestIds=true,
  coordinatedTerrain=true, navEpochs=true, navRequired=true}, 'legacy', 'legacy')
local sent, held, paused, settled = {}, false, false, false
local ctx = {connected=true, speed=30, safe=true, multiplayer=false,
  roads={setHold=function(v) held=v end}, paused=function() return paused end,
  pause=function(v) paused=v end, settled=function() return settled end,
  send=function(msg) sent[#sent+1]=msg end}
for epoch, backend in ipairs({'sumo','legacy','sumo','legacy'}) do
  T.check(m.requestIntelligence(backend), 'independent intelligence preference accepted')
  ctx.speed=30; m.update(2,ctx)
  T.check(#sent==epoch-1, 'driving defers coordinated switch')
  ctx.speed=0; m.update(1.1,ctx)
  local request=sent[#sent]
  T.check(request.backend=='legacy' and request.intelligence==backend, 'geometry remains independently selected')
  T.check(paused and held, 'one shared stationary hold')
  local retired=0
  m.state({ok=true, backend='legacy', intelligence=backend, epoch=epoch, requestId=request.requestId,
    terrainRebuild=true},function(state) retired=retired+1 end)
  T.check(retired==1 and not m.status().active and not m.intelligenceStatus().active, 'loaded states wait for full rebuild')
  m.progress({roads={5,5}, roadGeometry={epoch=epoch,failed=0}})
  settled=true; m.update(.1,ctx)
  T.check(paused, 'roads alone cannot complete terrain switch')
  T.check(not m.terrainAccept({roadEpoch=epoch-1}), 'old prepared terrain rejected')
  T.check(m.terrainAccept({roadEpoch=epoch}), 'current prepared terrain accepted under hold')
  m.terrainLoaded(); m.update(.1,ctx)
  T.check(paused, 'wait for current AI graph')
  T.check(not m.navigation({roadEpoch=epoch-1,roadBackend='legacy'}), 'stale AI graph rejected')
  T.check(m.navigation({roadEpoch=epoch,roadBackend='legacy'}), 'current AI graph received')
  settled=false; m.update(.1,ctx); T.check(paused, 'wait for actual graph/terrain/mesh settlement')
  settled=true; m.update(.1,ctx)
  T.check(not paused and not held and m.intelligenceStatus().active==backend, 'all consumers complete one epoch')
end
m.requestIntelligence('sumo'); m.update(1.1,ctx)
local request=sent[#sent]
m.state({ok=true, backend='legacy', intelligence='sumo', epoch=5,requestId=request.requestId,
  terrainRebuild=true},function() end)
m.update(120.1,ctx)
T.check(not paused and m.status().incomplete and not m.terrainAccept({roadEpoch=5}), 'timeout releases hold and blocks late terrain commit')
m.requestIntelligence('legacy'); m.update(1.1,ctx)
T.check(sent[#sent].rebuildTerrain and sent[#sent].intelligence=='legacy', 'explicit rollback rebuilds terrain')
request=sent[#sent]
m.state({ok=true,backend='legacy',intelligence='legacy',epoch=6,requestId=request.requestId,terrainRebuild=true},function() end)
m.terrainFailed('fixture import failure'); m.update(.1,ctx)
T.check(not paused and m.status().incomplete and m.intelligenceStatus().text:find('fixture import failure'), 'failed import remains visibly incomplete')
m.hello(nil,'legacy','sumo')
T.check(not m.requestIntelligence('sumo') and m.intelligenceStatus().active=='legacy', 'old servers retain Legacy')
T.done('test_roadintel')
