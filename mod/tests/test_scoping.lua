-- luajit mod/tests/test_scoping.lua  (every mod Lua file): no global read of a name the same file declares as a local.
-- A function that uses a local declared further down reads the (nil) global of that name instead: the BeamMP session's
-- test switch errored that way (remoteOwners) and roads.lua's prewarm after a level reload never ran (2026-10-22).
-- Reads LuaJIT's bytecode listing (luajit -bl): GGET is a global read.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local root = dir .. '/../beamng_earth/lua'

local files = {}
if test_lua_files then
  for _, f in ipairs(test_lua_files) do files[#files+1] = f end
else
  local command = package.config:sub(1,1) == '\\' and ('dir /s /b "' .. root .. '\\*.lua"')
    or ('find "' .. root .. '" -name "*.lua"')
  local p = io.popen(command)
  for f in p:lines() do files[#files + 1] = f:gsub('\\', '/') end
  p:close()
end
T.check(#files > 20, 'found the mod\'s Lua files: ' .. #files)

local bad = {}
local util, bit = require('jit.util'), require('bit')
-- Derive GGET's opcode from this runtime; no standalone luajit binary needed.
local probe = assert(loadstring('return __scoping_unique_global'))
local opcode = bit.band(util.funcbc(probe, 1), 255)
local function globalsOf(fn, seen)
  local info = util.funcinfo(fn)
  for pc = 1, info.bytecodes - 1 do
    local ins = util.funcbc(fn, pc)
    if bit.band(ins, 255) == opcode then seen[util.funck(fn, -1-bit.rshift(ins, 16))] = true end
  end
  for k = 1, info.gcconsts do
    local child = util.funck(fn, -k)
    if type(child) == 'function' then globalsOf(child, seen) end
  end
end
for _, f in ipairs(files) do
  local src = io.open(f):read('*a')
  local seen = {}
  globalsOf(assert(loadfile(f)), seen)
  for g in pairs(seen) do
    if type(g) == 'string' then
      if src:find('\n%s*local%s+' .. g .. '%f[^%w_]') or src:find('\n%s*local%s+function%s+' .. g .. '%f[^%w_]')
          or src:find('\n%s*local%s+[%w_,%s]-,%s*' .. g .. '%f[^%w_]') then
        bad[#bad + 1] = f:match('lua/(.*)$') .. ': ' .. g
      end
    end
  end
end
T.check(#bad == 0, 'no global reads of a file\'s own locals: ' .. table.concat(bad, ', '))
T.done('test_scoping')
