# Copyright (c) 2026, PostgreSQL Global Development Group

if not lua_dep.found()
  subdir_done()
endif

hstore_pl_lua_sources = files(
  'hstore_pl_lua.c',
)

# The Lua API functions must resolve to the same copy of Lua that pl_lua
# uses; a second copy would give two Lua runtimes operating on one
# lua_State.  On ELF and Mach-O platforms, compile against the Lua headers
# but don't link liblua, leaving the symbols to be resolved at load time
# against pl_lua, which is always loaded first.  Windows can't leave
# symbols unresolved in a DLL, so link against Lua there, which shares
# pl_lua's copy only if Lua is itself a DLL.  An import library defines
# __imp_ symbols and a static library doesn't, so check for one, and skip
# the transform if Lua is static.
hstore_pl_lua_deps = [lua_dep.partial_dependency(compile_args: true, includes: true)]
hstore_pl_lua_link_args = []
if host_system == 'windows'
  if not cc.links('''
extern void *__imp_lua_gettop;

int
main(void)
{
    return __imp_lua_gettop == 0;
}
''', args: test_c_args, dependencies: lua_dep, name: 'Lua is a DLL')
    subdir_done()
  endif
  hstore_pl_lua_deps = [lua_dep]
  hstore_pl_lua_sources += rc_lib_gen.process(win32ver_rc, extra_args: [
    '--NAME', 'hstore_pl_lua',
    '--FILEDESC', 'hstore_pl_lua - hstore transform for PL/Lua',])
elif host_system == 'darwin'
  hstore_pl_lua_link_args += '-Wl,-undefined,dynamic_lookup'
endif

hstore_pl_lua = shared_module('hstore_pl_lua',
  hstore_pl_lua_sources,
  include_directories: [pl_lua_inc, hstore_inc],
  link_args: hstore_pl_lua_link_args,
  kwargs: contrib_mod_args + {
    'dependencies': [hstore_pl_lua_deps, contrib_mod_args['dependencies']],
  },
)
contrib_targets += hstore_pl_lua

install_data(
  'hstore_pl_lua.control',
  'hstore_pl_lua--1.0.sql',
  'hstore_pl_luau.control',
  'hstore_pl_luau--1.0.sql',
  kwargs: contrib_data_args,
)

tests += {
  'name': 'hstore_pl_lua',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'regress': {
    'sql': [
      'create_ext',
      'hstore',
      'hstore_luau',
    ],
  },
}
