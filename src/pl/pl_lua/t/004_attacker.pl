# Copyright (c) 2026, PostgreSQL Global Development Group

# What a role without privileges can do with the trusted language, logged in
# as that role.  This cannot be done in a regression test: those connect as a
# superuser, and a superuser session can switch to any role with SET SESSION
# AUTHORIZATION, whatever role it is currently using.
#
# 1. Statements that need privileges, run through SPI, must all fail.
# 2. Changing the session's authorization from Lua must fail.
# 3. A query cancel (statement_timeout) must stop Lua code however it is
#    written, including code that catches errors in a loop.

use strict;
use warnings FATAL => 'all';

use PostgreSQL::Test::Cluster;
use PostgreSQL::Test::Utils;
use Test::More;

my $node = PostgreSQL::Test::Cluster->new('attacker');
# on Windows, the role that connects must be mapped to the operating system user
$node->init(auth_extra => [ '--create-role' => 'attacker' ]);
$node->start;

my $su = $node->safe_psql('postgres',
	q{SELECT rolname FROM pg_roles WHERE rolsuper ORDER BY oid LIMIT 1});

$node->safe_psql('postgres', q{
CREATE EXTENSION pl_lua;
CREATE EXTENSION pl_luau;
CREATE ROLE attacker LOGIN;
CREATE TABLE secret (v text);
INSERT INTO secret VALUES ('s3cret');
GRANT ALL ON SCHEMA public TO attacker;
});

my $tmp = $node->basedir;
$tmp =~ s{\\}{/}g;

# 1. SQL that needs privileges, through SPI.  The list is run in one
# function; those that succeed are returned.
my @queries = (
	"SELECT pg_read_file('/etc/passwd')",
	"SELECT pg_read_binary_file('/etc/passwd')",
	"SELECT pg_ls_dir('.')",
	"SELECT pg_stat_file('/etc/passwd')",
	"COPY (SELECT 1) TO PROGRAM 'true'",
	"COPY (SELECT 1) TO '$tmp/pll_attack_copy'",
	"COPY secret TO '$tmp/pll_attack_copy2'",
	"SELECT lo_import('/etc/passwd')",
	"SELECT lo_export(0, '$tmp/pll_attack_lo')",
	"CREATE FUNCTION pll_c() RETURNS int AS 'libc.so.6', 'getpid' LANGUAGE C",
	"LOAD 'plpgsql'",
	"CREATE EXTENSION plpython3u",
	"CREATE LANGUAGE pll_x HANDLER plpgsql_call_handler",
	"SET ROLE $su",
	"SET SESSION AUTHORIZATION $su",
	"ALTER SYSTEM SET work_mem = '1MB'",
	"SELECT pg_reload_conf()",
	"SELECT pg_rotate_logfile()",
	"SELECT pg_switch_wal()",
	"ALTER ROLE attacker SUPERUSER",
	"CREATE ROLE pll_super SUPERUSER",
	"GRANT pg_execute_server_program TO attacker",
	"GRANT pg_read_server_files TO attacker",
	"GRANT $su TO attacker",
	"SELECT rolpassword FROM pg_authid",
	"UPDATE pg_class SET relname = relname WHERE false",
	"INSERT INTO pg_proc SELECT * FROM pg_proc WHERE false",
	"CREATE TABLESPACE pll_ts LOCATION '$tmp'",
	"CREATE DATABASE pll_db",
	"DROP SCHEMA pg_catalog CASCADE",
	"SELECT set_config('pl_lua.on_init', 'x', false)",
	"SELECT set_config('pl_lua.on_trusted_init', 'x', false)",
	"SET pl_lua.install_globals = off",
	"SET pl_lua.check_for_interrupts = off",
	"SET session_replication_role = replica",
	"SET allow_system_table_mods = on",
	"SET log_statement = 'all'",
	"DO LANGUAGE pl_luau 'return 1'",
	"CREATE FUNCTION pll_u() RETURNS int LANGUAGE pl_luau AS 'return 1'",
);

my $list = join(",\n", map { "\"$_\"" } @queries);
$node->safe_psql('postgres', qq{
CREATE FUNCTION pll_spi_attacks() RETURNS SETOF text LANGUAGE pl_lua AS \$body\$
  local queries = { $list }
  local rows = {}
  for _, q in ipairs(queries) do
    if pcall(spi.execute, q) then rows[#rows + 1] = q end
  end
  table.sort(rows)
  for _, q in ipairs(rows) do coroutine.yield(q) end
\$body\$;
GRANT EXECUTE ON FUNCTION pll_spi_attacks() TO attacker;
});

my $attacker = [ '--username' => 'attacker' ];
my $out = $node->safe_psql('postgres', q{SELECT * FROM pll_spi_attacks()},
	extra_params => $attacker);
is($out, '', 'no privileged statement succeeds through SPI');

# The same statements, directly, as a check that the role has no privileges
# by other means (these should fail too; the result is the number that fail)
my $failed = 0;
foreach my $q (@queries)
{
	next if $q =~ /pl_lua\.|log_statement|replication_role|allow_system|work_mem|pg_reload|rotate_logfile|switch_wal|pll_u\(/;
	my ($ret) = $node->psql('postgres', $q, extra_params => $attacker);
	$failed++ if $ret != 0;
}
ok($failed > 20, "privileged statements fail when run directly ($failed)");

foreach my $f ("pll_c", "pll_x", "pll_u", "pll_ts", "pll_db", "pll_super")
{
	is($node->safe_psql('postgres', "SELECT count(*) FROM pg_proc WHERE proname = '$f'")
		. $node->safe_psql('postgres', "SELECT count(*) FROM pg_roles WHERE rolname = '$f'"),
		'00', "$f was not created");
}
ok(!-e "$tmp/pll_attack_copy" && !-e "$tmp/pll_attack_copy2" && !-e "$tmp/pll_attack_lo",
	'no file was written');
is($node->safe_psql('postgres',
	q{SELECT rolsuper FROM pg_roles WHERE rolname = 'attacker'}), 'f',
	'the role is still not a superuser');

# 2. In the same session, the role stays the role
my $who = $node->safe_psql('postgres', q{
CREATE FUNCTION pll_try_auth() RETURNS text LANGUAGE pl_lua AS $$
  pcall(spi.execute, "SET SESSION AUTHORIZATION " .. spi.execute("SELECT rolname FROM pg_roles WHERE rolsuper ORDER BY oid LIMIT 1")[1].rolname)
  pcall(spi.execute, "SET ROLE " .. spi.execute("SELECT rolname FROM pg_roles WHERE rolsuper ORDER BY oid LIMIT 1")[1].rolname)
  pcall(spi.execute, "RESET SESSION AUTHORIZATION")
  return spi.execute("SELECT session_user || '/' || current_user AS u")[1].u
$$;
GRANT EXECUTE ON FUNCTION pll_try_auth() TO attacker;
});
$out = $node->safe_psql('postgres', q{SELECT pll_try_auth()},
	extra_params => $attacker);
is($out, 'attacker/attacker', 'session authorization cannot be changed from Lua');

# 3. Cancellation
$node->safe_psql('postgres', q{
CREATE FUNCTION pll_spin(kind int) RETURNS int LANGUAGE pl_lua AS $$
  if kind == 1 then
    while true do end
  elseif kind == 2 then
    while true do pcall(function() local x = 0 for i = 1, 1e5 do x = x + i end end) end
  elseif kind == 3 then
    while true do pcall(function() error('x') end) end
  elseif kind == 4 then
    while true do xpcall(function() local x = 0 for i = 1, 1e5 do x = x + i end end, function() end) end
  elseif kind == 5 then
    while true do
      local co = coroutine.wrap(function() while true do end end)
      pcall(co)
    end
  elseif kind == 6 then
    while true do pcall(spi.execute, 'SELECT 1/0') end
  elseif kind == 7 then
    while true do spi.execute('SELECT count(*) FROM generate_series(1, 100000)') end
  elseif kind == 8 then
    local t = setmetatable({}, { __index = function(t, k) while true do end end })
    return t.x
  elseif kind == 9 then
    local s = 'x'
    while true do s = s .. s:sub(1, 10) end
  end
  return 0
$$;
GRANT EXECUTE ON FUNCTION pll_spin(int) TO attacker;
});

foreach my $kind (1 .. 7)
{
	my ($ret, $stdout, $stderr) = $node->psql(
		'postgres',
		qq{SET statement_timeout = '300ms'; SELECT pll_spin($kind);},
		extra_params => $attacker,
		timeout => 60);
	like($stderr, qr/canceling statement due to statement timeout/,
		"statement_timeout stops loop kind $kind");
}

# the session is usable afterwards
is($node->safe_psql('postgres', q{SELECT pll_try_auth()},
		extra_params => $attacker), 'attacker/attacker', 'session still works');

done_testing();
