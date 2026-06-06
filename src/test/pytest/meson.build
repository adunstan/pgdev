# Copyright (c) 2025-2026, PostgreSQL Global Development Group

# Register the infrastructure self-tests.  Whether they actually run is
# governed by the 'pytest' feature option, handled in the test-generation loop
# in the top-level meson.build (it emits a skip when pytest is not enabled).
tests += {
  'name': 'pytest',
  'sd': meson.current_source_dir(),
  'bd': meson.current_build_dir(),
  'pytest': {
    'tests': [
      'pyt/test_libpq.py',
      'pyt/test_replication.py',
    ],
  },
}

# Install the Python test framework alongside the Perl one, so out-of-tree
# (PGXS) test suites can import it.
install_data(
  'pgtap.py',
  'pyproject.toml',
  install_dir: dir_pgxs / 'src/test/pytest')

install_data(
  'libpq/__init__.py',
  'libpq/bindings.py',
  'libpq/constants.py',
  'libpq/errors.py',
  'libpq/findlib.py',
  'libpq/oids.py',
  'libpq/pgnotify.py',
  'libpq/result.py',
  'libpq/session.py',
  install_dir: dir_pgxs / 'src/test/pytest/libpq')

install_data(
  'pypg/__init__.py',
  'pypg/_env.py',
  'pypg/command.py',
  'pypg/fixtures.py',
  'pypg/server.py',
  'pypg/util.py',
  install_dir: dir_pgxs / 'src/test/pytest/pypg')
