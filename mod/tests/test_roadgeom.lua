-- luajit mod/tests/test_roadgeom.lua
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local function fresh()
  package.loaded['beamngEarth/roadgeom'] = nil
  return require('beamngEarth/roadgeom')
end
local m = fresh()
T.check(m.status().active == 'legacy', 'default is legacy')
T.check(m.hello(nil, 'geos') == 'legacy', 'old server restores persisted legacy')
T.check(not m.request('geos'), 'unsupported GEOS request rejected')
T.check(m.status().text:find('does not support'), 'unsupported capability visible')

m = fresh()
m.hello({version=1, active='legacy', epoch=0}, 'legacy')
local sent, paused, retired, hold, settled = {}, false, 0, false, false
local ctx = {connected=true, safe=true, speed=20, multiplayer=false,
  roads={setHold=function(v) hold=v end},
  paused=function() return paused end, pause=function(v) paused=v end,
  settled=function() return settled end, send=function(v) sent[#sent+1]=v end}
local function switch(to, epoch)
  m.request(to)
  ctx.speed = 20
  m.update(2, ctx)
  T.check(#sent == epoch-1, 'driving defers request')
  ctx.speed = 0
  m.update(1.1, ctx)
  T.check(sent[#sent].backend == to and paused and hold, 'stationary switch holds simulation')
  m.state({ok=true, backend=to, epoch=epoch}, function() retired=retired+1 end)
  T.check(not m.accept({roadEpoch=epoch-1, roadBackend=to}), 'stale worker tile rejected')
  T.check(m.accept({roadEpoch=epoch, roadBackend=to}), 'current tile accepted')
  T.check(m.status().active ~= to, 'active waits for geometry and collision')
  m.progress({roads={5,5}})
  m.progress({roads={5,5}, roadGeometry={epoch=epoch, failed=1}})
  settled=true; m.update(.1,ctx)
  T.check(paused, 'failed server tiles do not count as loaded geometry')
  m.progress({roads={5,5}, roadGeometry={epoch=epoch-1, failed=0}})
  m.update(.1,ctx)
  T.check(paused, 'old progress epoch cannot complete a rebuild')
  m.progress({roads={5,5}, roadGeometry={epoch=epoch, failed=0}})
  settled=false; m.update(.1,ctx)
  T.check(paused, 'waits for governor and collision')
  settled=true; m.update(.1,ctx)
  T.check(m.status().active == to and not paused and not hold, 'active only after loading and collision')
end
switch('geos',1)
switch('legacy',2)
switch('geos',3)
T.check(retired==3, 'both directions retire old geometry')
m.tile({data={roadGeometry={fallbacks=2}}})
T.check(m.status().text:find('legacy fallback') and m.status().active=='geos', 'loaded GEOS status exposes legacy tile fallback')
m.request('legacy'); m.request('geos')
T.check(m.status().pending==nil and m.status().text:find('GEOS'), 'cancelling a pending selection restores actual active label')
m.request('legacy'); ctx.multiplayer=true; m.update(5,ctx)
T.check(#sent==3 and m.status().text:find('multiplayer'), 'multiplayer switch deferred')
ctx.multiplayer=false; ctx.safe=false; m.update(5,ctx)
T.check(#sent==3, 'unsafe collision position deferred')
ctx.safe=true; m.update(1.1,ctx)
m.state({ok=false, error='missing Shapely'}, function() end)
m.update(.1,ctx)
T.check(not paused and m.status().text:find('missing Shapely'), 'missing dependencies release hold with diagnostic')
m.request('legacy'); m.update(1.1,ctx)
ctx.connected=false; m.update(.1,ctx)
T.check(not paused, 'disconnect releases simulation hold')
m.hello({version=1, active='legacy', epoch=0}, 'geos')
T.check(m.status().pending=='geos', 'reconnect re-requests saved preference')

local function session(ids, initiallyPaused)
  local gen = fresh()
  gen.hello({version=1, active='legacy', epoch=0, requestIds=ids}, 'legacy')
  local env = {sent={}, paused=initiallyPaused or false, hold=false, retired=0, settled=false, ids=ids}
  env.ctx = {connected=true, safe=true, speed=0, multiplayer=false,
    roads={setHold=function(v) env.hold=v end},
    paused=function() return env.paused end, pause=function(v) env.paused=v end,
    settled=function() return env.settled end, send=function(msg) env.sent[#env.sent+1]=msg end}
  function env.choose(value) gen.request(value); gen.update(1.1,env.ctx) end
  function env.ack(value, epoch, id, ok)
    return gen.state({ok=ok~=false, backend=value, epoch=epoch,
      requestId=ids and (id or env.sent[#env.sent].requestId) or nil, error='fixture rejection'},
      function() env.retired=env.retired+1 end)
  end
  function env.done(epoch)
    env.settled=true
    gen.progress({roads={5,5}, roadGeometry={epoch=epoch,failed=0}})
    gen.update(.1,env.ctx)
  end
  function env.timeoutLoading()
    env.choose('geos'); env.ack('geos',1)
    gen.progress({roads={2,5}, roadGeometry={epoch=1,failed=0}})
    gen.update(120.1,env.ctx)
  end
  return gen,env
end

do
  local gen,env=session(true)
  local probes=0
  env.ctx.safe=function() probes=probes+1; return false, 'waiting for the terrain update to finish' end
  gen.request('geos'); env.ctx.speed=10; gen.update(5,env.ctx)
  T.check(probes==0 and #env.sent==0 and gen.status().text:find('stop the vehicle'),
    'driving defers terrain probes and explains the stationary requirement')
  env.ctx.speed=0; gen.update(.5,env.ctx)
  T.check(probes==0 and #env.sent==0, 'terrain probes wait for a full stationary second')
  gen.update(.6,env.ctx)
  T.check(probes==1 and #env.sent==0 and not env.paused
    and gen.status().text:find('waiting for the terrain update to finish'),
    'blocked switch preserves the precise safety reason without taking a hold')
  env.ctx.safe=function() return true end
  gen.update(.1,env.ctx); env.ack('geos',1); env.done(1)
  T.check(gen.status().active=='geos' and not env.hold, 'pending switch proceeds after terrain support becomes safe')
end

-- Exact regression: accepted GEOS -> loading timeout -> choose the previously loaded Legacy.
-- Also exercise an existing v1 companion without optional request-ID support.
for _, ids in ipairs({false,true}) do
  local gen,env=session(ids)
  env.timeoutLoading()
  local state=gen.status()
  T.check(state.accepted=='geos' and state.active==nil and state.incomplete and state.phase=='incomplete',
    'timeout reports an incomplete world, not the previously loaded Legacy backend')
  T.check(state.lastLoaded=='legacy' and state.serverKnown and state.text:find('timed out'),
    'timeout retains the actual server selection and explicit rollback target')
  T.check(not env.paused and not env.hold and not gen.needsUpdate(), 'incomplete state releases only its own hold and stays idle')
  local message=state.text
  gen.progress({roads={5,5},roadGeometry={epoch=1,failed=0}}); gen.update(.1,env.ctx)
  T.check(not gen.accept({roadBackend='geos',roadEpoch=1}) and gen.status().text==message,
    'late tiles/progress cannot complete an abandoned rebuild after simulation resumes')
  gen.request('legacy'); env.ctx.speed=15; gen.update(2,env.ctx)
  T.check(#env.sent==1 and not env.paused, 'rollback after timeout remains pending while driving')
  env.ctx.speed=0; env.ctx.safe=false; gen.update(2,env.ctx)
  T.check(#env.sent==1 and not env.paused, 'rollback after timeout still checks collision support')
  env.ctx.safe=true
  env.choose('legacy')
  T.check(#env.sent==2 and env.sent[2].backend=='legacy' and env.paused and env.hold,
    'selecting Legacy after timeout sends a real server rollback request')
  env.ack('legacy',2)
  T.check(env.retired==2 and gen.status().active==nil, 'rollback retires partial GEOS and waits for its own geometry')
  gen.progress({roads={5,5},roadGeometry={epoch=2,failed=0}})
  gen.progress({roads={0,5},roadGeometry={epoch=1,failed=1}})
  env.settled=true; gen.update(.1,env.ctx)
  T.check(gen.status().active=='legacy', 'old epoch progress cannot erase completed rollback progress')
  env.done(2)
  T.check(gen.status().active=='legacy' and gen.status().accepted=='legacy' and not gen.status().incomplete
    and not env.paused and not env.hold, 'Legacy is active only after the rollback rebuild and collision settle')
  T.check(not gen.accept({roadBackend='geos',roadEpoch=1}), 'abandoned GEOS epoch remains stale after rollback')
end

do
  local gen,env=session(true,true)
  env.timeoutLoading()
  T.check(env.paused and not env.hold, 'timeout preserves a pause owned by the player')
  env.choose('geos')
  T.check(#env.sent==2 and env.sent[2].backend=='geos', 'selecting the same accepted GEOS value explicitly retries an incomplete build')
  env.ack('geos',2); env.done(2)
  T.check(gen.status().active=='geos' and not gen.status().incomplete and env.paused,
    'same-backend retry recovers without unpausing a player pause')
end

do
  local gen,env=session(true)
  env.choose('geos')
  local first=env.sent[1].requestId
  gen.update(120.1,env.ctx)
  T.check(not gen.status().serverKnown and gen.status().active==nil, 'request timeout marks the server selection unconfirmed')
  env.choose('legacy')
  env.ack('geos',1,first)
  T.check(gen.status().phase=='request' and gen.status().epoch==0 and env.retired==0,
    'late acknowledgement from the timed-out request cannot replace the newer rollback')
  env.ack('legacy',2); env.done(2)
  T.check(gen.status().active=='legacy', 'correlated rollback recovers after a missing acknowledgement')
end

do
  local gen,env=session(true)
  env.choose('geos'); gen.update(120.1,env.ctx)
  env.ack('geos',1)
  T.check(gen.status().accepted=='geos' and gen.status().serverKnown and gen.status().active==nil
    and env.retired==0 and not env.paused, 'late acknowledgement learns server state without loading or retiring unheld geometry')
  env.choose('legacy'); env.ack('legacy',2); env.done(2)
  T.check(gen.status().active=='legacy', 'explicit rollback works after a late acknowledgement')
end

do
  local gen,env=session(false)
  env.choose('geos'); gen.update(120.1,env.ctx)
  env.choose('legacy')
  T.check(#env.sent==1 and not env.paused, 'older uncorrelated server is not sent overlapping requests after timeout')
  env.ack('geos',1)
  T.check(env.retired==0 and gen.status().pending=='legacy', 'old-server late acknowledgement keeps the rollback queued safely')
  gen.update(1.1,env.ctx); env.ack('legacy',2); env.done(2)
  T.check(#env.sent==2 and gen.status().active=='legacy', 'older server completes the queued rollback after its first reply')
end

do
  local gen,env=session(true)
  env.timeoutLoading(); env.choose('legacy')
  local restore=env.ack('geos',1,nil,false)
  gen.update(.1,env.ctx)
  T.check(restore==nil and gen.status().active==nil and gen.status().incomplete and not env.paused,
    'rejected rollback cannot pretend the previous Legacy backend is loaded')
  env.choose('legacy'); env.ack('legacy',2); env.done(2)
  T.check(gen.status().active=='legacy', 'rollback can be retried after rejection')
end

do
  local gen,env=session(true)
  env.choose('geos'); env.ack('geos',1); gen.request('legacy')
  gen.update(120.1,env.ctx)
  T.check(gen.status().pending=='legacy' and not env.paused, 'rollback selected while loading remains queued after timeout')
  gen.update(1.1,env.ctx); env.ack('legacy',2); env.done(2)
  T.check(gen.status().active=='legacy', 'queued user rollback completes without another click')
end

do
  local gen,env=session(true)
  env.timeoutLoading()
  gen.hello({version=1,active='legacy',epoch=0,requestIds=true},'legacy')
  T.check(gen.status().active==nil and gen.status().pending=='legacy', 'reconnect does not equate the server selection with a completed rebuild')
  gen.update(1.1,env.ctx); env.ack('legacy',1); env.done(1)
  T.check(gen.status().active=='legacy' and not gen.status().incomplete, 'reconnect completes a fresh recovery epoch')
end
T.done('test_roadgeom')
