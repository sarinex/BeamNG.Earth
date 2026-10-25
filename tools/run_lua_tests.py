#!/usr/bin/env python3
"""Run existing Lua suites with standalone LuaJIT or an optional Lupa LuaJIT runtime.

.venv/Scripts/python.exe tools/run_lua_tests.py --runtime-dir build/lua-runtime roadgeom roads settings core_stubbed
Developer-only fallback: python -m pip install --target build/lua-runtime lupa
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CODE = '''
import sys, time
if sys.argv[1]:
    sys.path.insert(0, sys.argv[1])
from lupa.luajit21 import LuaRuntime
lua = LuaRuntime()
lua.globals().arg = lua.table_from({0: sys.argv[2]})
lua.globals().test_clock = time.perf_counter
# Pure suites need the same wall-clock contract as the game's hptimer/LuaSocket.
lua.execute("package.preload.socket = function() return {gettime=test_clock} end")
lua.execute(open(sys.argv[2], encoding='utf-8').read())
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--runtime-dir', help='directory containing optional Lupa; use the LuaJIT 2.1 runtime')
    ap.add_argument('suites', nargs='*', help='names without test_ or .lua (default: all mod Lua suites)')
    args = ap.parse_args()
    names = args.suites or [p.stem[5:] for p in sorted((ROOT / 'mod' / 'tests').glob('test_*.lua'))]
    luajit = shutil.which('luajit') if not args.runtime_dir else None
    failed = []
    for name in names:
        path = ROOT / 'mod' / 'tests' / f'test_{name}.lua'
        if not path.is_file():
            ap.error(f'unknown suite: {name}')
        cmd = [luajit, str(path)] if luajit else [sys.executable, '-c', CODE, args.runtime_dir or '', str(path)]
        result = subprocess.run(cmd, cwd=ROOT)
        if result.returncode:
            failed.append(name)
    print(f'{len(names)-len(failed)}/{len(names)} Lua suites passed' + (f'; failed: {", ".join(failed)}' if failed else ''))
    return bool(failed)


if __name__ == '__main__':
    sys.exit(main())
