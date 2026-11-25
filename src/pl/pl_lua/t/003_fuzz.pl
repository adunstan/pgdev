
# Copyright (c) 2026, PostgreSQL Global Development Group

# Run the random-call driver in t/fuzz.lua against a server, and check that
# the server survives: no crash, no assertion failure, no sanitizer report.
#
# The default run is short.  For a long run, set
#   PLLUA_FUZZ_SEEDS    how many seeds to run (default 4)
#   PLLUA_FUZZ_ITERS    actions per seed (default 400)
#   PLLUA_FUZZ_SEED0    the first seed (default 1)
#   PLLUA_FUZZ_TRACE    if set, log each action first, so that after a
#                       crash the last "fuzz seed" line in the server log
#                       names the action responsible
# A failure names its seed, and running that seed again repeats it.

use strict;
use warnings FATAL => 'all';

use FindBin;
use PostgreSQL::Test::Cluster;
use PostgreSQL::Test::Utils;
use Test::More;

my $nseeds = $ENV{PLLUA_FUZZ_SEEDS} // 4;
my $iters = $ENV{PLLUA_FUZZ_ITERS} // 400;
my $seed0 = $ENV{PLLUA_FUZZ_SEED0} // 1;
my $trace = $ENV{PLLUA_FUZZ_TRACE} ? 'true' : 'false';

my $node = PostgreSQL::Test::Cluster->new('fuzz');
$node->init;
$node->append_conf('postgresql.conf', q{
log_min_messages = log
restart_after_crash = off
});
$node->start;

my $fuzz_src = slurp_file("$FindBin::RealBin/fuzz.lua");

# The setup goes in a template database, and every seed gets its own copy, so
# that a seed does not depend on what the earlier ones did, and can be
# repeated alone.
$node->safe_psql('postgres', 'CREATE DATABASE fz_template');
$node->safe_psql('fz_template', q{
CREATE EXTENSION pl_lua;
CREATE EXTENSION hstore;
CREATE TYPE fz_enum AS ENUM ('a', 'b', 'c');
CREATE DOMAIN fz_domain AS integer CHECK (value > 0);
CREATE TYPE fz_comp AS (a integer, b varchar(3), c numeric(5,2), d integer[], e jsonb);
CREATE TYPE fz_nested AS (n fz_comp, arr fz_comp[], r int4range);
CREATE TABLE fz_t (id integer PRIMARY KEY, c fz_comp, v text);
INSERT INTO fz_t VALUES (1, ROW(1, 'abc', 1.5, '{1,2}', '{"k": 1}'), 'one'),
                        (2, NULL, 'two'),
                        (3, ROW(NULL, NULL, NULL, NULL, NULL), NULL);
});
$node->safe_psql('fz_template',
	"CREATE FUNCTION pl_lua_fuzz(seed integer, iterations integer, trace boolean)\n"
	  . "RETURNS text LANGUAGE pl_lua AS \$fuzz\$\n$fuzz_src\n\$fuzz\$;");

my $failed = 0;
for my $seed ($seed0 .. $seed0 + $nseeds - 1)
{
	my $db = "fz_$seed";
	$node->safe_psql('postgres', "CREATE DATABASE $db TEMPLATE fz_template");
	# the fuzzer and then, on Linux, the peak memory of the same session
	my $sql = "SELECT pl_lua_fuzz($seed, $iters, $trace);";
	$sql .= "\nSELECT 'peak memory of the session: ' || ((regexp_match("
	  . "pg_read_file('/proc/' || pg_backend_pid() || '/status'), "
	  . "'VmHWM:\\s+(\\d+)'))[1]::bigint / 1024) || ' MB';"
	  if $^O eq 'linux';
	my ($ret, $stdout, $stderr) = $node->psql($db, $sql);
	note($stdout);
	my $log = slurp_file($node->logfile);
	my $crashed = $log =~ /terminated by signal|TRAP:|Assertion .* failed|AddressSanitizer|runtime error:/;
	if ($crashed || $ret != 0)
	{
		$failed++;
		diag("seed $seed failed: exit $ret, stderr: $stderr");
		last;
	}
	$node->safe_psql('postgres', "DROP DATABASE $db");
}
is($failed, 0, "the server survived $nseeds seeds of $iters actions");

# the server still answers, and the objects of the setup still work
is($node->safe_psql('fz_template', 'SELECT count(*) FROM fz_t'), '3',
	'the server is still up');

$node->stop;
done_testing();
