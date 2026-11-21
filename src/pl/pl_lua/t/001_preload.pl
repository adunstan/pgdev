
# Copyright (c) 2026, PostgreSQL Global Development Group

# Test PL/Lua loaded through shared_preload_libraries, where interpreters
# are partly built in the postmaster.

use strict;
use warnings FATAL => 'all';

use PostgreSQL::Test::Cluster;
use PostgreSQL::Test::Utils;
use Test::More;

my $node = PostgreSQL::Test::Cluster->new('preload');
$node->init;
$node->append_conf(
	'postgresql.conf', q{
shared_preload_libraries = 'pl_lua'
pl_lua.on_init = 'marker = "built in the postmaster"'
pl_lua.interpreter_reload_ident = 'one'
pl_lua.prebuilt_interpreters = 2
});
$node->start;

$node->safe_psql('postgres', q{
CREATE EXTENSION pl_lua;
CREATE EXTENSION pl_luau;
CREATE FUNCTION u_info() RETURNS text LANGUAGE pl_luau AS $$
  return tostring(marker) .. '/' .. _PL_IDENT .. '/' .. tostring(_PL_IDENT_NEW)
$$;
CREATE FUNCTION t_info() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(marker) .. '/' .. _PL_IDENT
$$;
});

# The code of on_init ran before the session started.  Only the untrusted
# language sees the global it set, since the sandbox is built afterwards.
is($node->safe_psql('postgres', 'SELECT u_info()'),
	'built in the postmaster/one/nil', 'untrusted interpreter was prebuilt');
is($node->safe_psql('postgres', 'SELECT t_info()'),
	'nil/one', 'trusted interpreter does not see globals of on_init');

# A session that outlives a reload
my $session = $node->background_psql('postgres');
is($session->query_safe('SELECT u_info()'),
	'built in the postmaster/one/nil', 'session started with ident one');

$node->append_conf(
	'postgresql.conf', q{
pl_lua.interpreter_reload_ident = 'two'
pl_lua.on_init = 'marker = "rebuilt"'
});
$node->reload;
$node->poll_query_until('postgres',
	q{SELECT current_setting('pl_lua.interpreter_reload_ident') = 'two'});

# the running session is told about the new ident, but keeps its interpreter
is($session->query_safe('SELECT u_info()'),
	'built in the postmaster/one/two',
	'running session sees the new ident in _PL_IDENT_NEW');

# a new session gets a rebuilt interpreter
is($node->safe_psql('postgres', 'SELECT u_info()'),
	'rebuilt/two/nil', 'new session uses a rebuilt interpreter');
$session->quit;

# Changing on_init alone rebuilds the interpreters while an ident is set
$node->append_conf('postgresql.conf', q{pl_lua.on_init = 'marker = "again"'});
$node->reload;
$node->poll_query_until('postgres',
	q{SELECT current_setting('pl_lua.on_init') = 'marker = "again"'});
is($node->safe_psql('postgres', 'SELECT u_info()'),
	'again/two/nil', 'changing on_init rebuilds while an ident is set');

# With no ident, changing on_init does not affect the prebuilt interpreters
$node->append_conf(
	'postgresql.conf', q{
pl_lua.interpreter_reload_ident = ''
});
$node->reload;
$node->poll_query_until('postgres',
	q{SELECT current_setting('pl_lua.interpreter_reload_ident') = ''});
$node->append_conf('postgresql.conf', q{pl_lua.on_init = 'marker = "ignored"'});
$node->reload;
$node->poll_query_until('postgres',
	q{SELECT current_setting('pl_lua.on_init') = 'marker = "ignored"'});
SKIP:
{
	# Where processes are not forked, every session builds its interpreters
	# from the current settings, and nothing is prebuilt.
	skip 'interpreters are not inherited on this platform', 1
	  if $PostgreSQL::Test::Utils::windows_os;
	unlike($node->safe_psql('postgres', 'SELECT u_info()'),
		qr/ignored/, 'on_init change without an ident does not rebuild');
}

# Changing the number of prebuilt interpreters rebuilds them.  Count the
# builds with an on_init that appends to a file.
my $builds = $node->basedir . '/builds.txt';
$builds =~ s{\\}{/}g;    # a backslash would start a Lua escape
$node->append_conf(
	'postgresql.conf', qq{
pl_lua.interpreter_reload_ident = ''
pl_lua.on_init = 'local f = io.open("$builds", "a"); f:write("x", string.char(10)); f:close()'
});
$node->restart;
my $count = sub { -e $builds ? scalar(() = slurp_file($builds) =~ /x/g) : 0 };
SKIP:
{
	skip 'interpreters are not prebuilt on this platform', 2
	  if $PostgreSQL::Test::Utils::windows_os;
	is($count->(), 2, 'two interpreters were built at start');
	$node->append_conf('postgresql.conf', q{pl_lua.prebuilt_interpreters = 3});
	$node->reload;
	$node->poll_query_until('postgres',
		q{SELECT current_setting('pl_lua.prebuilt_interpreters') = '3'});
	is($count->(), 5, 'a new count rebuilds the interpreters');
}

$node->stop;

# An error in on_init prevents the server from starting
my $bad = PostgreSQL::Test::Cluster->new('badinit');
$bad->init;
$bad->append_conf(
	'postgresql.conf', q{
shared_preload_libraries = 'pl_lua'
pl_lua.on_init = 'error("deliberate failure")'
});
if ($PostgreSQL::Test::Utils::windows_os)
{
	# Nothing is built in the postmaster here, so the server starts, and the
	# error is raised when an interpreter is first needed.
	ok($bad->start(fail_ok => 1), 'server starts despite a failing on_init');
	$bad->safe_psql('postgres', 'CREATE EXTENSION pl_luau');
	my ($ret, $stdout, $stderr) =
	  $bad->psql('postgres', q{DO LANGUAGE pl_luau $$ print(1) $$});
	isnt($ret, 0, 'the first use fails');
	like($stderr, qr/deliberate failure/,
		'the error from on_init is reported at first use');
	$bad->stop;
}
else
{
	ok(!$bad->start(fail_ok => 1),
		'server does not start after on_init failure');
	like(slurp_file($bad->logfile), qr/deliberate failure/,
		'the error from on_init is reported');
}

# No prebuilt interpreters is fine
my $none = PostgreSQL::Test::Cluster->new('noprebuilt');
$none->init;
$none->append_conf(
	'postgresql.conf', q{
shared_preload_libraries = 'pl_lua'
pl_lua.prebuilt_interpreters = 0
});
$none->start;
$none->safe_psql('postgres', 'CREATE EXTENSION pl_luau');
is( $none->safe_psql(
		'postgres',
		q{DO LANGUAGE pl_luau $$ spi.execute('SELECT 1') $$; SELECT 'ok'}),
	'ok',
	'works with no prebuilt interpreters');
$none->stop;

done_testing();
