# Copyright (c) 2026, PostgreSQL Global Development Group

if not lua_dep.found()
  subdir_done()
endif

pl_lua_sources = files(
  'pl_lua.c',
  'pl_lua_datum.c',
  'pl_lua_error.c',
  'pl_lua_jsonb.c',
  'pl_lua_numeric.c',
  'pl_lua_spi.c',
  'pl_lua_time.c',
  'pl_lua_trigger.c',
  'pl_lua_trusted.c',
)

# same generator and output format as PL/pgSQL's copy
pl_lua_sources += custom_target('plerrcodes',
  input: files('../../backend/utils/errcodes.txt'),
  output: ['plerrcodes.h'],
  command: [perl, gen_plerrcodes, '@INPUT0@'],
  capture: true,
)

pl_lua_inc = include_directories('.')

if host_system == 'windows'
  pl_lua_sources += rc_lib_gen.process(win32ver_rc, extra_args: [
    '--NAME', 'pl_lua',
    '--FILEDESC', 'PL/Lua - procedural language',])
endif

pl_lua = shared_module('pl_lua',
  pl_lua_sources,
  c_pch: pch_postgres_h,
  include_directories: [pl_lua_inc, postgres_inc],
  kwargs: pg_mod_args + {
    'dependencies': [lua_dep, pg_mod_args['dependencies']],
  },
)
pl_targets += pl_lua

install_data(
  'pl_lua.control',
  'pl_lua--1.0.sql',
  'pl_luau.control',
  'pl_luau--1.0.sql',
  install_dir: dir_data_extension,
)

install_headers(
  'pl_lua.h',
  install_dir: dir_include_server,
)

pl_lua_regress = [
  'basic',
  'srf',
  'strict',
  'interpreters',
  'errors',
  'sandbox',
  'sandbox_env',
  'sandbox_inventory',
  'trusted',
  'paths',
  'encoding',
  'subxact',
  'types',
  'arrays',
  'rowdatum',
  'numerics',
  'jsonb',
  'horology',
  'horology_errors',
  'spi',
  'cursors',
  'quote',
  'helpers',
  'procedures',
  'triggers',
  'event_triggers',
  'roles',
  'privileges',
  'attacks',
  'extension',
]

tests += {
  'name': 'pl_lua',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'regress': {
    'sql': pl_lua_regress,
  },
  'tap': {
    'tests': [
      't/001_preload.pl',
      't/002_guc_placeholders.pl',
      't/003_fuzz.pl',
      't/004_attacker.pl',
    ],
  },
}

subdir('po', if_found: libintl)
