# Copyright (c) 2022-2026, PostgreSQL Global Development Group

psql_sources = files(
  'command.c',
  'common.c',
  'copy.c',
  'crosstabview.c',
  'describe.c',
  'help.c',
  'input.c',
  'large_obj.c',
  'mainloop.c',
  'prompt.c',
  'startup.c',
  'stringutils.c',
  'variables.c',
)

psqlscanslash = custom_target('psqlscanslash',
  input: 'psqlscanslash.l',
  output: 'psqlscanslash.c',
  command: [flex_cmd, '--no-backup', '--', '-Cfe', '-p', '-p'])
generated_sources += psqlscanslash
psql_sources += psqlscanslash

tabcomplete = custom_target('tabcomplete',
  input: 'tab-complete.in.c',
  output: 'tab-complete.c',
  command: [
    perl, files('gen_tabcomplete.pl'), '@INPUT@',
    '--outfile', '@OUTPUT@',
  ],
)
generated_sources += tabcomplete
psql_sources += tabcomplete

sql_help = custom_target('psql_help',
  output: ['sql_help.c', 'sql_help.h'],
  depfile: 'sql_help.dep',
  command: [
    perl, files('create_help.pl'),
    '--docdir', '@SOURCE_ROOT@/doc/src/sgml/ref',
    '--depfile', '@DEPFILE@',
    '--outdir', '@OUTDIR@',
    '--basename', 'sql_help',
  ],
)
generated_sources += sql_help.to_list()
psql_sources += sql_help

if host_system == 'windows'
  psql_sources += rc_bin_gen.process(win32ver_rc, extra_args: [
    '--NAME', 'psql',
    '--FILEDESC', 'psql - the PostgreSQL interactive terminal',])
endif

psql = executable('psql',
  psql_sources,
  c_pch: pch_postgres_fe_h,
  include_directories: include_directories('.'),
  dependencies: [frontend_code, libpq, readline],
  kwargs: default_bin_args,
)
bin_targets += psql

install_data('psqlrc.sample',
  install_dir: dir_data,
)

tests += {
  'name': 'psql',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'pytest': {
    'env': {'with_readline': readline.found() ? 'yes' : 'no'},
    # 010_tab_completion and 030_pager drive psql through an interactive pty;
    # they use pexpect via the interactive_psql fixture in pyt/conftest.py and
    # skip when pexpect (or readline / a working "wc -l") is unavailable.
    'tests': [
      'pyt/test_001_basic.py',
      'pyt/test_010_tab_completion.py',
      'pyt/test_020_cancel.py',
      'pyt/test_030_pager.py',
    ],
  },
}

subdir('po', if_found: libintl)
