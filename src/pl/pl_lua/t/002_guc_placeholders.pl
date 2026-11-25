
# Copyright (c) 2026, PostgreSQL Global Development Group

# The pl_lua.on_* settings run Lua code outside the sandbox, so only
# superusers may set them.  Until the library is loaded, a setting is only a
# placeholder, which any role can set; check that such values are not used
# when the library is loaded later, and that a superuser's settings still are.

use strict;
use warnings FATAL => 'all';

use PostgreSQL::Test::Cluster;
use PostgreSQL::Test::Utils;
use Test::More;

my $node = PostgreSQL::Test::Cluster->new('placeholders');
# on Windows, the roles that connect must be mapped to the operating system user
$node->init(auth_extra => [ '--create-role' => 'attacker,dbowner' ]);
$node->start;

my $dir = $node->basedir;
$dir =~ s{\\}{/}g;    # a backslash would start a Lua escape

$node->safe_psql('postgres', q{
CREATE EXTENSION pl_lua;
CREATE ROLE attacker LOGIN;
CREATE ROLE dbowner LOGIN;
CREATE DATABASE ownerdb OWNER dbowner;
GRANT USAGE ON LANGUAGE pl_lua TO attacker, dbowner;
CREATE FUNCTION probe() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(common_marker)
$$;
GRANT EXECUTE ON FUNCTION probe() TO attacker, dbowner;
});
$node->safe_psql('ownerdb', q{
CREATE EXTENSION pl_lua;
GRANT USAGE ON LANGUAGE pl_lua TO dbowner;
CREATE FUNCTION probe() RETURNS text LANGUAGE pl_lua AS $$
  return tostring(common_marker)
$$;
GRANT EXECUTE ON FUNCTION probe() TO dbowner;
});

# Lua that leaves a file behind, to show that code outside the sandbox ran
sub marker_code
{
	my ($name) = @_;
	return qq{local f = io.open("$dir/$name", "w") f:write("x") f:close()};
}

# 1. Set placeholders in a fresh session, then load the library by using it.
my ($ret, $stdout, $stderr) = $node->psql(
	'postgres',
	qq{
SET pl_lua.on_init = '} . marker_code('pwn_on_init') . qq{';
SET pl_lua.on_trusted_init = '} . marker_code('pwn_trusted_init') . qq{';
SET pl_lua.on_common_init = 'common_marker = "RAN"';
SELECT probe();
SHOW pl_lua.on_common_init;
},
	extra_params => [ '--username' => 'attacker' ]);
is($ret, 0, 'placeholders can be set before the library is loaded');
like($stdout, qr/^nil$/m, 'a placeholder on_common_init is not run');
unlike($stdout, qr/RAN/, 'its value is not kept');
like($stderr, qr/permission denied to set parameter "pl_lua\.on_init"/,
	'the placeholders are rejected when the library is loaded');
ok(!-e "$dir/pwn_on_init", 'on_init of a non-superuser did not run');
ok(!-e "$dir/pwn_trusted_init", 'on_trusted_init of a non-superuser did not run');

# 2. A role cannot make them its own defaults, nor can a database owner
($ret, $stdout, $stderr) = $node->psql(
	'postgres',
	q{ALTER ROLE attacker SET pl_lua.on_common_init = 'common_marker = "RAN2"'},
	extra_params => [ '--username' => 'attacker' ]);
isnt($ret, 0, 'a role cannot set its own default for on_common_init');
like($stderr, qr/permission denied to set parameter/, '... with this error');

($ret, $stdout, $stderr) = $node->psql(
	'ownerdb',
	q{ALTER DATABASE ownerdb SET pl_lua.on_init = '} . marker_code('pwn_db') . q{'},
	extra_params => [ '--username' => 'dbowner' ]);
isnt($ret, 0, 'a database owner cannot set on_init for the database');
like($stderr, qr/permission denied to set parameter/, '... with this error');

# 3. The same, set by a superuser, is used (so that the above is not vacuous)
$node->safe_psql('postgres',
	q{ALTER ROLE attacker SET pl_lua.on_common_init = 'common_marker = "SU"'});
is( $node->safe_psql(
		'postgres', 'SELECT probe()',
		extra_params => [ '--username' => 'attacker' ]),
	'SU',
	'a setting made by a superuser is used');

# 4. Settings that are not for code are still the user's own
is( $node->safe_psql(
		'postgres',
		q{SET pl_lua.strict = on; SET pl_lua.extra_gc_multiplier = 1; SELECT probe()},
		extra_params => [ '--username' => 'attacker' ]),
	'SU',
	'settings that do not run code can be set by anyone');

$node->stop;
done_testing();
