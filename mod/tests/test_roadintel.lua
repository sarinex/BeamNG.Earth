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
m.requestIntelligence('legacy'); m.update(1.1,ctx); request=sent[#sent]
m.update(.1,ctx)
T.check(m.status().phase=='request' and paused, 'retry waits for its acknowledgement without replaying the previous terrain error')
m.state({ok=true,backend='legacy',intelligence='legacy',epoch=7,requestId=request.requestId,terrainRebuild=true},function() end)
m.terrainFailed('fixture restoration failure',true); m.update(.1,ctx)
T.check(paused and held and m.intelligenceStatus().terrainBlocked, 'failed terrain restoration preserves the simulation hold')
ctx.safe=false
m.requestIntelligence('legacy'); m.update(1.1,ctx); request=sent[#sent]
T.check(request.rebuildTerrain, 'rollback can restore missing terrain while already held')
m.update(.1,ctx)
T.check(m.status().phase=='request' and paused, 'blocked restoration can wait for the recovery acknowledgement')
m.state({ok=true,backend='legacy',intelligence='legacy',epoch=8,requestId=request.requestId,terrainRebuild=true},function() end)
m.terrainLoaded(); m.navigation({roadEpoch=8,roadBackend='legacy'})
m.progress({roads={5,5},roadGeometry={epoch=8,failed=0}}); m.update(.1,ctx)
T.check(not paused and not held and not m.intelligenceStatus().terrainBlocked, 'successful terrain restoration releases only the owned hold')
m.hello(nil,'legacy','sumo')
T.check(not m.requestIntelligence('sumo') and m.intelligenceStatus().active=='legacy', 'old servers retain Legacy')

-- The settings adapter applies the returned accepted geometry after rejection.
-- An unchanged geometry must not requeue a rejected intelligence preference.
m.hello({version=1,active='geos',intelligence='legacy',epoch=0,requestIds=true,
  coordinatedTerrain=true,navEpochs=true,navRequired=false},'geos','sumo')
ctx.safe=true; settled=true
local requests=#sent
m.update(1.1,ctx); request=sent[#sent]
local restore,restoreIntel=m.state({ok=false,backend='geos',intelligence='legacy',epoch=0,
  requestId=request.requestId,error='No prepared regions; run tools/prepare_roadintel.py'},function() error('rejection must not retire roads') end)
if restore then m.request(restore) end -- settings.set('roadGeometryBackend', restore)
if restoreIntel then m.requestIntelligence(restoreIntel) end
for _=1,5 do m.update(1.1,ctx) end
T.check(#sent==requests+1 and not paused and not held, 'rejected intelligence stays idle after settings callbacks')
T.check(m.intelligenceStatus().desired=='legacy' and m.intelligenceStatus().active=='legacy'
  and m.intelligenceStatus().text:find('No prepared regions'), 'rejection restores the intelligence preference and retains its actionable error')
m.requestIntelligence('sumo'); m.update(1.1,ctx); request=sent[#sent]
m.requestIntelligence('legacy') -- user cancels before the rejection arrives
requests=#sent
restore,restoreIntel=m.state({ok=false,backend='geos',intelligence='legacy',epoch=0,
  requestId=request.requestId,error='No prepared regions'},function() error('rejection must not retire roads') end)
T.check(restore==nil and restoreIntel==nil, 'rejection does not overwrite a newer user choice through settings')
m.update(1.1,ctx)
T.check(#sent==requests and m.intelligenceStatus().desired=='legacy' and not paused and not held,
  'cancelling the intelligence request stays cancelled after rejection')
T.done('test_roadintel')
